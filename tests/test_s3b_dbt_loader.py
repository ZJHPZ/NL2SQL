"""S3b 验收：dbt `metrics.yml` 加载器。

这个套件要回答一个问题：**"语义层兼容 dbt metrics.yml"是不是一句空话？**

分三段验证
----------
[A] 加载器本身：两种规范、表达式翻译、错误可行动、报告诚实
[B] 等价性（**核心**）：把黄金语义模型用 dbt 语法重述一遍，
    加载出来必须与手写的 `golden_model.build_model()` **行为等价**：
    30 道黄金题逐题比较 *逻辑计划* → *编译出的 SQL 文本* → *结果集*。
    任何一层不一致都说明加载器丢了信息。
[C] 集成：`NL2SQLConfig` 指到 yml 后，整条链路（提问 → SQL → 结果）仍然通。

为什么等价性要用三层而不是只看结果
----------------------------------
只看结果会漏掉"计划不同但恰好结果相同"的情况（例如少了一个恒真过滤），
那种模型一旦遇到别的问句就错。计划相同 + SQL 文本相同是最强的证据：
说明加载器**没有丢失任何进入编译的信息**。

运行:
    python tests/test_s3b_dbt_loader.py
"""
from __future__ import annotations

import io
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
GOLDEN_YML = os.path.join(GOLDEN_DIR, "metrics.yml")
sys.path.insert(0, GOLDEN_DIR)

from golden_questions import GOLDEN_SET  # noqa: E402

from nl2sql.contracts.llm import PlannerContext  # noqa: E402
from nl2sql.dialect import SqlCompiler  # noqa: E402
from nl2sql.llm import RulePlanner  # noqa: E402
from nl2sql.semantics.dbt_loader import (  # noqa: E402
    DbtMetricsError, DbtMetricsLoader, load_with_report,
)
from nl2sql.semantics.golden_model import build_model  # noqa: E402
from nl2sql.sources.sqlite import SqliteAdapter  # noqa: E402

NOW = "2024-06-30"
_failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        _failures.append(f"{label} {detail}")


def section(t: str) -> None:
    print("\n" + "=" * 66)
    print(t)
    print("=" * 66)


def _write(path: str, text: str) -> str:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


