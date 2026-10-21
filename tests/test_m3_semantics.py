"""M3 验收：语义表达力（时间粒度 / 标量比较）。

S3c 之后语义层能表达"分组 + 聚合 + 排序 + 取前 N"之上的东西（HAVING、
窗口、透视）。M3 补的是两个**语义层根本说不出来**的能力，它们的共同点是
"不是规划器不够聪明，而是契约里没有这个词"：

| 能力 | 原缺口 | 为什么必须进语义层 |
|---|---|---|
| 时间粒度（week/quarter…） | 只有 month/day 能分组，"每周"解析出 TimeRange.grain 却没人消费 | 截断写法是方言相关的（strftime vs DATE_FORMAT），写进语义文件就把文件绑死在一个引擎上 |
| 标量比较（"高于平均"） | 只能走 raw_select 逃生通道 | 比的是**另一个聚合的结果**，不是常量（HAVING）、也不是窗口值（qualify）；选错"哪个平均"会安静返回一组合理答案 |

这个套件要盯住的关键取舍：

* 粒度维度只存"时间字段 + 粒度"，截断由编译器按 `SourceCapabilities.dialect`
  展开；SELECT / GROUP BY / ORDER BY 必须走同一个出口，漏一处就会出现
  "按周分组、按日排序"且不报错。
* `ScalarCompare.against` 显式区分 `all`（整表，只保留时间范围）与
  `result`（当前结果集）——两者只在"还有额外行过滤"时不同，但那时选错
  就是一个安静的错答案。
* 规划器会向共享 `SemanticModel` 注册扩展指标（`count_status_*`，label
  "订单数"）。这改变了后续问题的匹配环境：本套件第 [E] 段专门守这个
  顺序无关性——它曾经让同一道 B6 在"单独问"和"问过 24 题之后"返回
  不同的列数（详见 docs/13）。

运行:
    python tests/test_m3_semantics.py
"""
from __future__ import annotations

import copy
import io
import json
import os
import shutil
import sqlite3
import sys
import traceback
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_DIR = os.path.join(ROOT, "golden_set")
GOLDEN_DB = os.path.join(GOLDEN_DIR, "ecommerce.db")
METRICS_YML = os.path.join(GOLDEN_DIR, "metrics.yml")
BASELINE = os.path.join(GOLDEN_DIR, "baseline.json")
sys.path.insert(0, GOLDEN_DIR)

from evaluate import rows_equal  # noqa: E402
from nl2sql import NL2SQL, NL2SQLConfig  # noqa: E402
from nl2sql.config import SourceConfig, SourceOptions  # noqa: E402
from nl2sql.contracts.llm import PlannerContext  # noqa: E402
from nl2sql.contracts.semantic import (  # noqa: E402
    Filter, Having, LogicalPlan, Pivot, PivotBucket, PlanDimension,
    PlanMeasure, ScalarCompare, TimeRange,
)
from nl2sql.contracts.source import SourceCapabilities  # noqa: E402
from nl2sql.dialect import SqlCompiler  # noqa: E402
from nl2sql.errors import CompileError  # noqa: E402
from nl2sql.llm import RulePlanner  # noqa: E402
from nl2sql.semantics.dbt_loader import (  # noqa: E402
    DbtMetricsError, load_with_report,
)
from nl2sql.semantics.golden_model import build_model  # noqa: E402
from nl2sql.sources.sqlite import SqliteAdapter  # noqa: E402

NOW = "2024-06-30"
H1 = TimeRange(field="order_date", start="2024-01-01", end="2024-06-30")
C3_Q = "2024年上半年，每周的有效订单数是多少？按周升序排列。"
F1_Q = "2024年上半年，消费金额高于全部客户平均消费金额的客户有哪些？"
B6_Q = "各地区的已退款订单数占该地区全部订单数的比例是多少？列出地区和退款率。"
B7_Q = "'已退款'和'已取消'的订单各有多少笔？"
_failures: list[str] = []
_work = ""


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        _failures.append(f"{label} {detail}")


def section(t: str) -> None:
    print("\n" + "=" * 68)
    print(t)
    print("=" * 68)


# --------------------------------------------------------------------- 装置
def _cfg() -> NL2SQLConfig:
    cfg = NL2SQLConfig(
        sources={"shop": SourceConfig(
            source_id="shop", adapter="sqlite",
            options=SourceOptions(path=GOLDEN_DB))},
        default_source="shop",
        state_dir=os.path.join(_work, "state"),
    )
    cfg.agent.planner = "rule"
    return cfg


