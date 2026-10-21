"""S3 验收桥接：把黄金集 30 题写成 **手写逻辑计划**。

目的
----
验证执行链路（逻辑计划 → 编译 → 守门 → 执行 → 结果）**先于 LLM 可用**。
之后模型接进来只是替换"计划来源"，链路本身已经可信。

写法约定
--------
* 能用品语义表达的题 → 用 `measures` / `dimensions` / `filters`（真实路径）
* M3 之后 30 题**全部**走真实语义计划：跨期对比（pivot）、HAVING、累计/
  占比/排名（窗口指标）、标量比较（compare）都进了契约。`raw_select` 逃生
  通道仍然保留（且仍走 guard / cache / 结果比对），但黄金集里已无使用者。
* 过滤用 `filters`，时间范围用 `time_range`（支持相对锚定）

每题的 `expected_id` 对应 `golden_set/baseline.json` 中的题号。
"""
from __future__ import annotations

from ..contracts.semantic import (
    Filter,
    Having,
    LogicalPlan,
    OrderBy,
    Pivot,
    PivotBucket,
    PlanDimension,
    PlanMeasure,
    ScalarCompare,
    TimeRange,
)

SOURCE_ID = "shop"
SEM = "golden-v1"
H1 = ("2024-01-01", "2024-06-30")     # 上半年
JUN = ("2024-06-01", "2024-06-30")    # 本月
MAY = ("2024-05-01", "2024-05-31")    # 上月


def m(metric: str, alias: str | None = None) -> PlanMeasure:
    return PlanMeasure(metric=metric, alias=alias)


def d(dim: str, alias: str | None = None) -> PlanDimension:
    return PlanDimension(dimension=dim, alias=alias)


def o(field: str, desc: bool = True) -> OrderBy:
    return OrderBy(field=field, desc=desc)


def f(field: str, op: str, value: object) -> Filter:
    return Filter(field=field, op=op, value=value)


def half_year(grain: str | None = None) -> TimeRange:
    return TimeRange(field="order_date", start=H1[0], end=H1[1], grain=grain)


def june() -> TimeRange:
    return TimeRange(field="order_date", start=JUN[0], end=JUN[1])


def having(field: str, op: str, value: object) -> Having:
    """分组后的阈值过滤（HAVING）。"""
    return Having(field=field, op=op, value=value)


def qualify(field: str, op: str, value: object) -> Having:
    """窗口层之后的过滤（"每组前 N"要靠它，因为排名算完才知道谁是前 N）。"""
    return Having(field=field, op=op, value=value)


def bucket(label: str, conditions: list[Filter]) -> PivotBucket:
    return PivotBucket(label=label, conditions=tuple(conditions))


def pivot_plan(*, buckets: list[PivotBucket], measure: PlanMeasure,
               dimensions=(), filters=(), time_range=None,
               order_by=(), notes=()) -> LogicalPlan:
    """透视计划：把桶的取值转成列（跨期对比 / 分类转列共用一套机制）。"""
    return LogicalPlan(
        source_id=SOURCE_ID, sem_version=SEM,
        dimensions=tuple(dimensions), filters=tuple(filters),
        time_range=time_range, order_by=tuple(order_by),
        pivot=Pivot(buckets=tuple(buckets), measure=measure),
        notes=tuple(notes),
    )


def raw(sql: str, *, notes: tuple[str, ...] = ()) -> LogicalPlan:
    """逃生通道：整条 SELECT 由外部提供，但仍在链路内走守门与缓存。"""
    return LogicalPlan(
        source_id=SOURCE_ID, sem_version=SEM, raw_select=sql,
        notes=notes or ("该查询使用 raw_select（语义层未内建的复杂形态）",),
    )