# --------------------------------------------------------------------- A
def test_loader(work: str) -> None:
    section("[A] 加载器：两种规范 / 表达式翻译 / 错误可行动")

    model, report = load_with_report(GOLDEN_YML, source_id="shop")
    ref = build_model(order_level=True)
    # 与参考模型**比数量**而不是比硬编码数字：语义层会继续长，
    # 写死 14/19 会让每次扩充能力都误报失败，最后没人看这个测试。
    check(len(model.metrics) == len(ref.metrics),
          f"黄金 yml 与参考模型指标数一致（{len(ref.metrics)}）",
          f"{len(model.metrics)} vs {len(ref.metrics)}")
    check(len(model.dimensions) == len(ref.dimensions),
          f"维度数一致（{len(ref.dimensions)}）",
          f"{len(model.dimensions)} vs {len(ref.dimensions)}")
    check(model.base_table == "fact_order", "base_table 来自 ref()",
          model.base_table)
    check("legacy" in report.specs and "latest" in report.specs,
          "★ 同一份文件同时用了两套规范（都能读）", str(report.specs))
    check(report.entity_joins == 2,
          "★ JOIN 由 entities 外键推导（不是手写 join 配置）",
          str(report.entity_joins))

    # ---- 可移植性报告必须诚实：dbt 真做不到的要单独标出来 ----
    structural = set(report.structural_metrics)
    check({"revenue_share", "revenue_rank"} <= structural,
          "★ 窗口指标被标为「dbt 无等价物」", str(sorted(structural)))
    check(all(ref.metrics[n].window for n in structural if n in ref.metrics),
          "★ 被标为结构扩展的确实都是窗口指标（分类标准一致）",
          str(sorted(n for n in structural
                     if n in ref.metrics and not ref.metrics[n].window)))
    check("window" in report.extension_keys, "扩展键里列出了 window",
          str(report.extension_keys))
    check(0.3 < report.portability < 1.0,
          "口径可移植性是一个中间值（不是自吹的 100%）",
          str(report.portability))
    check(report.describe()["portability"] == round(report.portability, 4),
          "describe() 与属性一致")

    # ---- legacy 原生写法：条件聚合 ----
    net = model.metrics["net_revenue"]
    check(
        net.expr == "SUM(CASE WHEN {{ dimension('order_status') }} = '已支付'"
                    " THEN {{ dimension('amount') }}"
                    " - {{ dimension('discount_amount') }} END)",
        "★ calculation_method + filters → 条件聚合（不是 WHERE）", net.expr)
    check(net.decimals == 2 and net.synonyms,
          "标注类扩展（精度 / 同义词）读进来了",
          f"decimals={net.decimals} syn={net.synonyms[:2]}")

    check(model.metrics["all_order_count"].expr == "COUNT(*)",
          "count 不写 expression → COUNT(*)", model.metrics["all_order_count"].expr)
    check(model.metrics["customer_count"].expr
          == "COUNT(DISTINCT {{ dimension('customer_id') }})",
          "count_distinct → COUNT(DISTINCT x)", model.metrics["customer_count"].expr)

    # ---- latest 规范：type: ratio ----
    rr = model.metrics["refund_rate"]
    check("refunded_order_count" in rr.expr and "all_order_count" in rr.expr
          and "NULLIF" in rr.expr,
          "★ type: ratio → 分子/分母 + NULLIF 保护", rr.expr)

    # ---- jinja 翻译 ----
    p = _write(os.path.join(work, "jinja.yml"), """
version: 2
semantic_models:
  - name: orders
    model: ref('fact_order')
    entities: [{name: order, type: primary, expr: order_id}]
    dimensions:
      - {name: order_status, expr: order_status}
      - {name: amount, expr: amount}
    columns: [{name: order_status}, {name: amount}]
metrics:
  - name: paid
    label: 已支付
    calculation_method: sum
    expression: amount
    filter: "{{ Dimension('order__order_status') }} = '已支付'"
  - name: by_month
    label: 月度
    calculation_method: sum
    expression: amount
    filter: "{{ TimeDimension('order__order_date', 'month') }} > '2024-01-01'"
""")
    m2, r2 = load_with_report(p, source_id="shop")
    check("{{ dimension('order_status') }}" in m2.metrics["paid"].expr,
          "★ MetricFlow jinja `Dimension('a__b')` 翻成内部语法",
          m2.metrics["paid"].expr)
    check(any("粒度" in w for w in r2.warnings),
          "★ TimeDimension 的粒度被丢弃时发警告（会改变行数，不能静默）",
          str(r2.warnings))

    # ---- SUM(SUM(x)) 防护 ----
    p = _write(os.path.join(work, "double.yml"), """
version: 2
semantic_models:
  - name: orders
    model: ref('fact_order')
    entities: [{name: order, type: primary, expr: order_id}]
    dimensions: [{name: amount, expr: amount}]
    columns: [{name: amount}]
metrics:
  - name: doubled
    calculation_method: sum
    expression: SUM(amount)
""")
    m3, r3 = load_with_report(p, source_id="shop")
    check(m3.metrics["doubled"].expr.count("SUM(") == 1,
          "★ expression 已自带聚合时不再包一层（否则 SUM(SUM(x))）",
          m3.metrics["doubled"].expr)
    check(any("自带聚合" in w for w in r3.warnings), "且发出提示", str(r3.warnings))

    # ---- 字符串字面量不能被改写 ----
    p = _write(os.path.join(work, "literal.yml"), """
version: 2
semantic_models:
  - name: orders
    model: ref('fact_order')
    entities: [{name: order, type: primary, expr: order_id}]
    dimensions: [{name: amount, expr: amount}]
    columns: [{name: amount}]
metrics:
  - name: weird
    calculation_method: count
    expression: amount
    filters:
      - {field: order_status, operator: '=', value: "'amount'"}
""")
    m4, _ = load_with_report(p, source_id="shop")
    check("'amount'" in m4.metrics["weird"].expr,
          "★ 字符串字面量里的列名不被改写成维度引用",
          m4.metrics["weird"].expr)

    # ---- 不支持的 dbt 类型：拒绝 + 说明原因，但不拖垮整份文件 ----
    p = _write(os.path.join(work, "unsupported.yml"), """
version: 2
semantic_models:
  - name: orders
    model: ref('fact_order')
    entities: [{name: order, type: primary, expr: order_id}]
    dimensions: [{name: amount, expr: amount}]
    columns: [{name: amount}]
metrics:
  - name: good_one
    calculation_method: sum
    expression: amount
  - name: rolling
    type: cumulative
    input_metric: good_one
    window: 7 days
  - name: funnel
    type: conversion
    entity: order
    calculation: conversion_rate
    base_metric: good_one
    conversion_metric: good_one
""")
    m5, r5 = load_with_report(p, source_id="shop")
    check("good_one" in m5.metrics, "★ 一个指标不支持不影响其它指标加载",
          str(list(m5.metrics)))
    check(set(r5.rejected_metrics) == {"rolling", "funnel"},
          "累计算 / 转化被拒绝并记录", str(r5.rejected_metrics))
    joined = " ".join(r5.warnings)
    check("time spine" in joined, "拒绝原因说清了为什么（time spine）",
          joined[:160])
    check("转化表" in joined or "事件配对" in joined,
          "转化指标给了可行替代方案", joined[:200])

    # ---- 其它错误路径：消息要能照着改 ----
    p = _write(os.path.join(work, "badcalc.yml"), """
version: 2
semantic_models:
  - name: orders
    model: ref('fact_order')
    entities: [{name: order, type: primary, expr: order_id}]
metrics:
  - name: nope
    calculation_method: geometric_mean
    expression: amount
""")
    _, r6 = load_with_report(p, source_id="shop")
    check(any("geometric_mean" in w and "可用" in w for w in r6.warnings),
          "未知 calculation_method 报出可用值", str(r6.warnings)[:200])

    p = _write(os.path.join(work, "neither.yml"), """
version: 2
semantic_models:
  - name: orders
    model: ref('fact_order')
    entities: [{name: order, type: primary, expr: order_id}]
metrics:
  - name: vague
    expression: amount
""")
    _, r7 = load_with_report(p, source_id="shop")
    check(any("calculation_method" in w and "type" in w for w in r7.warnings),
          "既无 calculation_method 也无 type → 提示两种规范怎么写",
          str(r7.warnings)[:200])

    # ---- 缺 columns 时提示（不猜列名）----
    # 注意 expr 要加引号：YAML 流式映射里逗号是分隔符，
    # `expr: substr(order_date, 1, 7)` 会被拆成 `expr: substr(order_date`
    # 加一个新键。这正是加载器要发"不认识的键"警告的场景。
    p = _write(os.path.join(work, "nocols.yml"), """
version: 2
semantic_models:
  - name: orders
    model: ref('fact_order')
    entities: [{name: order, type: primary, expr: order_id}]
    dimensions: [{name: month, expr: "substr(order_date, 1, 7)"}]
metrics:
  - name: total
    calculation_method: sum
    expression: amount
""")
    m8, r8 = load_with_report(p, source_id="shop")
    check(m8.dimensions["month"].expr == "substr(order_date, 1, 7)",
          "没声明 columns 时**不猜**列名（保持原样）",
          m8.dimensions["month"].expr)
    check(any("columns" in w for w in r8.warnings),
          "★ 并提示补 columns: 而不是让它静默留坑", str(r8.warnings)[:200])

    # 未加引号的流式写法：YAML 会把它拆成 `expr: substr(order_date` + 新键，
    # 加载器必须**说出来**，否则用户会以为表达式生效了
    p = _write(os.path.join(work, "trap.yml"), """
version: 2
semantic_models:
  - name: orders
    model: ref('fact_order')
    entities: [{name: order, type: primary, expr: order_id}]
    dimensions: [{name: month, expr: substr(order_date, 1, 7)}]
    columns: [{name: order_date}]
metrics:
  - name: total
    calculation_method: sum
    expression: amount
""")
    _, r8b = load_with_report(p, source_id="shop")
    check(any("不认识的键" in w for w in r8b.warnings),
          "★ YAML 流式写法漏引号 → 报「不认识的键」而不是静默半解析",
          str(r8b.warnings)[:220])

    # ---- 多文件合并（dbt 就是这么组织的）----
    p1 = _write(os.path.join(work, "m1.yml"), """
version: 2
semantic_models:
  - name: orders
    model: ref('fact_order')
    entities: [{name: order, type: primary, expr: order_id}]
    dimensions: [{name: amount, expr: amount}]
    columns: [{name: amount}, {name: discount_amount}]
""")
    p2 = _write(os.path.join(work, "m2.yml"), """
version: 2
metrics:
  - name: total
    calculation_method: sum
    expression: amount - discount_amount
""")
    m9, r9 = load_with_report([p1, p2], source_id="shop")
    check("total" in m9.metrics and len(r9.files) == 2,
          "★ 多文件合并（指标与语义模型分开放）", str(r9.files))
    check("f.discount_amount" in m9.metrics["total"].expr,
          "跨文件时裸列名仍能补上表别名", m9.metrics["total"].expr)

    # ---- 错误信息 ----
    try:
        load_with_report(os.path.join(work, "nope.yml"), source_id="shop")
        check(False, "文件不存在时报错")
    except DbtMetricsError as e:
        check("不存在" in str(e), "文件不存在 → 明确报错", str(e))

    p = _write(os.path.join(work, "broken.yml"), "metrics: [ {name: x,\n")
    try:
        load_with_report(p, source_id="shop")
        check(False, "YAML 语法错时报错")
    except DbtMetricsError as e:
        check("YAML" in str(e) and "broken.yml" in str(e),
              "★ YAML 语法错 → 报出文件名（不是静默半份模型）", str(e)[:120])

    p = _write(os.path.join(work, "empty.yml"), "version: 2\n")
    try:
        load_with_report(p, source_id="shop")
        check(False, "没有语义模型时报错")
    except DbtMetricsError as e:
        check("语义模型" in str(e), "★ 没有任何语义模型 → 明确报错并给写法",
              str(e)[:160])

    # ---- base_table 覆盖 ----
    p = _write(os.path.join(work, "two.yml"), """
version: 2
semantic_models:
  - name: a
    model: ref('fact_order')
    entities: [{name: order, type: primary, expr: order_id}]
  - name: b
    model: ref('dim_customer')
    entities: [{name: customer, type: primary, expr: customer_id}]
""")
    m10, _ = load_with_report(p, source_id="shop")
    check(m10.base_table == "fact_order", "默认选第一个带 primary 实体的模型",
          m10.base_table)
    m11, _ = load_with_report(p, source_id="shop", base_table="dim_customer")
    check(m11.base_table == "dim_customer", "base_table 可覆盖", m11.base_table)