def _compiler(dialect: str = "sqlite") -> SqlCompiler:
    if dialect == "sqlite":
        return SqlCompiler(SqliteAdapter(_cfg().sources["shop"]).capabilities)
    return SqlCompiler(SourceCapabilities(
        dialect=dialect, identifier_quote="`" if dialect == "mysql" else '"'))


def _rows(sql: str) -> tuple[list[str], list[tuple]]:
    conn = sqlite3.connect(f"file:{GOLDEN_DB}?mode=ro", uri=True)
    try:
        cur = conn.execute(sql)
        return [d[0] for d in cur.description], cur.fetchall()
    finally:
        conn.close()


def _baseline() -> dict[str, dict]:
    data = json.load(io.open(BASELINE, encoding="utf-8"))
    return {c["id"]: c for c in data["cases"]}


def _plan(model, question: str) -> LogicalPlan:
    return RulePlanner(model).resolve(
        question, PlannerContext(source_id="shop", now=NOW))


def _write_yml(name: str, text: str) -> str:
    path = os.path.join(_work, name)
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


_MIN_YML = """\
version: 2
semantic_models:
  - name: orders
    model: ref('fact_order')
    entities: [{{name: order, type: primary, expr: order_id}}]
    dimensions:
      - name: order_date
        expr: order_date
        meta:
          nl2sql:
            {extra}
    columns: [{{name: order_date}}]
metrics:
  - name: rows
    label: 行数
    calculation_method: count
    expression: order_id
"""