def plan(*, measures=(), dimensions=(), filters=(), time_range=None,
         order_by=(), limit=None, having_=(), qualify_=(),
         notes=()) -> LogicalPlan:
    return LogicalPlan(
        source_id=SOURCE_ID, sem_version=SEM,
        measures=tuple(measures), dimensions=tuple(dimensions),
        filters=tuple(filters), time_range=time_range,
        order_by=tuple(order_by), limit=limit,
        having=tuple(having_), qualify=tuple(qualify_),
        notes=tuple(notes),
    )


# ===================================================================== 计划表
PLANS: dict[str, LogicalPlan] = {
    # ---------------- A. 基础过滤与聚合 ----------------
    "A1": plan(
        measures=[m("net_revenue", "实收金额")],
        filters=[f("category", "=", "数码")],
        time_range=june(),
    ),
    "A2": plan(
        measures=[m("paid_order_count", "有效订单数"), m("net_revenue", "实收金额")],
        filters=[f("region", "=", "华东")],
        time_range=june(),
    ),
    "A3": plan(
        measures=[m("paid_order_count", "订单数"), m("max_order_amount", "最高成交金额")],
        filters=[f("amount", ">", 5000)],
        time_range=june(),
    ),
    "A4": plan(
        measures=[m("customer_count", "客户数"), m("paid_order_count", "有效订单数")],
        time_range=half_year(),
    ),
    "A5": plan(
        measures=[m("all_order_count", "订单数")],
        dimensions=[d("status", "订单状态")],
        filters=[f("status", "in", ["已退款", "已取消"])],
        time_range=half_year(),
        order_by=[o("订单数"), o("订单状态", desc=False)],
    ),

    # ---------------- B. 分组聚合 ----------------
    "B1": plan(
        measures=[m("net_revenue", "实收金额"), m("paid_order_count", "有效订单数")],
        dimensions=[d("channel", "渠道")],
        time_range=half_year(),
        order_by=[o("实收金额"), o("渠道", desc=False)],
    ),
    "B2": plan(
        measures=[m("net_revenue", "实收金额"), m("quantity_total", "销量")],
        dimensions=[d("category", "品类")],
        time_range=half_year(),
        order_by=[o("实收金额"), o("品类", desc=False)],
    ),
    "B3": plan(
        measures=[m("net_revenue", "实收金额")],
        dimensions=[d("brand", "品牌")],
        time_range=half_year(),
        order_by=[o("实收金额"), o("品牌", desc=False)],
    ),
    "B4": plan(
        measures=[m("customer_unit_price", "客单价")],
        dimensions=[d("channel", "渠道")],
        time_range=half_year(),
        order_by=[o("客单价"), o("渠道", desc=False)],
    ),
    "B5": plan(
        # 聚合后阈值过滤 → HAVING（不再需要 raw_select）
        measures=[m("net_revenue", "实收金额")],
        dimensions=[d("category", "品类")],
        time_range=half_year(),
        having_=[having("net_revenue", ">", 20000)],
        order_by=[o("实收金额"), o("品类", desc=False)],
        notes=("★ HAVING：对分组后的聚合值做阈值过滤（语义层已内建）",),
    ),
    "B6": plan(
        measures=[m("refund_rate", "退款率")],
        dimensions=[d("region", "地区")],
        time_range=half_year(),
        order_by=[o("退款率"), o("地区", desc=False)],
    ),

    # ---------------- C. 时间趋势 ----------------
    "C1": plan(
        measures=[m("paid_order_count", "有效订单数"), m("net_revenue", "实收金额")],
        dimensions=[d("month", "月份")],
        time_range=half_year("month"),
        order_by=[o("月份", desc=False)],
    ),
    "C2": plan(
        measures=[m("paid_order_count", "有效订单数")],
        dimensions=[d("month", "月份")],
        time_range=half_year("month"),
        order_by=[o("月份", desc=False)],
    ),
    # M3 之后这里不再用 raw_select：计划只说"按周"，截断表达式由
    # 编译器按数据源方言展开（sqlite: strftime('%Y-%W') / mysql:
    # DATE_FORMAT('%Y-%u')）。同一个计划因此在两个引擎上都能跑。
    "C3": LogicalPlan(
        source_id=SOURCE_ID, sem_version=SEM,
        measures=(m("paid_order_count", "有效订单数"),),
        dimensions=(d("week", "年周"),),
        time_range=half_year("week"),
        order_by=(o("年周", desc=False),),
        notes=("时间粒度是语义层的 grain，计划里不出现方言函数",),
    ),
    "C4": pivot_plan(
        # 跨期对比 → 透视（桶 = 两个时间段）。
        # 两个时间窗是**问题给的**，不是语义模型给的，所以做成计划的一部分
        # 而不是两个指标——否则每个可能的窗口组合都要在模型里写一遍。
        buckets=[
            bucket("本月实收金额", [f("order_date", ">=", JUN[0]),
                                f("order_date", "<=", JUN[1])]),
            bucket("上月实收金额", [f("order_date", ">=", MAY[0]),
                                f("order_date", "<=", MAY[1])]),
        ],
        measure=m("net_revenue"),
        notes=("★ 透视：把两个时间桶转成两列（语义层已内建）",),
    ),
    "C5": pivot_plan(
        # 分类转列 → 用同一个透视机制（桶 = 维度取值）
        buckets=[
            bucket("线上实收金额", [f("channel_type", "=", "线上")]),
            bucket("线下实收金额", [f("channel_type", "=", "线下")]),
        ],
        measure=m("net_revenue"),
        dimensions=[d("month", "月份")],
        time_range=half_year(),
        order_by=[o("月份", desc=False)],
        notes=("★ 透视：时间 × 渠道类型交叉转列（与跨期对比同一机制）",),
    ),
    "C6": plan(
        # 按天分桶 + HAVING：两件事都在语义层内了
        measures=[m("net_revenue", "实收金额")],
        dimensions=[d("day", "日期")],
        time_range=june(),
        having_=[having("net_revenue", ">", 3000)],
        order_by=[o("日期", desc=False)],
        notes=("★ 按天分桶 + HAVING",),
    ),

    # ---------------- D. 多表关联 ----------------
    "D1": plan(
        measures=[m("net_revenue", "实收金额")],
        dimensions=[d("customer_name", "客户姓名"), d("region", "地区")],
        time_range=half_year(),
        order_by=[o("实收金额"), o("客户姓名", desc=False)],
        limit=10,
    ),
    "D2": plan(
        measures=[m("customer_count", "客户数"), m("net_revenue", "实收金额")],
        dimensions=[d("customer_level", "客户等级")],
        time_range=half_year(),
        order_by=[o("实收金额"), o("客户等级", desc=False)],
    ),
    "D3": plan(
        measures=[m("customer_count", "客户数"), m("net_revenue", "实收金额")],
        dimensions=[d("customer_type", "客户类型")],
        time_range=half_year(),
        order_by=[o("实收金额"), o("客户类型", desc=False)],
    ),
    "D4": plan(
        measures=[m("quantity_total", "销量")],
        dimensions=[d("product_name", "商品名称"), d("category", "品类")],
        time_range=half_year(),
        order_by=[o("销量"), o("商品名称", desc=False)],
        limit=5,
    ),
    "D5": plan(
        measures=[m("customer_count", "消费客户数"), m("net_revenue", "实收金额")],
        dimensions=[d("region", "地区")],
        time_range=half_year(),
        order_by=[o("实收金额"), o("地区", desc=False)],
    ),
    "D6": plan(
        measures=[m("quantity_total", "销量"), m("net_revenue", "实收金额")],
        dimensions=[d("channel", "渠道")],
        time_range=june(),
        order_by=[o("实收金额"), o("渠道", desc=False)],
    ),
    "D7": plan(
        measures=[m("net_revenue", "实收金额"), m("cost_total", "成本"),
                  m("gross_margin", "毛利率")],
        dimensions=[d("category", "品类")],
        time_range=half_year(),
        order_by=[o("毛利率"), o("品类", desc=False)],
        notes=("毛利率为派生指标，由 net_revenue 与 cost_total 组合（指标引用指标）",),
    ),

    # ---------------- E. 窗口与排名 ----------------
    "E1": plan(
        measures=[m("net_revenue", "实收金额"), m("revenue_rank", "排名")],
        dimensions=[d("channel", "渠道")],
        time_range=half_year(),
        order_by=[o("排名", desc=False), o("渠道", desc=False)],
        notes=("revenue_rank 是窗口指标（RANK OVER），由语义层定义为窗口表达式",),
    ),
    "E2": plan(
        # 分组内 TopN：内层聚合 → 中层 PARTITION BY 排名 → 外层筛名次。
        # **必须是 qualify 而不是 having**：排名要在窗口层算出来，
        # HAVING 比窗口层早，那时候"名次"还不存在。
        measures=[m("quantity_total", "销量"),
                  m("product_rank_in_category", "名次")],
        dimensions=[d("category", "品类"), d("product_name", "商品名称")],
        time_range=half_year(),
        qualify_=[qualify("名次", "<=", 2)],
        order_by=[o("品类", desc=False), o("名次", desc=False)],
        notes=("★ 分组内 TopN：PARTITION BY 排名 + qualify（语义层已内建）",),
    ),
    "E3": plan(
        # 累计：窗口指标自带 `OVER (ORDER BY month)`，month 必须是分组维度
        measures=[m("net_revenue", "当月实收金额"),
                  m("cumulative_net_revenue", "累计实收金额")],
        dimensions=[d("month", "月份")],
        time_range=half_year(),
        order_by=[o("月份", desc=False)],
        notes=("★ 累计求和：窗口函数按分组维度滚动（语义层已内建）",),
    ),
    "E4": plan(
        # 分区排名：PARTITION BY 地区
        measures=[m("net_revenue", "实收金额"),
                  m("customer_rank_in_region", "地区内排名")],
        dimensions=[d("customer_name", "客户姓名"), d("region", "地区")],
        time_range=half_year(),
        order_by=[o("地区", desc=False), o("地区内排名", desc=False),
                  o("客户姓名", desc=False)],
        notes=("★ 分区排名：RANK() OVER (PARTITION BY 地区)（语义层已内建）",),
    ),

    # ---------------- F. 子查询与 CTE ----------------
    # M3 之后这里也不再走 raw_select：与"另一个聚合结果"比较被建模成
    # `ScalarCompare(measure, against, op, factor)`，编译器展开成
    # "聚合 CTE + 外层 WHERE 聚合值 > (SELECT AVG(...) FROM agg)"。
    "F1": LogicalPlan(
        source_id=SOURCE_ID, sem_version=SEM,
        measures=(m("net_revenue", "实收金额"),),
        dimensions=(d("customer_name", "客户姓名"),),
        time_range=half_year(),
        compare=ScalarCompare(measure="net_revenue", against="all", op=">"),
        notes=("★ 标量比较：与「整表平均」比，不是常量（HAVING）也不是窗口值"
               "（qualify）",),
    ),
    "F2": plan(
        measures=[m("net_revenue", "实收金额"), m("revenue_share", "占比")],
        dimensions=[d("channel", "渠道")],
        time_range=half_year(),
        order_by=[o("实收金额"), o("渠道", desc=False)],
        notes=("占比用窗口总计 SUM(...) OVER () 实现",),
    ),
}


def summary() -> dict[str, int]:
    semantic = sum(1 for p in PLANS.values() if p.raw_select is None)
    return {"total": len(PLANS), "semantic": semantic,
            "raw_select": len(PLANS) - semantic}


if __name__ == "__main__":
    import io
    import sys

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    print(summary())
    for k, v in PLANS.items():
        kind = "raw " if v.raw_select else "sem "
        print(f"  {k}  {kind} measures={[x.metric for x in v.measures]}")
