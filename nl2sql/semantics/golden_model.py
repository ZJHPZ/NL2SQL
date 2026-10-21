"""黄金集（电商数仓）的语义模型。

这份文件就是「**精简指标文件**」的具体形态——对应 dbt `metrics.yml` 的字段语义，
由开发者手工维护。它是 S3 验收（用逻辑计划跑通 30 题）的输入。

设计要点
--------
1. **两个粒度用同一个模型表达**：明细粒度（order 行级）和订单粒度（order 级）。
   后者通过 `logical_source`（派生表）实现，因此指标与维度定义不必写两套。
   这是让语义层保持精简、不被问句类型撑爆的关键手段。
2. **口径显式**：`amount` 字段是成交金额（未减优惠），"实收金额"是减去
   `discount_amount` 并只算已支付订单。口径差异正是语义层存在的理由。
3. **`{{ dimension('x') }}`**：指标表达式里引用维度，避免把字段名硬编码两遍。
"""
from __future__ import annotations

from ..contracts.semantic import (
    DimensionDefinition,
    JoinSpec,
    MetricDefinition,
    SemanticModel,
)

SOURCE_ID = "shop"
SEM_VERSION = "golden-v1"

# --------------------------------------------------------------------- 公共片段
# 订单粒度视图：把订单级问题需要的商品属性固化进来
ORDER_VIEW = """(
    SELECT f.order_id, f.order_date, f.customer_id, f.channel_id,
           f.quantity, f.unit_price, f.discount_amount, f.order_status,
           f.amount, p.category, p.brand, p.unit_cost, p.product_name
    FROM fact_order f
    JOIN dim_product p ON f.product_id = p.product_id
)"""

# 只算"已支付"的实收金额。未支付订单不计入收入。
PAID_NET = (
    "SUM(CASE WHEN {{ dimension('order_status') }} = '已支付'"
    " THEN {{ dimension('amount') }} - {{ dimension('discount_amount') }} END)"
)

JOINS = {
    # `table` 直接写成"表名 别名"：编译器把它原样拼进 JOIN 子句，
    # 别名由语义模型自己声明，避免在条件字符串和表名之间出现不一致。
    "customer": JoinSpec(
        name="customer", table="dim_customer c",
        condition="f.customer_id = c.customer_id", kind="left",
    ),
    "channel": JoinSpec(
        name="channel", table="dim_channel ch",
        condition="f.channel_id = ch.channel_id", kind="left",
    ),
}

TABLE_JOINS = {
    "dim_customer": "customer",
    "dim_channel": "channel",
}


