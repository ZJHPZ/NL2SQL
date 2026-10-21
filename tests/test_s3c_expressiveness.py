"""S3c 验收：语义层表达能力（累计 / HAVING / 跨期对比 / 分组内条件筛选）。

这批能力解决的是**同一个瓶颈**：语义层原先只能表达"分组 + 聚合 + 排序 + 取前 N"，
一旦问题要求在**聚合结果之上**再做点什么（筛组、开窗、转列），就只能走
`raw_select` 逃生通道——那条路能跑，但语义层没有真正表达它，
模型规划器也就学不会。

四件事各自的关键取舍（也是这个套件要盯住的点）：

| 能力 | 关键取舍 | 写错会怎样 |
|---|---|---|
| HAVING | 过滤**分组**而不是行 | 口径静默改变，数字看着合理 |
| 累计 | 按分组维度开窗，该维度必须在分组里 | 生成外层查不到的列 |
| 跨期对比 | 桶条件与 time_range 只能有一个 | 某一列恒为 0，不报错 |
| 组内前 N | 必须 `qualify`（窗口层之后），不能 `having` | 排名还不存在，取不到数 |

运行:
    python tests/test_s3c_expressiveness.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_DIR = os.path.join(ROOT, "golden_set")
GOLDEN_DB = os.path.join(GOLDEN_DIR, "ecommerce.db")
sys.path.insert(0, GOLDEN_DIR)

from nl2sql import NL2SQL, NL2SQLConfig  # noqa: E402
from nl2sql.contracts.llm import PlannerContext  # noqa: E402
from nl2sql.contracts.semantic import (  # noqa: E402
    Filter, Having, LogicalPlan, OrderBy, Pivot, PivotBucket,
    PlanDimension, PlanMeasure, TimeRange,
)
from nl2sql.dialect import SqlCompiler  # noqa: E402
from nl2sql.errors import CompileError  # noqa: E402
from nl2sql.llm import RulePlanner  # noqa: E402
from nl2sql.semantics.golden_model import build_model  # noqa: E402
from nl2sql.semantics.golden_plans import PLANS, summary  # noqa: E402
from nl2sql.sources.sqlite import SqliteAdapter  # noqa: E402

NOW = "2024-06-30"
H1 = TimeRange(field="order_date", start="2024-01-01", end="2024-06-30")
JUN = TimeRange(field="order_date", start="2024-06-01", end="2024-06-30")
_failures: list[str] = []


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


def _mk(q: str, **kw) -> LogicalPlan:
    return LogicalPlan(source_id="shop", sem_version="golden-v1", **kw)


def _compiler() -> SqlCompiler:
    cfg = NL2SQLConfig(sources={"shop": __import__(
        "nl2sql.config", fromlist=["SourceConfig"]).SourceConfig(
            source_id="shop", adapter="sqlite",
            options=__import__("nl2sql.config", fromlist=["SourceOptions"])
            .SourceOptions(path=GOLDEN_DB))}, default_source="shop")
    return SqlCompiler(SqliteAdapter(cfg.sources["shop"]).capabilities)


def _rows(sql: str) -> list[tuple]:
    conn = sqlite3.connect(f"file:{GOLDEN_DB}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        return cur.fetchall()
    finally:
        conn.close()


def _nums(rows: list[tuple]) -> list[list[float]]:
    out = []
    for r in rows:
        ns = []
        for v in r:
            try:
                ns.append(round(float(str(v).strip()), 6))
            except (TypeError, ValueError):
                pass
        out.append(sorted(ns))
    return sorted(out)


def _baseline() -> list[dict]:
    with open(os.path.join(GOLDEN_DIR, "baseline.json"), encoding="utf-8") as f:
        return json.load(f)["cases"]


# --------------------------------------------------------------------- A
def test_fingerprint() -> None:
    section("[A] 新构造必须进缓存指纹（否则不同问题共用一份缓存）")
    base = _mk("shop--ignored", measures=(PlanMeasure("net_revenue"),),
               dimensions=(PlanDimension("category"),))
    with_h = _mk("x", measures=(PlanMeasure("net_revenue"),),
                 dimensions=(PlanDimension("category"),),
                 having=(Having("net_revenue", ">", 20000),))
    with_q = _mk("x", measures=(PlanMeasure("net_revenue"),),
                 dimensions=(PlanDimension("category"),),
                 qualify=(Having("net_revenue", "<=", 2),))
    with_p = _mk("x", pivot=Pivot(
        buckets=(PivotBucket("本期", (Filter("order_date", "=", "2024-06-30"),)),),
        measure=PlanMeasure("net_revenue")))
    with_p2 = _mk("x", pivot=Pivot(
        buckets=(PivotBucket("上期", (Filter("order_date", "=", "2024-06-30"),)),),
        measure=PlanMeasure("net_revenue")))

    fps = {n: p.fingerprint() for n, p in (
        ("base", base), ("having", with_h), ("qualify", with_q),
        ("pivot", with_p), ("pivot2", with_p2))}
    check(len(set(fps.values())) == 5,
          "★ 五种计划的指纹两两不同", str(fps))
    check(base.fingerprint() == _mk(
        "other-id", measures=(PlanMeasure("net_revenue", ""),),
        dimensions=(PlanDimension("category", ""),)).fingerprint(),
        "同一语义、不同 source_id 占位不影响该断言（归一化仍稳定）")


# --------------------------------------------------------------------- B
def test_having() -> None:
    section("[B] HAVING：过滤分组，而不是过滤行")
    comp, model = _compiler(), build_model(order_level=True)
    plan = _mk("x", measures=(PlanMeasure("net_revenue", "实收金额"),),
               dimensions=(PlanDimension("category", "品类"),),
               time_range=H1,
               having=(Having("net_revenue", ">", 20000),),
               order_by=(OrderBy("实收金额"), OrderBy("品类", False)))
    sql = comp.compile(plan, model).sql
    check("HAVING" in sql, "★ 生成 HAVING 子句")
    check("HAVING SUM(CASE WHEN" in sql,
          "★ HAVING 左边是**聚合表达式**（不是列名）", sql.split("HAVING")[-1][:80])
    check("WHERE f.order_date" in sql and "WHERE f.amount > 20000" not in sql,
          "阈值没有跑到 WHERE 里（跑了就是过滤时机错）")
    rows = _rows(sql)
    check(len(rows) == 5, "结果 5 个品类（B5 基线）", str(len(rows)))
    check(all(float(r[1]) > 20000 for r in rows),
          "每行实收金额确实 > 20000", str([r[1] for r in rows]))

    # 行级过滤与分组级过滤不能混：同一个数字出现在"单个订单"里是 WHERE
    print("     对照：'单个订单金额超过5000' 必须是行级过滤")
    rp = RulePlanner(model).resolve(
        "2024年6月，单个订单成交金额超过5000元的订单有几笔？最高一笔多少？",
        PlannerContext(source_id="shop", now=NOW))
    check(not rp.having, "★ 行级阈值没有被误判成 HAVING", str(rp.having))
    check(any(f.field == "amount" for f in rp.filters),
          "★ 行级过滤保留在 filters 里", str(rp.filters))

    # 编译期保护：having 引用不存在的字段
    try:
        comp.compile(_mk("x", measures=(PlanMeasure("net_revenue"),),
                         dimensions=(PlanDimension("category"),),
                         having=(Having("no_such", ">", 1),)), model)
        check(False, "having 引用不存在字段应报错")
    except Exception as e:  # noqa: BLE001
        check("no_such" in str(e), "★ having 引用不存在字段 → 明确报错",
              f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------- C
def test_pivot() -> None:
    section("[C] 透视：跨期对比与分类转列共用一套机制")
    comp, model = _compiler(), build_model(order_level=True)

    # ---- 跨期对比 ----
    plan = _mk("x", pivot=Pivot(
        buckets=(
            PivotBucket("本期实收金额", (Filter("order_date", ">=", "2024-06-01"),
                                    Filter("order_date", "<=", "2024-06-30"))),
            PivotBucket("上期实收金额", (Filter("order_date", ">=", "2024-05-01"),
                                    Filter("order_date", "<=", "2024-05-31"))),
        ),
        measure=PlanMeasure("net_revenue", "实收金额")))
    sql = comp.compile(plan, model).sql
    check("__pivot_bucket" in sql and sql.count("WHEN p.\"__pivot_bucket\"") == 2,
          "★ 两个桶转成两列")
    rows = _rows(sql)
    check(len(rows) == 1 and len(rows[0]) == 2,
          "结果 1 行 2 列（C4 基线）", str(rows))
    check(abs(float(rows[0][0]) - 171907.99) < 0.01
          and abs(float(rows[0][1]) - 210179.88) < 0.01,
          "★ 两期数值与基线一致", str(rows[0]))

    # ---- 桶条件与 time_range 冲突必须被拒 ----
    bad = _mk("x", time_range=JUN, pivot=plan.pivot)
    try:
        comp.compile(bad, model)
        check(False, "桶条件与 time_range 冲突应被拒绝")
    except CompileError as e:
        check("time_range" in str(e) and "过滤" in str(e),
              "★ 桶条件与 time_range 冲突 → 编译期拒绝（不是静默出 0）",
              str(e)[:120])

    # ---- 分类转列 ----
    plan2 = _mk("x", dimensions=(PlanDimension("month", "月份"),),
                time_range=H1,
                pivot=Pivot(
                    buckets=(
                        PivotBucket("线上实收金额",
                                    (Filter("channel_type", "=", "线上"),)),
                        PivotBucket("线下实收金额",
                                    (Filter("channel_type", "=", "线下"),)),
                    ),
                    measure=PlanMeasure("net_revenue", "实收金额")),
                order_by=(OrderBy("月份", False),))
    sql2 = comp.compile(plan2, model).sql
    check("GROUP BY substr(f.order_date, 1, 7)" in sql2,
          "外层只按分组维度聚合（被转列的维度不在 GROUP BY 里）",
          sql2.split("GROUP BY")[-1][:60])
    rows2 = _rows(sql2)
    check(len(rows2) == 6 and len(rows2[0]) == 3,
          "6 行 × (月份, 线上, 线下)（C5 基线）", str(rows2[:2]))
    check(sum(float(r[1]) for r in rows2) > 0
          and sum(float(r[2]) for r in rows2) > 0,
          "两列都有值（若把桶维度也当分组列，会有一列恒为 0）")

    # ---- 桶没有条件：直接拒绝 ----
    try:
        comp.compile(_mk("x", pivot=Pivot(
            buckets=(PivotBucket("空桶", ()),),
            measure=PlanMeasure("net_revenue"))), model)
        check(False, "无条件桶应被拒绝")
    except CompileError as e:
        check("没有条件" in str(e), "★ 无条件桶 → 明确拒绝", str(e)[:100])


# --------------------------------------------------------------------- D
def test_window() -> None:
    section("[D] 窗口：累计 / 组内排名 / 组内前 N")
    comp, model = _compiler(), build_model(order_level=True)

    # ---- 累计 ----
    plan = _mk("x", measures=(PlanMeasure("net_revenue", "当月实收金额"),
                              PlanMeasure("cumulative_net_revenue", "累计实收金额")),
               dimensions=(PlanDimension("month", "月份"),),
               time_range=H1, order_by=(OrderBy("月份", False),))
    sql = comp.compile(plan, model).sql
    check("OVER (ORDER BY agg.\"月份\" ROWS BETWEEN UNBOUNDED PRECEDING"
          in sql.replace("\n", " ").replace("  ", " "),
          "★ 累计按**分组维度**开窗（维度被解析成内层列名）", sql[-260:])
    rows = _rows(sql)
    check(len(rows) == 6, "6 个月（E3 基线）", str(len(rows)))
    cum = [float(r[2]) for r in rows]
    check(all(cum[i] >= cum[i - 1] for i in range(1, len(cum))),
          "累计值单调不减", str(cum))
    check(abs(cum[-1] - sum(float(r[1]) for r in rows)) < 0.05,
          "最后一期累计 = 各期之和", f"{cum[-1]} vs {sum(float(r[1]) for r in rows)}")

    # ---- 缺排序维度：必须报可行动的错 ----
    try:
        comp.compile(_mk("x", measures=(
            PlanMeasure("cumulative_net_revenue", "累计"),),
            dimensions=(PlanDimension("category", "品类"),)), model)
        check(False, "累计指标缺分组维度应报错")
    except CompileError as e:
        check("不在分组里" in str(e) and "month" in str(e),
              "★ 窗口维度的缺漏 → 报出缺哪个维度（不是 SQL 报 no such column）",
              str(e)[:140])

    # ---- 组内前 N（三层）----
    top = _mk("x", measures=(PlanMeasure("quantity_total", "销量"),
                             PlanMeasure("product_rank_in_category", "名次")),
              dimensions=(PlanDimension("category", "品类"),
                          PlanDimension("product_name", "商品名称")),
              time_range=H1,
              qualify=(Having("名次", "<=", 2),),
              order_by=(OrderBy("品类", False), OrderBy("名次", False)))
    sql_t = comp.compile(top, model).sql
    check(sql_t.count("WITH agg AS") == 1 and ", win AS (" in sql_t,
          "★ 三层结构：agg → win → 过滤窗口结果", sql_t[:120])
    check('WHERE win."名次" <= 2' in sql_t,
          "★ 名次过滤发生在窗口层**之后**（窗口函数不能出现在 WHERE 里）")
    rows_t = _rows(sql_t)
    check(len(rows_t) == 10, "5 个品类 × 前 2 = 10 行（E2 基线）", str(len(rows_t)))
    by_cat: dict[str, list[int]] = {}
    for r in rows_t:
        by_cat.setdefault(str(r[0]), []).append(int(r[3]))
    check(all(sorted(v) == [1, 2] for v in by_cat.values()),
          "★ 每个品类内部都有名次 1 和 2（分区生效）", str(by_cat))

    # ---- qualify 引用不存在的列 ----
    try:
        comp.compile(_mk("x", measures=(PlanMeasure("quantity_total", "销量"),),
                         dimensions=(PlanDimension("category", "品类"),),
                         qualify=(Having("不存在的列", "<=", 2),)), model)
        check(False, "qualify 引用不存在的列应报错")
    except CompileError as e:
        check("不在窗口层的输出列里" in str(e) and "可用" in str(e),
              "★ qualify 列名不对 → 列出可用列", str(e)[:140])

    # ---- 组内排名（无 qualify）----
    rk = _mk("x", measures=(PlanMeasure("net_revenue", "实收金额"),
                            PlanMeasure("customer_rank_in_region", "地区内排名")),
             dimensions=(PlanDimension("customer_name", "客户姓名"),
                         PlanDimension("region", "地区")),
             time_range=H1)
    rows_r = _rows(comp.compile(rk, model).sql)
    check(len(rows_r) == 12, "12 位客户（E4 基线）", str(len(rows_r)))
    # 列顺序 = plan.dimensions 顺序：(客户姓名, 地区, 实收金额, 地区内排名)
    per_region: dict[str, list[int]] = {}
    for r in rows_r:
        per_region.setdefault(str(r[1]), []).append(int(r[3]))
    check(len(per_region) == 5, "5 个地区", str(sorted(per_region)))
    check(all(sorted(v) == list(range(1, len(v) + 1))
              for v in per_region.values()),
          "★ 每个地区内排名从 1 连续编号", str(per_region))

    # 维度没给别名时不能崩（PlanDimension 的字段是 dimension 不是 name）
    noalias = _mk("x", measures=(PlanMeasure("net_revenue"),),
                  dimensions=(PlanDimension("category"),),
                  time_range=H1)
    try:
        sql_na = comp.compile(noalias, model).sql
        check("AS \"category\"" in sql_na,
              "★ 维度未给别名时回退到维度名（不再 AttributeError）",
              sql_na[:80])
    except Exception as e:  # noqa: BLE001
        check(False, "维度未给别名时应能编译", f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------- E
def test_plans_converted() -> None:
    section("[E] 逃生通道收窄：raw_select 从 9 题降到 2 题")
    s = summary()
    check(s["semantic"] >= 28,
          f"★ {s['semantic']}/30 题用真实语义计划表达（原 21）", str(s))
    check(s["raw_select"] <= 2,
          f"★ 只剩 {s['raw_select']} 题走逃生通道", str(s["raw_select"]))
    raw_ids = sorted(k for k, v in PLANS.items() if v.raw_select)
    check(raw_ids == ["C3", "F1"],
          "★ 剩下的两题是**别的能力缺口**，而且被明确留在逃生通道",
          str(raw_ids))
    print("     C3 = 按 ISO 周分桶（需要方言函数，属于方言细节，"
          "放进可移植的指标文件反而是倒退）")
    print("     F1 = 与全局平均值的标量子查询比较（比较的右边是一个聚合结果，"
          "当前 qualify 只支持字面量）")

    # 四个新能力都在计划里被真的用上了
    kinds = set()
    for p in PLANS.values():
        if p.having:
            kinds.add("having")
        if p.qualify:
            kinds.add("qualify")
        if p.pivot:
            kinds.add("pivot")
        if any("cumulative" in m.metric for m in p.measures):
            kinds.add("cumulative")
        if any("rank" in m.metric for m in p.measures):
            kinds.add("rank")
    check(kinds == {"having", "qualify", "pivot", "cumulative", "rank"},
          "★ 五种新构造都在黄金计划里被使用（不是只写了代码）", str(sorted(kinds)))


# --------------------------------------------------------------------- F
def test_rule_planner() -> None:
    section("[F] 端到端：规则规划器对这四类问法的准确率")
    model = build_model(order_level=True)
    cases = _baseline()
    planner = RulePlanner(model)

    target = {"B5": "HAVING", "C6": "按天分桶 + HAVING", "C4": "跨期对比",
              "C5": "分类转列", "E2": "组内前N", "E3": "累计",
              "E4": "组内排名"}
    for cid, label in target.items():
        case = next(c for c in cases if c["id"] == cid)
        try:
            plan = planner.resolve(case["question"],
                                   PlannerContext(source_id="shop", now=NOW))
        except Exception as e:  # noqa: BLE001
            check(False, f"[{cid}] {label} 能规划", f"{type(e).__name__}: {e}")
            continue
        try:
            sql = _compiler().compile(plan, model).sql
            got = _rows(sql)
        except Exception as e:  # noqa: BLE001
            check(False, f"[{cid}] {label} 能执行", f"{type(e).__name__}: {e}")
            continue
        check(_nums(got) == _nums([tuple(r) for r in case["rows"]]),
              f"★ [{cid}] {label}：规则规划器算出的结果与基线一致",
              f"{len(got)} 行 vs 基线 {case['row_count']} 行")


def main() -> int:
    base = os.environ.get("NL2SQL_TEST_TMP") or os.path.join(
        ROOT, "data", "_test_s3c")
    work = os.path.join(base, "run")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)

    try:
        test_fingerprint()
        test_having()
        test_pivot()
        test_window()
        test_plans_converted()
        test_rule_planner()
    except Exception:
        traceback.print_exc()
        _failures.append("未捕获异常")

    shutil.rmtree(work, ignore_errors=True)
    print("\n" + "=" * 68)
    if _failures:
        print(f"S3c 验收未通过：{len(_failures)} 项失败")
        for x in _failures[:25]:
            print("  -", x)
        return 1
    print("S3c 验收全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
