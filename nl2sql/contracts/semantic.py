"""语义层契约：业务概念 → 逻辑计划。

这是整个项目最重要的契约。它决定了：
* 智能体组件是否需要理解方言（不需要）
* 输出是否可复现（同一问题 → 同一指纹 → 同一结果）
* 缓存是否安全（指纹是确定性摘要，不是文本相似度）

设计要点
--------
1. **逻辑计划必须确定性**：字段顺序、过滤顺序归一化，保证指纹稳定。
2. **口径与字段分离**：`amount` 字段 ≠ `实收金额` 口径。口径承载在 measure 定义里。
3. **版本显式**：`sem_version` 参与指纹，业务口径变更必须让缓存失效。
4. 不做 SQL 生成——那是 `dialect/` 的事。语义层只产出"要什么"，不产出"怎么写"。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable


# --------------------------------------------------------------------- 过滤
@dataclass(frozen=True)
class Filter:
    """一条过滤条件。field 为语义名（维度名或指标名）。"""

    field: str
    op: str                      # "=" "!=" ">" ">=" "<" "<=" "in" "between" "like"
    value: Any
    # "and" / "or"，用于多个同组过滤的组合
    combinator: str = "and"


@dataclass(frozen=True)
class TimeRange:
    """时间范围。支持相对表达，由语义层锚定成绝对区间。

    相对表达的锚点必须显式传入，不允许读取系统当前时间——
    否则同一问题在不同日期得到不同结果，评测与缓存都会失效。
    """

    field: str                   # 时间维度名，如 "order_date"
    start: str | None = None     # ISO 日期
    end: str | None = None
    grain: str | None = None     # "day" | "week" | "month" | "quarter" | "year"
    relative: str | None = None  # "this_month" | "last_month" | "ytd" ...
    # 用户是否**明确要求按该粒度分组**（"按月/每月/按天"）。
    # 与 grain 的区别："2024年6月" 的 grain=month 只是筛选桶，
    # 而"按月"才是要求输出月份分组列。混淆这两者会让结果从 1 行变成 6 行。
    explicit_grain: bool = False


@dataclass(frozen=True)
class Having:
    """对**分组结果**的阈值过滤（SQL HAVING 语义）。

    刻意不复用 `Filter`：`Filter` 作用在**行**上（WHERE），`Having` 作用在
    **分组**上（HAVING）。合成一个类型，编译器就无从判断该放哪一边——
    放错了不会报错，只会给出**过滤时机不同**的结果，而且往往看起来合理。

    同一个类型也用于 `LogicalPlan.qualify`（窗口层之后的过滤）：
    形状完全一样，只是作用层不同，没必要造两个近似类型。
    """

    field: str                   # 指标名 / 维度名 / 窗口指标名
    op: str                      # ">" ">=" "<" "<=" "=" "!=" "in" "between"
    value: Any


@dataclass(frozen=True)
class PivotBucket:
    """透视表的一列：一组条件 + 输出列名。

    `conditions` 是**明细行上的条件**（决定每行落在哪一列），
    多个条件之间是 AND。用 `Filter` 而不是裸字符串，是为了让含义
    与 WHERE 过滤完全一致——同一套编译器代码负责拼 SQL。
    """

    label: str
    conditions: tuple[Filter, ...]


@dataclass(frozen=True)
class Pivot:
    """长表 → 宽表：把"桶"的取值转成列。

    一个机制覆盖两类问法（这是它进契约、而不是做成两个特例的理由）：
        * 跨期对比："本月和上月的实收金额分别是多少" → 桶 = 时间段
        * 分类转列："线上和线下的实收金额各是多少"   → 桶 = 渠道类型

    为什么必须分两层：桶条件作用在**明细行**上，指标聚合也作用在明细行上。
    直接写 `SUM(CASE WHEN <桶> THEN SUM(x) END)` 是嵌套聚合，SQL 非法。
    所以内层按 `(分组维度, 桶)` 聚合，外层再把桶转成列。

    `measure` 与 `LogicalPlan.measures` 分开：透视的"值列"只有一个，
    而 `measures` 是"每个指标一列"。混在一起会让"到底输出几列"变得含糊。
    """

    buckets: tuple[PivotBucket, ...]
    measure: PlanMeasure


# --------------------------------------------------------------------- 计划
@dataclass(frozen=True)
class PlanMeasure:
    metric: str                  # 指标名（来自 dbt metrics.yml）
    alias: str | None = None


@dataclass(frozen=True)
class PlanDimension:
    dimension: str
    alias: str | None = None


@dataclass(frozen=True)
class OrderBy:
    field: str
    desc: bool = True


@dataclass(frozen=True)
class LogicalPlan:
    """确定性逻辑计划：只描述"要什么"，不含任何方言信息。

    归一化要求（保证 fingerprint 稳定）：
    * measures / dimensions / filters 参与指纹前必须排序
    * 过滤值统一字符串化
    * 不含时间戳、不含随机量、不含执行信息
    """

    source_id: str
    measures: tuple[PlanMeasure, ...] = ()
    dimensions: tuple[PlanDimension, ...] = ()
    filters: tuple[Filter, ...] = ()
    time_range: TimeRange | None = None
    order_by: tuple[OrderBy, ...] = ()
    limit: int | None = None
    sem_version: str = "v0"
    notes: tuple[str, ...] = ()   # 例如 "未验证口径"，需要透出给用户

    # ---------------------------------------------------------- 聚合之上的能力
    # 这三项把"分组之后还能做什么"从"只能排序取前 N"扩展到
    # "按聚合值筛组 / 在组内排序取前 N / 把某个维度的取值转成列"。
    #
    # 它们都是**计划层的构造**而不是新指标类型：同一份语义模型配合不同的
    # having/qualify/pivot 能回答一族问题，不必为每种问法在模型里写一个指标。
    having: tuple[Having, ...] = ()
    """分组后的阈值过滤（SQL HAVING）。字段可以是指标或窗口指标。"""

    qualify: tuple[Having, ...] = ()
    """**窗口层之后**的过滤（SQL 标准的 QUALIFY 语义）。

    与 `having` 的区别是执行时机：`having` 在窗口函数之前，`qualify` 在之后。
    所以"每组排名前 2"必须用 `qualify`——排名算出来才知道谁是前 2。
    写成 `having` 会得不到结果或报错（窗口函数不能出现在 HAVING 里）。
    """

    pivot: Pivot | None = None
    """把一个维度的取值转成列（跨期对比 / 分类转列）。"""

    # 逃生通道：整条 SELECT 由外部提供（仍走 guard / cache / 结果比对）。
    # 用途：多步归因、与全局平均值的标量子查询比较等语义层不该内建的复杂查询。
    # 有它，语义层不必为了覆盖率而膨胀成一个 SQL 生成器。
    raw_select: str | None = None

    def normalized(self) -> dict[str, Any]:
        """归一化字典，供指纹计算。**字段顺序必须稳定。**"""
        return {
            "source_id": self.source_id,
            "sem_version": self.sem_version,
            "measures": sorted(
                (m.metric, m.alias or "") for m in self.measures
            ),
            "dimensions": sorted(
                (d.dimension, d.alias or "") for d in self.dimensions
            ),
            "filters": sorted(
                (
                    f.field,
                    f.op,
                    f.combinator,
                    repr(f.value) if not isinstance(f.value, (list, tuple))
                    else repr(tuple(str(v) for v in f.value)),
                )
                for f in self.filters
            ),
            "time_range": (
                asdict(self.time_range) if self.time_range else None
            ),
            "order_by": [(o.field, o.desc) for o in self.order_by],
            "limit": self.limit,
            # 聚合之上的三项**必须进指纹**：它们改变结果集，
            # 漏进指纹会让"带阈值"和"不带阈值"的两个问题共用一份缓存。
            "having": sorted(
                (h.field, h.op, repr(h.value)) for h in self.having
            ),
            "qualify": sorted(
                (h.field, h.op, repr(h.value)) for h in self.qualify
            ),
            "pivot": (
                {
                    "measure": (self.pivot.measure.metric,
                                self.pivot.measure.alias or ""),
                    "buckets": sorted(
                        (
                            b.label,
                            tuple(sorted(
                                (c.field, c.op, c.combinator, repr(c.value))
                                for c in b.conditions
                            )),
                        )
                        for b in self.pivot.buckets
                    ),
                }
                if self.pivot else None
            ),
            "raw_select": self.raw_select,
        }

    def fingerprint(self) -> str:
        """逻辑计划指纹 —— 结果缓存的唯一键。

        注意：**不要**把 notes 计入指纹，否则同一计划因提示文案不同而缓存分裂。
        """
        import hashlib
        import json

        payload = json.dumps(
            self.normalized(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------- 定义
@dataclass
class MetricDefinition:
    """指标定义，来源于 dbt metrics.yml（兼容加载，不自创 DSL）。

    `expr` 里可以用 `{{ dimension('name') }}` 引用维度，
    例如 `"SUM(CASE WHEN {{ dimension('order_status') }} = '已支付' THEN amount END)"`。
    """

    name: str
    label: str = ""
    description: str = ""
    expr: str = ""                    # SQL 聚合表达式
    table: str | None = None          # 所属表；None 表示用模型 base_table
    dimensions: list[str] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)
    version: str = "v1"
    owner: str = ""
    effective_date: str | None = None
    synonyms: list[str] = field(default_factory=list)
    # 窗口指标：expr 里含 OVER()，必须在**聚合结果之上**再算。
    # 例如排名 RANK() OVER (ORDER BY SUM(...) DESC)、占比 SUM(x) OVER ()。
    # 这类指标不能与聚合指标放在同一层 SELECT。
    window: bool = False
    # 输出精度：金额 2 位、比率 4 位。**小数位数属于口径的一部分**——
    # "实收金额保留两位"和"占比保留四位"是业务约定，不该由各查询自行决定，
    # 否则同一指标在不同问题里精度不一致，用户会看到 18028.2975 这种噪声。
    decimals: int | None = None
    # 比率类指标（0~1 之间），用于校验与展示格式化
    ratio: bool = False


@dataclass
class DimensionDefinition:
    """维度定义：名称 → SQL 表达式 + 所属表。

    `select` 是**逃生通道**：当维度不是简单列（需要 CTE、窗口函数、多列拼接）时，
    用 `select` 提供一个完整 SELECT 片段，编译时作为 CTE 注入。
    没有它，复杂维度会把整个编译器逼成 SQL 生成器。
    """

    name: str
    expr: str = ""                    # 简单列或表达式，如 "p.category"
    table: str | None = None          # 所属表；用于决定是否 JOIN
    select: str | None = None         # 复杂维度：完整 SELECT（作为 CTE）
    alias: str | None = None          # select 的产出列名，默认与 name 相同
    description: str = ""
    synonyms: list[str] = field(default_factory=list)

    def resolved_expr(self) -> str:
        if self.select:
            return f"d_{self.name}.{self.alias or self.name}"
        return self.expr


@dataclass
class JoinSpec:
    """命名的 join 条件。编译时按维度/指标所属表按需引入，不生成多余 JOIN。"""

    name: str
    table: str                        # 形如 "dim_customer c"（含别名），或 "dim_customer"
    condition: str                    # 例如 "f.customer_id = c.customer_id"
    kind: str = "left"                # left | inner
    aliases: dict[str, str] = field(default_factory=dict)


@dataclass
class SemanticModel:
    """一个数据源的语义模型（指标 + 维度 + join + 同义词）。"""

    source_id: str
    version: str = "v0"
    base_table: str = ""                                       # 事实表
    base_alias: str = "f"
    metrics: dict[str, MetricDefinition] = field(default_factory=dict)
    dimensions: dict[str, DimensionDefinition] = field(default_factory=dict)
    joins: dict[str, JoinSpec] = field(default_factory=dict)   # join 名 → 定义
    entities: dict[str, str] = field(default_factory=dict)     # 业务实体 → 主表.主键
    synonyms: dict[str, str] = field(default_factory=dict)     # 口语 → 语义名
    table_joins: dict[str, str] = field(default_factory=dict)  # 表名 → join 名

    # 逻辑数据源：当问题需要"订单粒度视图"而非明细粒度时，用一段派生表替换
    # `base_table` 作为 FROM。这样指标与维度的定义可以复用，
    # 不必为每个粒度各写一套语义 —— 这是让语义层保持精简的关键。
    logical_source: str | None = None
    logical_tables: tuple[str, ...] = ()     # 已包含在 logical_source 里的表，无需 JOIN
    logical_join_key: str | None = None      # logical_source 与事实表的关联键

    def metric_names(self) -> list[str]:
        return sorted(self.metrics)

    def dimension_names(self) -> list[str]:
        return sorted(self.dimensions)

    def join_for_table(self, table: str | None) -> JoinSpec | None:
        """按表名找 join 定义。

        已在 `logical_tables` 中声明的表不再需要 JOIN（它们已经并入 logical_source）。
        """
        if not table or table == self.base_table or table in self.logical_tables:
            return None
        name = self.table_joins.get(table)
        if not name:
            return None
        return self.joins.get(name)

    def extend_metrics(self, metrics: dict[str, MetricDefinition]) -> None:
        """注册扩展指标（不覆盖已存在的同名定义）。

        两个用途：
        1. 规划器在语义层覆盖不足时，按问题即时定义一个**条件聚合指标**
           （例如"已退款和已取消的订单数"），而不是拼裸 SQL。
        2. 后续"用户自定义指标"的入口——自定义指标最终也要进入语义模型
           才能被编译、被权限校验、被缓存指纹覆盖。

        幂等：同名已存在则不覆盖，保证重复调用不会改变语义。
        """
        for name, metric in metrics.items():
            self.metrics.setdefault(name, metric)

    def from_clause(self) -> tuple[str, str]:
        """返回 `(from_sql, alias)`。"""
        if self.logical_source:
            return self.logical_source, self.base_alias
        return self.base_table, self.base_alias


class SemanticResolutionError(Exception):
    """问题无法映射到语义模型（指标未定义 / 维度不存在）。"""


# --------------------------------------------------------------------- 契约
@runtime_checkable
class SemanticProvider(Protocol):
    """语义层契约。

    实现要求：
    * `resolve()` 必须确定性：同样入参 → 同样 LogicalPlan（含同样指纹）
    * 未覆盖的问题抛 SemanticResolutionError，由上层决定"自由生成并标注"
    * 不生成 SQL
    """

    def load(self, source_id: str) -> SemanticModel:
        """加载语义模型（dbt metrics.yml）。"""
        ...

    def reload(self, source_id: str) -> SemanticModel:
        """重新加载（指标文件变更后调用）。"""
        ...

    def resolve(
        self,
        question: str,
        *,
        source_id: str,
        now: str | None = None,
    ) -> LogicalPlan:
        """问题 → 逻辑计划。`now` 为相对时间的锚点，必须显式传入。"""
        ...