# --------------------------------------------------------------------- 构建
def build_model(*, order_level: bool = True, version: str = SEM_VERSION) -> SemanticModel:
    """构建语义模型。

    Args:
        order_level: True 使用订单粒度视图（含商品属性），
            False 使用明细粒度（fact_order 原始行）。
    """
    dims: dict[str, DimensionDefinition] = {
        # ---- 时间 ----
        "order_date": DimensionDefinition(
            name="order_date", expr="f.order_date", description="下单日期",
            synonyms=["日期", "下单时间"],
        ),
        "month": DimensionDefinition(
            name="month", expr="substr(f.order_date, 1, 7)",
            description="月份（YYYY-MM）", synonyms=["月份", "按月", "每月"],
        ),
        "day": DimensionDefinition(
            name="day", expr="substr(f.order_date, 1, 10)",
            description="日期（YYYY-MM-DD）", synonyms=["按天", "每日", "每天"],
        ),
        "status": DimensionDefinition(
            name="status", expr="f.order_status", description="订单状态",
            synonyms=["订单状态", "状态"],
        ),
        # ---- 客户（需 JOIN dim_customer）----
        "region": DimensionDefinition(
            name="region", expr="c.region", table="dim_customer",
            description="客户所属地区", synonyms=["地区", "区域", "大区"],
        ),
        "customer_name": DimensionDefinition(
            name="customer_name", expr="c.customer_name", table="dim_customer",
            description="客户姓名", synonyms=["客户姓名", "客户名"],
        ),
        "customer_level": DimensionDefinition(
            name="customer_level", expr="c.customer_level", table="dim_customer",
            description="客户等级", synonyms=["客户等级", "会员等级", "等级"],
        ),
        "customer_type": DimensionDefinition(
            name="customer_type",
            expr="CASE WHEN c.is_new_customer = 1 THEN '新客户' ELSE '老客户' END",
            table="dim_customer",
            description="新老客", synonyms=["新客户", "老客户", "新客", "老客"],
        ),
        # ---- 渠道（需 JOIN dim_channel）----
        "channel": DimensionDefinition(
            name="channel", expr="ch.channel_name", table="dim_channel",
            description="销售渠道", synonyms=["渠道", "平台"],
        ),
        "channel_type": DimensionDefinition(
            name="channel_type", expr="ch.channel_type", table="dim_channel",
            description="线上/线下", synonyms=["线上渠道", "线下渠道"],
        ),
        # ---- 过滤用：指标表达式的可引用维度（不出现在输出）----
        "order_status": DimensionDefinition(
            name="order_status", expr="f.order_status",
            description="订单状态（过滤用）", synonyms=["订单状态"],
        ),
        "amount": DimensionDefinition(
            name="amount", expr="f.amount",
            description="成交金额（未减优惠）", synonyms=["成交金额"],
        ),
        "discount_amount": DimensionDefinition(
            name="discount_amount", expr="f.discount_amount", description="优惠金额",
        ),
    }

    if order_level:
        dims.update({
            "category": DimensionDefinition(
                name="category", expr="f.category", table="dim_product",
                description="商品品类", synonyms=["品类", "商品品类", "类目", "分类"],
            ),
            "brand": DimensionDefinition(
                name="brand", expr="f.brand", table="dim_product",
                description="商品品牌", synonyms=["品牌", "商品品牌"],
            ),
            "product_name": DimensionDefinition(
                name="product_name", expr="f.product_name", table="dim_product",
                description="商品名称", synonyms=["商品名称", "商品名", "货品"],
            ),
        })

    metrics: dict[str, MetricDefinition] = {
        "net_revenue": MetricDefinition(
            name="net_revenue", label="实收金额", expr=PAID_NET,
            description="已支付订单的成交金额减去优惠金额",
            synonyms=["销售额", "收入", "营收", "实收", "成交额", "实收额"],
            decimals=2,
        ),
        "gmv": MetricDefinition(
            name="gmv", label="GMV",
            expr=("SUM(CASE WHEN {{ dimension('order_status') }} <> '已取消'"
                  " THEN {{ dimension('amount') }} END)"),
            description="成交金额，含优惠、不剔除退款",
            synonyms=["成交总额", "gmv"],
        ),
        "paid_order_count": MetricDefinition(
            name="paid_order_count", label="有效订单数",
            expr=("SUM(CASE WHEN {{ dimension('order_status') }} = '已支付'"
                  " THEN 1 ELSE 0 END)"),
            description="已支付订单笔数",
            synonyms=["有效订单", "订单量", "订单数", "单量", "订单笔数", "多少笔"],
        ),
        "all_order_count": MetricDefinition(
            name="all_order_count", label="全部订单数",
            expr="COUNT(*)", description="含退款与取消的全部订单笔数",
            synonyms=["全部订单", "总订单", "订单总数"],
        ),
        "refunded_order_count": MetricDefinition(
            name="refunded_order_count", label="退款订单数",
            expr=("SUM(CASE WHEN {{ dimension('order_status') }} = '已退款'"
                  " THEN 1 ELSE 0 END)"),
            description="已退款订单笔数",
            synonyms=["退款订单", "退款单数", "已退款订单数"],
        ),
        "customer_count": MetricDefinition(
            name="customer_count", label="客户数",
            expr="COUNT(DISTINCT {{ dimension('customer_id') }})",
            description="去重客户数",
            synonyms=["客户数", "消费客户数", "买家数", "人数", "多少人", "客户有多少"],
        ),
        "quantity_total": MetricDefinition(
            name="quantity_total", label="销量",
            expr=("SUM(CASE WHEN {{ dimension('order_status') }} = '已支付'"
                  " THEN {{ dimension('quantity') }} ELSE 0 END)"),
            description="已支付订单的商品件数",
            synonyms=["销量", "件数", "售出数量", "卖出多少"],
        ),
        "cost_total": MetricDefinition(
            name="cost_total", label="商品成本",
            expr=("SUM(CASE WHEN {{ dimension('order_status') }} = '已支付'"
                  " THEN {{ dimension('quantity') }} * {{ dimension('unit_cost') }}"
                  " ELSE 0 END)"),
            description="已支付订单的商品成本合计",
            synonyms=["成本", "商品成本", "成本合计"],
        ),
        "gross_margin": MetricDefinition(
            name="gross_margin", label="毛利率",
            expr="({{ metric('net_revenue') }} - {{ metric('cost_total') }})"
                 " * 1.0 / NULLIF({{ metric('net_revenue') }}, 0)",
            description="(实收金额 - 商品成本) / 实收金额",
            decimals=4, ratio=True,
            synonyms=["毛利率", "利润率"],
        ),
        "customer_unit_price": MetricDefinition(
            name="customer_unit_price", label="客单价",
            expr="{{ metric('net_revenue') }} * 1.0"
                 " / NULLIF({{ metric('customer_count') }}, 0)",
            description="实收金额 / 去重客户数",
            decimals=2,
            synonyms=["客单价", "人均消费", "人均金额"],
        ),
        "refund_rate": MetricDefinition(
            name="refund_rate", label="退款率",
            expr="{{ metric('refunded_order_count') }} * 1.0"
                 " / NULLIF({{ metric('all_order_count') }}, 0)",
            description="已退款订单数 / 全部订单数",
            decimals=4, ratio=True,
            synonyms=["退款率", "退货率"],
        ),
        "revenue_share": MetricDefinition(
            name="revenue_share", label="占比",
            expr="{{ metric('net_revenue') }} * 1.0"
                 " / NULLIF(SUM({{ metric('net_revenue') }}) OVER (), 0)",
            description="该分组的实收金额占全量比例",
            window=True, decimals=4, ratio=True,
            # 同义词覆盖各类"占…比例"的问法（零售域最常见口径之一）
            synonyms=["占比", "比重", "份额", "占全渠道", "占总量", "占全部"],
        ),
        "revenue_rank": MetricDefinition(
            name="revenue_rank", label="排名",
            expr="RANK() OVER (ORDER BY {{ metric('net_revenue') }} DESC)",
            description="按实收金额的降序排名（窗口指标）",
            window=True,
            synonyms=["排名", "排行"],
        ),
        # ---- 累计：按分组的时间维度做滚动求和 --------------------------
        # `OVER (ORDER BY {{ dimension('month') }})` 里的 month 是**分组维度**，
        # 编译器会把它解析成内层 CTE 的列名。所以用这个指标时 plan.dimensions
        # 必须包含 month——编译器会检查并给出可行动的报错，而不是生成查不动的 SQL。
        "cumulative_net_revenue": MetricDefinition(
            name="cumulative_net_revenue", label="累计实收金额",
            expr=("SUM({{ metric('net_revenue') }})"
                  " OVER (ORDER BY {{ dimension('month') }}"
                  " ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"),
            description="实收金额按月累计（含当月）",
            window=True, decimals=2,
            synonyms=["累计实收金额", "累计实收", "累计金额", "累计值"],
        ),
        # ---- 分组内排名：PARTITION BY 分组维度 -------------------------
        # 与上面的 revenue_rank 的区别：那个是全局排名（"各渠道按实收金额排名"），
        # 这个是**组内**排名（"每个地区内客户的排名"）。
        "customer_rank_in_region": MetricDefinition(
            name="customer_rank_in_region", label="地区内排名",
            expr=("RANK() OVER (PARTITION BY {{ dimension('region') }}"
                  " ORDER BY {{ metric('net_revenue') }} DESC)"),
            description="客户在自己所属地区内按实收金额的排名",
            window=True,
            synonyms=["地区内排名", "地区内名次", "区域内排名"],
        ),
        # 分组内 TopN 的排序依据。层级数量（前2/前3）由计划层的 qualify 决定，
        # 不写进指标——否则每个 N 都要新增一个指标。
        "product_rank_in_category": MetricDefinition(
            name="product_rank_in_category", label="品类内名次",
            expr=("ROW_NUMBER() OVER (PARTITION BY {{ dimension('category') }}"
                  " ORDER BY {{ metric('quantity_total') }} DESC,"
                  " {{ dimension('product_name') }})"),
            description="商品在自己所属品类内按销量的名次（并列按商品名稳定）",
            window=True,
            synonyms=["品类内名次", "品类内排名", "商品排名"],
        ),
        "max_order_amount": MetricDefinition(
            name="max_order_amount", label="最高成交金额",
            expr=("MAX(CASE WHEN {{ dimension('order_status') }} = '已支付'"
                  " THEN {{ dimension('amount') }} END)"),
            description="已支付订单中的最大成交金额",
            synonyms=["最高成交金额", "最高金额", "最高一笔", "最大金额"],
        ),
    }

    # 这些维度是指标表达式的组成部分，不应出现在 GROUP BY
    internal_dims = {
        "order_status", "amount", "discount_amount",
        "quantity", "unit_cost", "customer_id",
    }
    # 参考维度（未被指标引用时仍需存在于模型中，用于过滤/分组）
    extra_dims = {
        "quantity": DimensionDefinition(name="quantity", expr="f.quantity"),
        "unit_cost": DimensionDefinition(
            name="unit_cost", expr="f.unit_cost", table="dim_product",
        ),
        "customer_id": DimensionDefinition(name="customer_id", expr="f.customer_id"),
    }
    for k, v in extra_dims.items():
        dims[k] = v
    model = SemanticModel(
        source_id=SOURCE_ID,
        version=version,
        base_table="fact_order",
        base_alias="f",
        metrics=metrics,
        dimensions=dims,
        joins=JOINS,
        table_joins=TABLE_JOINS,
        entities={
            "order": "fact_order.order_id",
            "customer": "dim_customer.customer_id",
            "product": "dim_product.product_id",
            "channel": "dim_channel.channel_id",
        },
        synonyms={
            "销售额": "net_revenue", "收入": "net_revenue", "营收": "net_revenue",
            "订单数": "paid_order_count", "单量": "paid_order_count",
            "客单价": "customer_unit_price", "退款率": "refund_rate",
            "毛利率": "gross_margin", "占比": "revenue_share",
            "地区": "region", "区域": "region", "品类": "category",
            "渠道": "channel", "月份": "month", "品牌": "brand",
        },
    )
    if order_level:
        model.logical_source = ORDER_VIEW
        model.logical_tables = ("dim_product",)
        model.logical_join_key = "f.order_id = o.order_id"
    # 标记为内部维度（供编译器/计划构造器判断是否 GROUP BY）
    model.entities["__internal_dimensions__"] = ",".join(sorted(internal_dims))
    return model


def internal_dimension_names(model: SemanticModel) -> set[str]:
    raw = model.entities.get("__internal_dimensions__", "")
    return {n for n in raw.split(",") if n}