# --------------------------------------------------------------------- B
def test_equivalence() -> None:
    section("[B] 等价性：yml 加载的模型 ≡ 手写参考模型（30 题三层比对）")

    ref = build_model(order_level=True)
    got, report = load_with_report(GOLDEN_YML, source_id="shop")

    check(set(ref.metrics) == set(got.metrics),
          "指标集合一致",
          f"仅参考={sorted(set(ref.metrics) - set(got.metrics))} "
          f"仅加载={sorted(set(got.metrics) - set(ref.metrics))}")
    check(set(ref.dimensions) == set(got.dimensions),
          "维度集合一致",
          f"仅参考={sorted(set(ref.dimensions) - set(got.dimensions))} "
          f"仅加载={sorted(set(got.dimensions) - set(ref.dimensions))}")
    check(ref.base_table == got.base_table and ref.base_alias == got.base_alias,
          "FROM 与别名一致", f"{ref.base_table}/{got.base_table}")
    check(ref.logical_source == got.logical_source,
          "★ 订单粒度派生表（logical_source）一致")
    check(ref.logical_tables == got.logical_tables,
          "logical_tables 一致", str(got.logical_tables))
    check({(j.table, j.condition, j.kind) for j in ref.joins.values()}
          == {(j.table, j.condition, j.kind) for j in got.joins.values()},
          "★ JOIN（含别名与条件）一致",
          str(sorted(j.condition for j in got.joins.values())))
    check(set(ref.table_joins) == set(got.table_joins),
          "表→join 映射一致", str(sorted(got.table_joins)))

    # 表达式与口径字段逐条比对
    bad_expr, bad_meta, bad_syn = [], [], []
    for name, r in ref.metrics.items():
        g = got.metrics[name]
        if r.expr != g.expr:
            bad_expr.append(name)
        for attr in ("window", "decimals", "ratio", "table",
                     "label", "description"):
            if getattr(r, attr) != getattr(g, attr):
                bad_meta.append(f"{name}.{attr}")
        if sorted(r.synonyms) != sorted(g.synonyms):
            bad_syn.append(name)
    for name, r in ref.dimensions.items():
        g = got.dimensions[name]
        if (r.resolved_expr(), r.table, r.select, r.alias) != \
           (g.resolved_expr(), g.table, g.select, g.alias):
            bad_expr.append(f"dim:{name}")
        if sorted(r.synonyms) != sorted(g.synonyms):
            bad_syn.append(f"dim:{name}")

    check(not bad_expr, "★ 每个指标/维度的表达式逐字一致",
          str(bad_expr[:6]))
    check(not bad_meta, "★ 口径字段（window/decimals/ratio/table）一致",
          str(bad_meta[:6]))
    check(not bad_syn, "★ 同义词一致（规划器靠它匹配中文问法）",
          str(bad_syn[:6]))

    # ---- 30 题：计划 → SQL → 结果 ----
    from nl2sql.config import NL2SQLConfig, SourceOptions, SourceConfig

    cfg = NL2SQLConfig(
        sources={"shop": SourceConfig(
            source_id="shop", adapter="sqlite",
            options=SourceOptions(path=GOLDEN_DB))},
        default_source="shop",
    )
    adapter = SqliteAdapter(cfg.sources["shop"])
    compiler = SqlCompiler(adapter.capabilities)
    pr, pg = RulePlanner(ref), RulePlanner(got)

    plan_bad, sql_bad, row_bad, raised = [], [], [], []
    conn = sqlite3.connect(f"file:{GOLDEN_DB}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        for q in GOLDEN_SET:
            ctx = PlannerContext(source_id="shop", now=NOW)
            # 两边**分别**捕获：黄金集里有 13 题规则规划器本来就答不出
            # （见 README 的 17/30 基线），那些题抛异常是正常的。
            # 要比的是"两边表现是否一致"——包括抛不抛、抛哪种。
            ea = eb = None
            try:
                a = pr.resolve(q["question"], ctx)
            except Exception as e:  # noqa: BLE001
                ea, a = e, None
            try:
                b = pg.resolve(q["question"], ctx)
            except Exception as e:  # noqa: BLE001
                eb, b = e, None
            if ea is not None or eb is not None:
                if type(ea) is not type(eb) or str(ea) != str(eb):
                    raised.append(
                        f"{q['id']}: ref={type(ea).__name__ if ea else 'ok'}"
                        f"/got={type(eb).__name__ if eb else 'ok'}"
                    )
                continue
            if a != b:
                plan_bad.append(q["id"])
                continue
            sa = compiler.compile(a, ref).sql
            sb = compiler.compile(b, got).sql
            if sa != sb:
                sql_bad.append(q["id"])
            cur.execute(sa)
            ra = cur.fetchall()
            cur.execute(sb)
            rb = cur.fetchall()
            if ra != rb:
                row_bad.append(q["id"])
    finally:
        conn.close()

    check(not raised, "★ 答不出的题两边**抛同样的错**（异常也一致）",
          str(raised[:6]))
    check(not plan_bad, "★ 30 题逻辑计划逐题相同", str(plan_bad[:6]))
    check(not sql_bad, "★ 30 题编译出的 SQL **文本**逐题相同", str(sql_bad[:6]))
    check(not row_bad, "★ 30 题执行结果逐题相同", str(row_bad[:6]))
    check(len(GOLDEN_SET) == 30, "确实跑了 30 题", str(len(GOLDEN_SET)))

    print("\n  加载报告：")
    for line in report.lines():
        print("    " + line)


# --------------------------------------------------------------------- C
def test_integration(work: str) -> None:
    section("[C] 集成：配置指向 yml 后整条链路仍然通")

    from nl2sql import NL2SQL, NL2SQLConfig

    db = os.path.join(work, "ecommerce.db")
    shutil.copy2(GOLDEN_DB, db)

    cfg = NL2SQLConfig(state_dir=os.path.join(work, "state"))
    cfg.add_sqlite(db, source_id="shop", semantic_path=GOLDEN_YML)
    cfg.default_source = "shop"
    cfg.agent.planner = "rule"

    with NL2SQL(cfg) as e:
        e.refresh_snapshot()
        check(e.semantic_model.base_table == "fact_order",
              "引擎装上了 yml 语义模型", e.semantic_model.base_table)
        check(len(e.semantic_model.metrics) == len(build_model().metrics),
              "指标数量与参考模型一致",
              str(len(e.semantic_model.metrics)))

        out = e.ask("2024年6月，数码品类的实收金额是多少？", now=NOW)
        check(out.ok and out.status == "ok",
              "★ 走 yml 语义模型能答对问题",
              f"{out.status} {out.message}")
        check(out.result is not None and out.result.rows,
              "拿到结果行", str(out.result.rows[:2] if out.result else None))
        check(out.sql is not None and "SUM(CASE WHEN" in (out.sql or ""),
              "SQL 口径来自 yml（条件聚合）", (out.sql or "")[:120])

        # 窗口指标（结构扩展）也能跑
        out2 = e.ask("各渠道的实收金额占比是多少？", now=NOW)
        check(out2.ok and out2.status == "ok",
              "★ 用到窗口指标（dbt 无等价物，走结构扩展）的问题也能答",
              f"{out2.status} {out2.message}")
        rows = out2.result.rows if out2.result else []
        if rows:
            total = sum(float(r[-1]) for r in rows if r[-1] is not None)
            check(abs(total - 1.0) < 0.01,
                  "占比合计≈1（口径正确）", str(round(total, 4)))


def test_http_surface(work: str) -> None:
    section("[D] HTTP：壳层注入 yml 后 /api/info 如实报告语义层来源")

    from fastapi.testclient import TestClient

    from apps.web.backend.main import create_app

    db = os.path.join(work, "ecommerce.db")
    shutil.copy2(GOLDEN_DB, db)

    os.environ["NL2SQL_DB"] = db
    os.environ["NL2SQL_STATE_DIR"] = os.path.join(work, "http_state")
    os.environ["NL2SQL_SOURCE_ID"] = "shop"
    os.environ["NL2SQL_CONFIG"] = os.path.join(work, "http.toml")
    os.environ["NL2SQL_PLANNER"] = "rule"
    os.environ["NL2SQL_METRICS"] = GOLDEN_YML
    os.environ.pop("NL2SQL_LLM_API_KEY", None)

    with TestClient(create_app()) as client:
        r = client.get("/api/info")
        check(r.status_code == 200, "GET /api/info 200", str(r.status_code))
        d = r.json()
        sem = d.get("semantic") or {}
        check(sem.get("source") == "dbt metrics.yml",
              "★ 界面能看出语义层来自 dbt 指标文件", str(sem.get("source")))
        check(sem.get("metrics") == len(build_model().metrics)
              and sem.get("dimensions") == len(build_model().dimensions),
              "报出指标/维度数量（与参考模型一致）",
              f"{sem.get('metrics')}/{sem.get('dimensions')}")
        check(sem.get("base_table") == "fact_order",
              "报出事实表", str(sem.get("base_table")))
        check(0 < float(sem.get("portability", 0)) < 1,
              "★ 报出**口径可移植性**（不是自吹 100%）",
              str(sem.get("portability")))
        ref = build_model()
        want = {n for n, mt in ref.metrics.items() if mt.window}
        check(want <= set(sem.get("structural_metrics") or []),
              "★ 明确指出哪些指标 dbt 里没有等价物",
              str(sem.get("structural_metrics")))

        # 指到不存在的文件：引擎装配失败，但**接口要能说清原因**。
        # 早先 /api/info 直接 503「核心引擎未就绪」，用户无从知道是哪个文件写错。
        os.environ["NL2SQL_METRICS"] = os.path.join(work, "nope.yml")
        os.environ["NL2SQL_STATE_DIR"] = os.path.join(work, "http_state2")
        with TestClient(create_app()) as c2:
            r2 = c2.get("/api/info")
        check(r2.status_code == 200,
              "★ 引擎装配失败时 /api/info 仍返回 200（能报原因）",
              str(r2.status_code))
        body = r2.json() if r2.status_code == 200 else {}
        check(body.get("engine_ready") is False, "标出 engine_ready=false",
              str(body.get("engine_ready")))
        warn = " ".join(body.get("startup_warnings") or [])
        check("不存在" in warn or "装配失败" in warn,
              "★ 启动警告里带上失败原因（照着改就行）", warn[:200])


def main() -> int:
    base = os.environ.get("NL2SQL_TEST_TMP") or os.path.join(ROOT, "data",
                                                             "_test_s3b")
    work = os.path.join(base, "run")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    print(f"工作目录: {work}")

    try:
        test_loader(work)
        test_equivalence()
        test_integration(work)
        test_http_surface(work)
    except Exception:
        traceback.print_exc()
        _failures.append("未捕获异常")

    shutil.rmtree(work, ignore_errors=True)
    print("\n" + "=" * 66)
    if _failures:
        print(f"S3b 验收未通过：{len(_failures)} 项失败")
        for x in _failures[:25]:
            print("  -", x)
        return 1
    print("S3b 验收全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