# --------------------------------------------------------------------- A
def part_a_semantic_file(model, ref) -> None:
    section("[A] 语义文件：粒度维度只是声明，不含方言")
    week = model.dimensions.get("week")
    check(week is not None, "dbt 模型里有 week 维度")
    if week is not None:
        check(week.grain == "week", "week 维度声明 grain=week", str(week.grain))
        check(week.description == "年周", "week 维度描述是「年周」",
              week.description)
        check(not week.select, "week 维度不用 select 逃逸通道", str(week.select))
        check("order_date" in week.expr,
              "week 维度的 expr 是时间字段（截断由编译器展开）", week.expr)
        check("strftime" not in week.expr and "DATE_FORMAT" not in week.expr,
              "语义文件里没有方言函数（否则同一文件换了引擎就错）", week.expr)
    rweek = ref.dimensions.get("week")
    check(rweek is not None and rweek.grain == "week",
          "手写参考模型与 dbt 模型在 week 上一致",
          str(getattr(rweek, "grain", None)))

    # month/day 也在 M3 从 `substr(...)` 迁到了 grain：语义文件里不再出现
    # 方言写法。这一条容易漏——"新加一个 week"不等于"老的两个也收编了"。
    for name, grain in (("month", "month"), ("day", "day")):
        dim = model.dimensions.get(name)
        ok = (dim is not None and dim.grain == grain
              and "substr" not in dim.expr and "order_date" in dim.expr)
        check(ok, f"{name} 维度已迁到 grain={grain}（不再写 substr）",
              f"expr={getattr(dim, 'expr', None)} grain={getattr(dim, 'grain', None)}")

    # ---- 词表：F1 的「消费金额」「客户」必须由语义层覆盖 ----
    for title, m in (("dbt", model), ("参考", ref)):
        check("消费金额" in m.metrics["net_revenue"].synonyms,
              f"{title}模型：net_revenue 覆盖「消费金额」",
              str(m.metrics["net_revenue"].synonyms))
        check("客户" in m.dimensions["customer_name"].synonyms,
              f"{title}模型：customer_name 覆盖「客户」",
              str(m.dimensions["customer_name"].synonyms))

    # ---- 加载器：坏声明要能照着改 ----
    bad = _write_yml("grain_bad.yml", _MIN_YML.format(
        extra="grain: hour"))
    try:
        load_with_report(bad, source_id="shop")
        check(False, "未知粒度在加载期被拒绝")
    except DbtMetricsError as exc:
        check("grain" in str(exc) and "hour" in str(exc),
              "未知粒度在加载期被拒绝，且报出可用值", str(exc)[:120])
    except Exception as exc:  # noqa: BLE001
        check(False, "未知粒度在加载期被拒绝（异常类型不对）",
              f"{type(exc).__name__}: {exc}")

    bad = _write_yml("grain_select.yml", _MIN_YML.format(
        extra="grain: week\n            select: SELECT 1"))
    try:
        load_with_report(bad, source_id="shop")
        check(False, "grain + select 在加载期被拒绝")
    except DbtMetricsError as exc:
        check("select" in str(exc), "grain + select 在加载期被拒绝并说明原因",
              str(exc)[:160])
    except Exception as exc:  # noqa: BLE001
        check(False, "grain + select 在加载期被拒绝（异常类型不对）",
              f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------- B
def part_b_grain(model) -> None:
    section("[B] 粒度编译：同一份计划在两个方言上都能执行")
    plan = _plan(model, C3_Q)
    check([d.dimension for d in plan.dimensions] == ["week"],
          "C3 计划带 week 维度", str([d.dimension for d in plan.dimensions]))
    check(plan.time_range is not None and plan.time_range.grain == "week",
          "C3 的 time_range 记录了 grain=week（供规划器补维度）",
          str(plan.time_range))

    sqlite = _compiler("sqlite")
    sql = sqlite.compile(plan, model).sql
    check("strftime('%Y-%W'" in sql, "sqlite 用 strftime('%Y-%W') 展开周桶",
          sql[:120])
    check(sql.count("strftime('%Y-%W'") >= 3,
          "★ SELECT / GROUP BY / ORDER BY 都走同一个展开出口",
          f"出现 {sql.count(chr(39)+'%Y-%W'+chr(39))} 次")
    check("GROUP BY strftime('%Y-%W'" in sql, "GROUP BY 用的是展开后的表达式",
          "")
    cols, rws = _rows(sql)
    check(cols == ["年周", "有效订单数"], "C3 输出列正确", str(cols))
    check(len(rws) == 26, "C3 有 26 个周桶（半年）", f"实际 {len(rws)} 行")
    check(rows_equal(rws, _baseline()["C3"]["rows"]),
          "C3 结果与黄金集 baseline 一致", "")

    # month/day 从 substr 迁到 grain 后，编译结果要和原来逐字一致
    c1 = _plan(model, "2024年上半年，每月的有效订单数和实收金额是多少？"
                      "按月份升序排列。")
    sql_c1 = sqlite.compile(c1, model).sql
    check("strftime('%Y-%m', f.order_date)" in sql_c1,
          "month 维度走 grain 展开（不再是 substr）", sql_c1[:120])
    c6 = _plan(model, "2024年6月，每天的实收金额是多少？"
                      "只列出实收金额大于3000元的日期，按日期升序。")
    sql_c6 = sqlite.compile(c6, model).sql
    check("strftime('%Y-%m-%d', f.order_date)" in sql_c6,
          "day 维度走 grain 展开（不再是 substr）", sql_c6[:120])

    mysql = _compiler("mysql")
    msql = mysql.compile(plan, model).sql
    check("DATE_FORMAT(f.order_date, '%Y-%u')" in msql,
          "mysql 用 DATE_FORMAT(..., '%Y-%u') 展开周桶",
          msql[:160])
    check("strftime" not in msql, "mysql SQL 里没有 sqlite 函数", "")
    check("`年周`" in msql, "mysql 用反引号引用中文别名", msql[:120])

    # ---- 未知方言 / 未知粒度 / 冲突声明：拒绝而不是猜 ----
    try:
        _compiler("oracle").compile(plan, model)
        check(False, "未知方言在编译期被拒绝")
    except CompileError as exc:
        check("oracle" in str(exc) and "已支持" in str(exc),
              "未知方言被拒绝，并列出已支持方言", str(exc)[:140])
    except Exception as exc:  # noqa: BLE001
        check(False, "未知方言在编译期被拒绝（异常类型不对）",
              f"{type(exc).__name__}: {exc}")

    broken = copy.deepcopy(model)
    broken.dimensions["week"].grain = "hour"
    try:
        sqlite.compile(plan, broken)
        check(False, "未知粒度在编译期被拒绝")
    except CompileError as exc:
        check("hour" in str(exc) and "未知粒度" in str(exc),
              "未知粒度被拒绝，说明当前方言支持哪些", str(exc)[:140])
    except Exception as exc:  # noqa: BLE001
        check(False, "未知粒度在编译期被拒绝（异常类型不对）",
              f"{type(exc).__name__}: {exc}")

    broken = copy.deepcopy(model)
    broken.dimensions["week"].select = "SELECT 1 AS week"
    broken.dimensions["week"].expr = ""
    try:
        sqlite.compile(plan, broken)
        check(False, "编译器也拒绝 grain + select 的组合")
    except CompileError as exc:
        check("grain 和 select" in str(exc) or "grain 和 select" in str(exc),
              "编译器拒绝 grain + select（加载器只挡住文件，代码也可能构造）",
              str(exc)[:140])
    except Exception as exc:  # noqa: BLE001
        check(False, "编译器拒绝 grain + select（异常类型不对）",
              f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------- C
def part_c_scalar_compare(model) -> None:
    section("[C] 标量比较：比的是另一个聚合的结果")
    f1 = _plan(model, F1_Q)
    check(f1.compare is not None, "F1 计划带 compare")
    if f1.compare is None:
        return
    check(f1.compare.measure == "net_revenue", "被比较指标是实收金额",
          f1.compare.measure)
    check(f1.compare.against == "all", "「全部客户平均」→ against=all",
          f1.compare.against)
    check(f1.compare.op == ">", "「高于」→ op='>'", f1.compare.op)

    plan = [_plan(model, F1_Q)][0]
    sql = _compiler().compile(plan, model).sql
    check(sql.startswith("WITH agg AS"), "编译成 CTE（聚合先落地）", sql[:60])
    check("(SELECT AVG(\"实收金额\") FROM agg)" in sql,
          "分母是同一聚合结果上的标量子查询", "")
    check("base AS" not in sql,
          "没有额外行过滤时不需要第二份聚合（all 与 result 等价）", "")
    cols, rws = _rows(sql)
    check(cols == ["客户姓名", "实收金额"], "F1 输出列正确", str(cols))
    check(rows_equal(rws, _baseline()["F1"]["rows"]),
          "F1 结果与黄金集 baseline 一致", f"{len(rws)} 行")
    same = _compiler().compile(
        replace(plan, compare=replace(f1.compare, against="result")),
        model).sql
    check(same == sql, "没有行过滤时 all 与 result 生成同一条 SQL（口径相同）")

    # ---- against 的两种含义：只有"还有额外过滤"时才分开 ----
    filtered = replace(plan, filters=(
        Filter(field="category", op="=", value="数码"),))
    sql_all = _compiler().compile(filtered, model).sql
    sql_res = _compiler().compile(
        replace(filtered, compare=replace(f1.compare, against="result")),
        model).sql
    check("base AS" in sql_all,
          "★ against=all 且有额外过滤 → 多一份只含时间范围的聚合", "")
    check("base AS" not in sql_res,
          "★ against=result → 分母就是当前结果集，不要第二份", "")
    _, rows_all = _rows(sql_all)
    _, rows_res = _rows(sql_res)
    check(set(map(tuple, rows_all)) != set(map(tuple, rows_res)),
          "★ all 与 result 在这里给出不同结果（选错会安静地错）",
          f"all={len(rows_all)} 行 / result={len(rows_res)} 行")

    # ---- factor / op ----
    sql12 = _compiler().compile(
        replace(plan, compare=replace(f1.compare, factor=1.2)), model).sql
    check("* 1.2" in sql12, "倍数写进 SQL（> 平均 × 1.2）", "")
    _, rows12 = _rows(sql12)
    check(set(map(tuple, rows12)) <= set(map(tuple, rws)),
          "乘 1.2 后结果是被比较结果集的子集", f"{len(rows12)} ⊆ {len(rws)}")
    sql_lt = _compiler().compile(
        replace(plan, compare=replace(f1.compare, op="<")), model).sql
    check(" < " in sql_lt, "「低于」→ 比较符写进 SQL", "")
    check(bool(_rows(sql_lt)[1]), "「低于平均」能查到数据（不是空集）")

    # ---- 拒绝不成立组合：报错要能照着改 ----
    not_in_plan = replace(plan, compare=replace(
        f1.compare, measure="customer_count"))
    try:
        _compiler().compile(not_in_plan, model)
        check(False, "compare 引用不在计划里的指标时被拒绝")
    except CompileError as exc:
        check("不在计划指标里" in str(exc),
              "compare 引用不在计划里的指标时被拒绝", str(exc)[:120])
    except Exception as exc:  # noqa: BLE001
        check(False, "compare 引用不在计划里的指标时被拒绝（异常类型不对）",
              f"{type(exc).__name__}: {exc}")

    window_plan = LogicalPlan(
        source_id="shop", sem_version="golden-v1",
        measures=[PlanMeasure("revenue_rank", "排名")],
        dimensions=[PlanDimension("customer_name", "客户姓名")],
        time_range=H1, compare=ScalarCompare(measure="revenue_rank"),
    )
    try:
        _compiler().compile(window_plan, model)
        check(False, "窗口指标不能做标量比较（没有「整表平均」概念）")
    except CompileError as exc:
        check("窗口指标" in str(exc), "窗口指标被拒绝并说明原因",
              str(exc)[:140])
    except Exception as exc:  # noqa: BLE001
        check(False, "窗口指标不能做标量比较（异常类型不对）",
              f"{type(exc).__name__}: {exc}")

    pivot_plan = replace(plan, pivot=Pivot(
        buckets=[PivotBucket(label="A", conditions=[])], measure=plan.measures[0]))
    try:
        _compiler().compile(pivot_plan, model)
        check(False, "compare + pivot 被拒绝")
    except CompileError as exc:
        check("透视" in str(exc), "compare + pivot 被拒绝并说明原因",
              str(exc)[:120])
    except Exception as exc:  # noqa: BLE001
        check(False, "compare + pivot 被拒绝（异常类型不对）",
              f"{type(exc).__name__}: {exc}")

    qualify_plan = replace(plan, qualify=(Having("实收金额", ">", 1),))
    try:
        _compiler().compile(qualify_plan, model)
        check(False, "compare + qualify 被拒绝")
    except CompileError as exc:
        check("qualify" in str(exc), "compare + qualify 被拒绝并说明原因",
              str(exc)[:120])
    except Exception as exc:  # noqa: BLE001
        check(False, "compare + qualify 被拒绝（异常类型不对）",
              f"{type(exc).__name__}: {exc}")

    # ---- 指纹：compare 必须进指纹，否则两个问题共用缓存 ----
    base_fp = plan.fingerprint()
    check(base_fp != replace(
        plan, compare=replace(f1.compare, op="<")).fingerprint(),
        "★ 只改 op 也改变指纹（否则「高于/低于」共用缓存）")
    check(replace(plan, compare=None).fingerprint() != base_fp,
          "★ 去掉 compare 改变指纹")


# --------------------------------------------------------------------- D
def part_d_planner(model) -> None:
    section("[D] 规划器识别：口径从问句里读出来，不是猜的")
    cases = [
        ("2024年上半年，消费金额高于本组平均消费金额的客户有哪些？",
         "result", ">", 1.0),
        ("2024年上半年，消费金额低于全部客户平均消费金额的客户有哪些？",
         "all", "<", 1.0),
        ("2024年上半年，消费金额高于全部客户平均消费金额1.2倍的客户有哪些？",
         "all", ">", 1.2),
        ("2024年上半年，消费金额高于平均消费金额的客户有哪些？",
         "all", ">", 1.0),
    ]
    for q, against, op, factor in cases:
        try:
            plan = _plan(model, q)
        except Exception as exc:  # noqa: BLE001
            check(False, f"规划器识别：{q[:22]}…",
                  f"{type(exc).__name__}: {exc}")
            continue
        got = plan.compare
        ok = (got is not None and got.against == against
              and got.op == op and abs(got.factor - factor) < 1e-9)
        check(ok, f"「{q[:14]}…」→ {against} {op} ×{factor:g}",
              str(got))
        check(any("标量比较" in n for n in plan.notes),
              "计划 notes 里留了「这是标量比较不是 HAVING」的线索")

    # 「高于 5000 元」不是标量比较（没有平均）；不能误判
    plan = _plan(model, "2024年6月，单个订单成交金额超过5000元的订单有几笔？"
                        "最高一笔多少？")
    check(plan.compare is None,
          "★ 没有「平均」时不误判成标量比较（那是 HAVING/行过滤）",
          str(plan.compare))
    check({m.metric for m in plan.measures}
          == {"paid_order_count", "max_order_amount"},
          "A3 两个指标都识别出来了（「几笔」已进词表）",
          str([m.metric for m in plan.measures]))


# --------------------------------------------------------------------- E
def part_e_order_independence(model, engine) -> None:
    section("[E] 顺序无关：先问过什么，不能改变后面问题的列数")
    before = engine.ask(B6_Q, now=NOW, use_cache=False)
    check(before.plan is not None
          and [m.metric for m in before.plan.measures] == ["refund_rate"],
          "B6 单独问：只保留比率指标（分子分母是解释性成分）",
          str([m.metric for m in before.plan.measures]
              if before.plan else None))
    check(before.result is not None
          and len(before.result.columns) == 2,
          "B6 单独问：2 列", str(before.result.columns if before.result else None))

    # 这道题会向共享模型注册 count_status_* 扩展指标（label「订单数」）
    ask = engine.ask(B7_Q, now=NOW, use_cache=False)
    check(ask.status == "ok", "B7（各状态订单数）能作答", str(ask.status))
    after = engine.ask(B6_Q, now=NOW, use_cache=False)
    measures = [m.metric for m in after.plan.measures] if after.plan else []
    check(measures == ["refund_rate"],
          "★ 问过 B7 之后 B6 仍然只保留 refund_rate（曾因短 label "
          "「订单数」子串命中「全部订单数」而多出两列）", str(measures))
    check(after.result is not None and len(after.result.columns) == 2,
          "★ 问过 B7 之后 B6 仍是 2 列",
          str(after.result.columns if after.result else None))
    check(after.result is not None
          and rows_equal(after.result.rows, _baseline()["B6"]["rows"]),
          "B6 结果与黄金集一致", "")


# --------------------------------------------------------------------- F
def part_f_golden(model, engine) -> None:
    section("[F] 黄金集（Execution Accuracy，判分口径同 evaluate.rows_equal）")
    baseline = _baseline()
    passed, failed = 0, []
    for cid in sorted(baseline):
        case = baseline[cid]
        try:
            out = engine.ask(case["question"], now=NOW, use_cache=False)
        except Exception as exc:  # noqa: BLE001
            failed.append(cid)
            print(f"  ..    {cid} 异常 {type(exc).__name__}: {exc}")
            continue
        if out.result is not None and rows_equal(out.result.rows, case["rows"]):
            passed += 1
        else:
            failed.append(cid)
            got = len(out.result.rows) if out.result else None
            print(f"  ..    {cid} 不匹配（期望 {len(case['rows'])} 行，"
                  f"得到 {got} 行）")
    check(passed == 29, f"规则规划器 {passed}/30 = {passed / 30:.1%}（M3 验收线 ≥28/30）",
          f"失败: {failed}")
    check(set(failed) == {"D3"},
          "★ 唯一失败是 D3 —— 题面没问客户数，黄金 SQL 却多一列；"
          "记为黄金集口径不一致，不写规划器硬编码", str(failed))
    d3 = engine.ask(baseline["D3"]["question"], now=NOW, use_cache=False)
    check(d3.status == "ok", "D3 仍能作答（不是报错，只是列数少）",
          str(d3.status))


# --------------------------------------------------------------------- 主流程
def main() -> int:
    global _work
    # 工作目录沿用项目约定放在 data/ 下：系统临时目录在本机沙箱里
    # 偶发 ACL 异常（新建目录后写文件被拒），而 data/ 是已知可写的。
    _work = os.path.join(ROOT, "data", "_test_m3", "run")
    shutil.rmtree(_work, ignore_errors=True)
    os.makedirs(_work, exist_ok=True)
    try:
        print("M3 语义表达力验收：时间粒度与标量比较")
        model, report = load_with_report(METRICS_YML, source_id="shop")
        ref = build_model(order_level=True)
        print(f"dbt 模型指标 {len(model.metrics)} 个 / 维度 "
              f"{len(model.dimensions)} 个；可移植性 "
              f"{report.portability:.1%}")

        part_a_semantic_file(model, ref)
        part_b_grain(model)
        part_c_scalar_compare(model)
        part_d_planner(model)

        cfg = _cfg()
        engine = NL2SQL(cfg, semantic_model=model)
        engine.refresh_snapshot()
        part_e_order_independence(model, engine)
        part_f_golden(model, engine)
    except Exception:
        traceback.print_exc()
        print("\n[异常] 套件未跑完")
        return 1
    finally:
        shutil.rmtree(_work, ignore_errors=True)

    print("\n" + "=" * 68)
    if _failures:
        print(f"M3 验收失败：{len(_failures)} 项")
        for f in _failures:
            print("  -", f)
        return 1
    print("M3 语义表达力验收全部通过（黄金集 29/30，D3 为已知口径不一致）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
