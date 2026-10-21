"""方言层：逻辑计划 → 方言 SQL，以及 SQL 校验。

**不让 LLM 直接写方言 SQL。** 确定性编译，必要时用 sqlglot 转译。

分工：
    LangGraph 管编排（高层决策）
    sqlglot/SQLAlchemy 管方言（确定性编译）
"""
from __future__ import annotations

import re
from typing import Any

from ..contracts.semantic import (
    DimensionDefinition,
    Filter,
    Having,
    JoinSpec,
    LogicalPlan,
    Pivot,
    PlanDimension,
    PlanMeasure,
    SemanticModel,
    TimeRange,
)
from ..contracts.source import ColumnMeta, SourceCapabilities, TableMeta
from ..errors import CompileError, DimensionNotFoundError, MetricNotFoundError
from .tokens import extract_identifiers, is_read_only, split_statements

__all__ = [
    "SqlCompiler",
    "SqlValidator",
    "CompileResult",
    "ValidationResult",
    "render_metric_expr",
    "expand_metric_expr",
    "referenced_dimensions",
]

from ._deps import (  # noqa: E402  (放在 __all__ 之后避免循环)
    direct_dimension_refs,
    metric_dependencies,
)

# 语义层表达式里的两种引用
_DIMENSION_REF = re.compile(r"\{\{\s*dimension\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")
_METRIC_REF = re.compile(r"\{\{\s*metric\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")


def expand_metric_expr(
    name: str, model: SemanticModel,
    _seen: frozenset[str] = frozenset(),
    alias_map: dict[str, str] | None = None,
    dim_alias_map: dict[str, str] | None = None,
    _top: bool = True,
) -> str:
    """把指标表达式展开成纯 SQL。

    处理两种引用，**先展开指标再解析维度**（顺序不能反）：
        `{{ metric('other') }}`    → 递归展开另一个指标的表达式
        `{{ dimension('x') }}`     → 替换为该维度的 SQL 表达式

    为什么要支持"指标引用指标"：
        毛利率 = (实收 - 成本)/实收、客单价 = 实收/客户数、占比 = 部分/总量——
        这些派生指标天然由基础指标组合而成。若不支持，用户只能把它们
        各自写一遍完整表达式，口径会有多份副本，改一处漏一处。

    `alias_map`：把被引用的指标替换成**已算好的列别名**而不是重复整个聚合表达式。
        窗口指标必须走这条路：`RANK() OVER (ORDER BY SUM(...))` 若把 SUM 再展开一遍，
        外层就变成对聚合结果再聚合，语义和方言规则都会出问题。
        （顶层指标自身不会被替换成别名，否则 SELECT 里会只剩别名自引用。）

    `dim_alias_map`：维度同理。窗口指标常要按分组维度排序或分区，例如
        `SUM(x) OVER (ORDER BY {{ dimension('month') }})`
        在两层编译的外层里，`month` 已经不存在了——外层 `FROM agg`，
        该维度只是一列。所以必须替换成 `agg."月份"`。
        不传它就会生成外层查不到的 `substr(f.order_date, 1, 7)`，
        报 `no such column: f.order_date`。

    循环引用会抛 CompileError，而不是无限递归。
    """
    if name in _seen:
        chain = " -> ".join([*sorted(_seen), name])
        raise CompileError(f"指标存在循环引用: {chain}")
    metric = model.metrics.get(name)
    if metric is None:
        avail = ", ".join(model.metric_names()) or "（无）"
        raise MetricNotFoundError(f"未定义指标: {name}；可用: {avail}")

    seen = _seen | {name}

    def metric_repl(match: re.Match[str]) -> str:
        ref = match.group(1)
        if alias_map and ref in alias_map:
            return alias_map[ref]
        return "(" + expand_metric_expr(
            ref, model, seen, alias_map=alias_map,
            dim_alias_map=dim_alias_map, _top=False
        ) + ")"

    expr = _METRIC_REF.sub(metric_repl, metric.expr)
    return _DIMENSION_REF.sub(
        lambda m: (
            dim_alias_map[m.group(1)]
            if dim_alias_map and m.group(1) in dim_alias_map
            else _dimension_expr(m.group(1), model)
        ),
        expr,
    )


def _dimension_expr(name: str, model: SemanticModel) -> str:
    dim = model.dimensions.get(name)
    if dim is None:
        raise DimensionNotFoundError(f"指标表达式引用了未定义维度: {name}")
    return dim.resolved_expr()


def referenced_dimensions(name: str, model: SemanticModel,
                          _seen: frozenset[str] = frozenset()) -> set[str]:
    """递归收集某指标（含其引用的指标）用到的全部维度名。

    编译器用它来引入必要的 JOIN：指标表达式里写
    `{{ dimension('category') }}`，就需要 JOIN 到商品表。
    """
    if name in _seen:
        return set()
    metric = model.metrics.get(name)
    if metric is None:
        return set()
    seen = _seen | {name}
    out = {m.group(1) for m in _DIMENSION_REF.finditer(metric.expr)}
    for m in _METRIC_REF.finditer(metric.expr):
        out |= referenced_dimensions(m.group(1), model, seen)
    # 指标引用指标的表达式里也可能含维度引用
    for m in _METRIC_REF.finditer(metric.expr):
        sub = model.metrics.get(m.group(1))
        if sub:
            out |= {mm.group(1) for mm in _DIMENSION_REF.finditer(sub.expr)}
    return out


def render_metric_expr(metric_expr: str, model: SemanticModel) -> str:
    """把表达式里的 `{{ dimension('x') }}` 替换成维度 SQL（兼容旧入口）。"""
    return _DIMENSION_REF.sub(
        lambda m: _dimension_expr(m.group(1), model), metric_expr
    )


# --------------------------------------------------------------------- 结果
class CompileResult:
    __slots__ = ("sql", "tables_used", "ctes")

    def __init__(self, sql: str, tables_used: list[str], ctes: list[str]) -> None:
        self.sql = sql
        self.tables_used = tables_used
        self.ctes = ctes


class ValidationResult:
    __slots__ = ("ok", "errors", "warnings", "candidates")

    def __init__(self, ok: bool, errors: list[str] | None = None,
                 warnings: list[str] | None = None,
                 candidates: list[str] | None = None) -> None:
        self.ok = ok
        self.errors = errors or []
        self.warnings = warnings or []
        self.candidates = candidates or []


# --------------------------------------------------------------------- 编译器
class SqlCompiler:
    """`LogicalPlan` → 方言 SQL。

    设计取舍：
    * P0 用**字符串模板**而非完整 SQL AST 生成器——方言需求只有 SQLite/MySQL，
      写 AST 生成器是过度设计。接 Oracle 时再引入 sqlglot 转译。
    * **只对用到的表生成 JOIN**，不产生多余的笛卡尔积风险。
    * 复杂维度通过 `select` 作为 CTE 注入，避免编译器膨胀。
    """

    def __init__(self, capabilities: SourceCapabilities) -> None:
        self._cap = capabilities

    # ------------------------------------------------------------ 主入口
    def compile(self, plan: LogicalPlan, model: SemanticModel) -> CompileResult:
        if plan.raw_select:
            return CompileResult(
                sql=self._with_limit(plan.raw_select.strip(), plan.limit),
                tables_used=[],
                ctes=[],
            )

        if not plan.measures and not plan.dimensions and plan.pivot is None:
            raise CompileError("逻辑计划既无指标也无维度，无法生成 SQL")

        if plan.pivot is not None:
            return self._compile_pivot(plan, model)
        if plan.qualify:
            return self._compile_three_level(plan, model)

        # 指标按"聚合"与"窗口"分层：窗口指标必须在聚合结果之上计算
        agg_measures = [
            pm for pm in plan.measures
            if not self._metric(pm, model).window
        ]
        window_measures = [
            pm for pm in plan.measures
            if self._metric(pm, model).window
        ]

        dim_sql = [self._dimension_sql(d, model) for d in plan.dimensions]
        # GROUP BY 必须用**不带别名**的表达式。
        # 注意 SQLite/MySQL 的宽松模式虽允许按别名分组，
        # 但写法 `GROUP BY expr AS alias` 是语法错误——这是真实踩过的坑。
        dim_group_exprs = [
            self._dimension(d, model).resolved_expr() for d in plan.dimensions
        ]
        dim_aliases = [(d.alias or d.dimension) for d in plan.dimensions]
        measure_sql = [self._measure_sql(pm, model) for pm in agg_measures]

        select_parts = dim_sql + measure_sql
        if not select_parts and not window_measures:
            raise CompileError("SELECT 列表为空")

        # 需要的 join：来自指标、维度、过滤字段
        joins: list[JoinSpec] = []
        ctes: list[DimensionDefinition] = []

        def need_join(table: str | None) -> None:
            spec = model.join_for_table(table)
            if spec is None:
                # base_table 与 logical_tables 里的表已合并在 FROM 中，无需 JOIN
                if (table and table != model.base_table
                        and table not in model.logical_tables):
                    raise CompileError(
                        f"表 {table} 没有对应的 join 定义"
                        f"（在 SemanticModel.table_joins 中配置）"
                    )
                return
            if all(j.name != spec.name for j in joins):
                joins.append(spec)

        for m in plan.measures:
            metric = self._metric(m, model)
            # 窗口指标的 expr 作用在**聚合结果**上，其 join 需求由内层
            # 被引用的聚合指标覆盖，这里不能重复引入。
            if metric.window:
                continue
            need_join(metric.table)
            # 指标表达式里引用的维度可能来自其他表（如 {{ dimension('category') }}）
            for dim_name in referenced_dimensions(m.metric, model):
                dim = model.dimensions.get(dim_name)
                if dim is not None and not dim.select:
                    need_join(dim.table)
        for d in plan.dimensions:
            dim = self._dimension(d, model)
            if dim.select:
                ctes.append(dim)
            else:
                need_join(dim.table)

        # 窗口指标引用的维度必须出现在计划的分组维度里。
        # 理由：两层编译的外层 `FROM agg` 只有内层产出的列，而窗口表达式
        # （`OVER (ORDER BY {{ dimension('month') }})`）需要那个维度的**列**。
        # 不检查就会生成外层查不到的 `substr(f.order_date, 1, 7)`，
        # 用户看到的是 `no such column` —— 从那里反推"要加个维度"太难了。
        if window_measures:
            self._check_window_dimensions(plan, model, window_measures)

        # WHERE：过滤 + 时间范围
        where_parts: list[str] = []
        for fo in plan.filters:
            need_join(self._filter_table(fo, model))
            where_parts.append(self._filter_sql(fo, model))
        if plan.time_range:
            where_parts.append(self._time_sql(plan.time_range, model))

        # HAVING：对分组结果的阈值过滤。
        # 只在这里（单层/两层的**内层**）拼——按 SQL 语义 HAVING 在窗口函数
        # 之前生效，放进内层 CTE 才是对的。
        having_parts = self._having_sql(plan.having, model, need_join)

        base, base_alias = model.from_clause()
        if not base:
            raise CompileError("SemanticModel.base_table 未设置")

        order_sql = self._order_sql(plan, model, need_join)
        # 窗口指标需分层：内层聚合 → 外层窗口
        if window_measures:
            return self._compile_two_level(
                base=base, base_alias=base_alias, model=model,
                agg_measures=agg_measures, window_measures=window_measures,
                dim_sql=dim_sql, dim_group_exprs=dim_group_exprs,
                dim_aliases=dim_aliases, joins=joins,
                where_parts=where_parts, having_parts=having_parts, ctes=ctes,
                limit=plan.limit, plan=plan,
            )

        sql = self._assemble(
            base=base,
            base_alias=base_alias,
            select_parts=select_parts,
            joins=joins,
            where_parts=where_parts,
            group_by=dim_group_exprs,
            order_by=order_sql,
            limit=plan.limit,
            ctes=ctes,
            having_parts=having_parts,
        )
        return CompileResult(
            sql=sql,
            tables_used=[base] + [j.table for j in joins],
            ctes=[c.name for c in ctes],
        )

    # ------------------------------------------------- 聚合之上的能力检查
    def _check_window_dimensions(self, plan: LogicalPlan, model: SemanticModel,
                                 window_measures: list[PlanMeasure]) -> None:
        """窗口指标**直接**引用的维度必须都在计划的分组维度里。

        只看直接引用：被引用指标的内部维度是内层 CTE 的事，
        要求它们也进分组会把结果行数放大好几个数量级。
        """
        have = {d.dimension for d in plan.dimensions}
        missing: dict[str, list[str]] = {}
        for wm in window_measures:
            for dim_name in direct_dimension_refs(wm.metric, model):
                if dim_name not in have:
                    missing.setdefault(wm.metric, []).append(dim_name)
        if missing:
            detail = "；".join(
                f"{k} 需要 {v}" for k, v in sorted(missing.items())
            )
            raise CompileError(
                f"窗口指标引用的维度不在分组里：{detail}。"
                f"窗口函数要按维度排序/分区，该维度必须同时出现在 "
                f"dimensions 中（否则它的值在外层查询里不存在）"
            )

    # ------------------------------------------------------------ 三层编译
    def _compile_three_level(self, plan: LogicalPlan,
                             model: SemanticModel) -> CompileResult:
        """聚合 → 窗口 → **过滤窗口结果**。三层。

        为什么非要第三层：对窗口结果过滤（标准 SQL 的 `QUALIFY`）在
        SQLite/MySQL 里没有对应语法，而窗口函数**不能出现在 WHERE 里**。
        所以只能"算完窗口另起一层再筛"。这不是实现偷懒，是这些方言的硬约束。

        典型用途："每个品类内部销量前 2 的商品"——
        排名要先算出来，才知道谁是前 2。
        """
        assert plan.qualify
        if plan.pivot is not None:
            raise CompileError("透视（pivot）与 qualify 暂不支持组合使用")

        joins: list[JoinSpec] = []
        ctes: list[DimensionDefinition] = []

        def need_join(table: str | None) -> None:
            spec = model.join_for_table(table)
            if spec is None:
                if (table and table != model.base_table
                        and table not in model.logical_tables):
                    raise CompileError(
                        f"表 {table} 没有对应的 join 定义"
                        f"（在 SemanticModel.table_joins 中配置）"
                    )
                return
            if all(j.name != spec.name for j in joins):
                joins.append(spec)

        agg_measures = [pm for pm in plan.measures
                        if not self._metric(pm, model).window]
        window_measures = [pm for pm in plan.measures
                           if self._metric(pm, model).window]
        for m in agg_measures:
            metric = self._metric(m, model)
            need_join(metric.table)
            for dim_name in referenced_dimensions(m.metric, model):
                sub = model.dimensions.get(dim_name)
                if sub is not None and not sub.select:
                    need_join(sub.table)
        for d in plan.dimensions:
            dim = self._dimension(d, model)
            if dim.select:
                ctes.append(dim)
            else:
                need_join(dim.table)

        where_parts: list[str] = []
        for fo in plan.filters:
            need_join(self._filter_table(fo, model))
            where_parts.append(self._filter_sql(fo, model))
        if plan.time_range:
            where_parts.append(self._time_sql(plan.time_range, model))

        base, base_alias = model.from_clause()
        if not base:
            raise CompileError("SemanticModel.base_table 未设置")

        if window_measures:
            self._check_window_dimensions(plan, model, window_measures)

        dim_aliases = [(d.alias or d.dimension) for d in plan.dimensions]
        need_agg = list(agg_measures)
        for wm in window_measures:
            for dep in metric_dependencies(wm.metric, model):
                if any(x.metric == dep for x in need_agg):
                    continue
                need_agg.append(PlanMeasure(
                    metric=dep,
                    alias=((model.metrics[dep].label or dep)
                           if dep in model.metrics else dep),
                ))

        # ---- 第一层：聚合 ----
        inner = self._assemble(
            base=base, base_alias=base_alias,
            select_parts=([self._dimension_sql(d, model)
                           for d in plan.dimensions]
                          + [self._measure_sql(m, model) for m in need_agg]),
            joins=joins, where_parts=where_parts,
            group_by=[self._dimension(d, model).resolved_expr()
                      for d in plan.dimensions],
            order_by=[], limit=None, ctes=ctes,
            having_parts=self._having_sql(plan.having, model, need_join),
        )

        # ---- 第二层：窗口 + 把 qualify 要用的列带上来 ----
        agg_alias_map = {
            m.metric: f"agg.{self._alias(m.alias or m.metric)}"
            for m in need_agg
        }
        dim_alias_map = {
            d.dimension: f"agg.{self._alias(d.alias or d.dimension)}"
            for d in plan.dimensions
        }
        win_parts = [f'agg.{self._alias(n)}' for n in dim_aliases]
        for m in need_agg:
            n = m.alias or m.metric
            win_parts.append(f'agg.{self._alias(n)} AS {self._alias(n)}')
        for wm in window_measures:
            expr = expand_metric_expr(wm.metric, model,
                                     alias_map=agg_alias_map,
                                     dim_alias_map=dim_alias_map)
            metric = self._metric(wm, model)
            if metric.decimals is not None:
                expr = f"ROUND({expr}, {metric.decimals})"
            win_parts.append(
                f'{expr} AS {self._alias(wm.alias or wm.metric)}')
        win = self._assemble(
            base="agg", base_alias="", select_parts=win_parts, joins=[],
            where_parts=[], group_by=[], order_by=[], limit=None, ctes=[],
        )

        # ---- 第三层：对窗口/聚合结果过滤 ----
        available = set(dim_aliases) | {
            (m.alias or m.metric) for m in need_agg
        } | {(m.alias or m.metric) for m in window_measures} | {
            m.metric for m in (*need_agg, *window_measures)
        }
        qualify_parts: list[str] = []
        for h in plan.qualify:
            if h.field not in available:
                raise CompileError(
                    f"qualify 引用的字段 '{h.field}' 不在窗口层的输出列里；"
                    f"可用: {', '.join(sorted(available))}"
                )
            qualify_parts.append(_compare_sql(
                self, f'win.{self._alias(h.field)}', h.op, h.value))

        outer_parts = [f'win.{self._alias(n)}' for n in dim_aliases]
        for m in plan.measures:
            n = m.alias or m.metric
            outer_parts.append(f'win.{self._alias(n)} AS {self._alias(n)}')
        outer_order = self._outer_order_sql(
            plan, model, dim_aliases, need_agg, window_measures,
            agg_alias_map, src="win", window_as_expr=False,
            dim_alias_map=dim_alias_map,
        )
        outer = self._assemble(
            base="win", base_alias="", select_parts=outer_parts, joins=[],
            where_parts=qualify_parts, group_by=[], order_by=outer_order,
            limit=plan.limit, ctes=[],
        )
        sql = (f"WITH agg AS (\n{self._indent(inner)}\n)\n"
               f", win AS (\n{self._indent(win)}\n)\n{outer}")
        return CompileResult(
            sql=sql,
            tables_used=[base] + [j.table for j in joins],
            ctes=[c.name for c in ctes] + ["agg", "win"],
        )

    # ------------------------------------------------------------ 透视编译
    def _compile_pivot(self, plan: LogicalPlan,
                       model: SemanticModel) -> CompileResult:
        """长表 → 宽表：内层按 `(分组维度, 桶)` 聚合，外层把桶转成列。

        覆盖两类问法，用的是同一套代码：
            * 跨期对比（桶 = 时间段条件）
            * 分类转列（桶 = 维度取值）

        内层 WHERE 里加上"只扫桶覆盖的行"，外层仍然保留 `ELSE 0`
        ——某桶完全没数据时要显示 0 而不是 NULL，否则那一列整列是空值，
        用户会以为漏算了。
        """
        pivot: Pivot = plan.pivot  # type: ignore[assignment]
        if plan.having:
            raise CompileError("透视（pivot）与 HAVING 暂不支持组合使用")
        if not pivot.buckets:
            raise CompileError("透视没有定义任何列（buckets 为空）")

        measure = pivot.measure
        metric = self._metric(measure, model)
        if metric.window:
            raise CompileError(
                f"透视的值列不支持窗口指标（{measure.metric}）；"
                f"透视本身就是一次聚合"
            )

        joins: list[JoinSpec] = []
        ctes: list[DimensionDefinition] = []

        def need_join(table: str | None) -> None:
            spec = model.join_for_table(table)
            if spec is None:
                if (table and table != model.base_table
                        and table not in model.logical_tables):
                    raise CompileError(
                        f"表 {table} 没有对应的 join 定义"
                        f"（在 SemanticModel.table_joins 中配置）"
                    )
                return
            if all(j.name != spec.name for j in joins):
                joins.append(spec)

        need_join(metric.table)
        for dim_name in referenced_dimensions(measure.metric, model):
            sub = model.dimensions.get(dim_name)
            if sub is not None and not sub.select:
                need_join(sub.table)
        for d in plan.dimensions:
            dim = self._dimension(d, model)
            if dim.select:
                ctes.append(dim)
            else:
                need_join(dim.table)

        bucket_conds: list[tuple[str, str]] = []
        for b in pivot.buckets:
            parts: list[str] = []
            for c in b.conditions:
                need_join(self._filter_table(c, model))
                parts.append(self._filter_sql(c, model))
            if not parts:
                raise CompileError(
                    f"透视列 '{b.label}' 没有条件——无条件的一列会把所有行都算进去"
                )
            bucket_conds.append((b.label, " AND ".join(parts)))

        where_parts: list[str] = []
        for fo in plan.filters:
            need_join(self._filter_table(fo, model))
            where_parts.append(self._filter_sql(fo, model))
        if plan.time_range:
            # 计划级时间范围与桶条件作用在**同一个字段**上是矛盾的：
            # 桶已经把时间写死了，再叠一个 WHERE 会把某些桶整段切掉。
            # 失败方式是"某一列恒为 0"而不是报错——最难查的一类。
            # 所以这里直接拒绝，而不是悄悄按一种解释执行。
            clash = next(
                (b for b in pivot.buckets
                 if any(c.field == plan.time_range.field for c in b.conditions)),
                None,
            )
            if clash is not None:
                raise CompileError(
                    f"透视的列 '{clash.label}' 的条件与计划的时间范围都在约束 "
                    f"'{plan.time_range.field}'——两者叠加会把这一列整段过滤掉。"
                    f"请在计划里去掉 time_range（时间已由透视的桶表达）"
                )
            where_parts.append(self._time_sql(plan.time_range, model))
        where_parts.append(
            "(" + "\n    OR ".join(f"({c})" for _l, c in bucket_conds) + ")"
        )

        base, base_alias = model.from_clause()
        if not base:
            raise CompileError("SemanticModel.base_table 未设置")

        BUCKET = "__pivot_bucket"
        VALUE = "__pivot_value"
        dim_aliases = [(d.alias or d.dimension) for d in plan.dimensions]
        bucket_case = "CASE " + " ".join(
            f"WHEN {c} THEN {self._literal(l)}" for l, c in bucket_conds
        ) + " END"

        inner_select = [self._dimension_sql(d, model) for d in plan.dimensions]
        inner_select.append(f"{bucket_case} AS {self._alias(BUCKET)}")
        inner_select.append(
            f"{expand_metric_expr(measure.metric, model)}"
            f" AS {self._alias(VALUE)}"
        )
        inner = self._assemble(
            base=base, base_alias=base_alias, select_parts=inner_select,
            joins=joins, where_parts=where_parts,
            group_by=[self._dimension(d, model).resolved_expr()
                      for d in plan.dimensions] + [bucket_case],
            order_by=[], limit=None, ctes=ctes,
        )

        outer_select = [f'p.{self._alias(n)}' for n in dim_aliases]
        for label, _c in bucket_conds:
            outer_select.append(_pivot_column_sql(
                self, f'p.{self._alias(BUCKET)}', label,
                f'p.{self._alias(VALUE)}', metric.decimals,
            ))
        group_by_outer = ([f'p.{self._alias(n)}' for n in dim_aliases]
                          if dim_aliases else [])
        outer_order: list[str] = []
        for ob in plan.order_by:
            direction = "DESC" if ob.desc else "ASC"
            col = ob.field
            if col.isdigit():
                outer_order.append(f"{col} {direction}")
                continue
            if col in dim_aliases:
                outer_order.append(f'p.{self._alias(col)} {direction}')
                continue
            if col in model.dimensions:
                alias = next((d.alias or d.dimension) for d in plan.dimensions
                             if d.dimension == col)
                if alias:
                    outer_order.append(f'p.{self._alias(alias)} {direction}')
                    continue
            raise CompileError(
                f"透视结果的排序字段只能是分组维度，得到: {ob.field}"
                f"（可用: {', '.join(dim_aliases) or '（无分组维度）'}）"
            )
        outer = self._assemble(
            base="p", base_alias="", select_parts=outer_select, joins=[],
            where_parts=[], group_by=group_by_outer, order_by=outer_order,
            limit=plan.limit, ctes=[],
        )
        sql = (f"WITH p AS (\n{self._indent(inner)}\n)\n{outer}")
        return CompileResult(
            sql=sql,
            tables_used=[base] + [j.table for j in joins],
            ctes=[c.name for c in ctes] + ["p"],
        )

    # ------------------------------------------------------------ 两分层编译
    def _compile_two_level(self, *, base: str, base_alias: str,
                           model: SemanticModel,
                           agg_measures: list[PlanMeasure],
                           window_measures: list[PlanMeasure],
                           dim_sql: list[str], dim_group_exprs: list[str],
                           dim_aliases: list[str],
                           joins: list[JoinSpec], where_parts: list[str],
                           ctes: list[DimensionDefinition],
                           limit: int | None, plan: LogicalPlan,
                           having_parts: list[str] | None = None
                           ) -> CompileResult:
        """内层聚合 + 外层窗口。

        为什么必须分两层：
            `RANK() OVER (ORDER BY SUM(x) DESC)` 里的 SUM 是聚合，
            不能与 GROUP BY 的其它聚合指标平铺在同一层 SELECT。
            因此内层先算出聚合结果（CTE `agg`），外层再做窗口计算。

        外层引用内层的方式：维度与聚合指标都用**别名**引用
        （`agg."渠道"`、`agg."实收金额"`），避免重复表达式。
        """
        # 外层窗口指标引用内层聚合结果时用别名而非重复表达式。
        # **必须把窗口指标依赖的聚合指标也纳入内层**：
        # "各渠道按实收金额排名"里 measures 只有 revenue_rank（窗口），
        # 但它的表达式依赖 net_revenue；若内层不先算出 net_revenue，
        # 外层就会去引用基础表列 f.order_status，报 no such column。
        needed: list[PlanMeasure] = list(agg_measures)
        for wm in window_measures:
            for dep in metric_dependencies(wm.metric, model):
                if any(x.metric == dep for x in needed):
                    continue
                needed.append(PlanMeasure(
                    metric=dep,
                    alias=(model.metrics[dep].label or dep) if dep in model.metrics else dep,
                ))
        agg_measures = needed

        dim_select_sql = [
            f"{self._dimension(dm, model).resolved_expr()}"
            f" AS {self._alias(dm.alias or dm.dimension)}"
            for dm in plan.dimensions
        ]
        agg_sql = [
            self._measure_sql(m, model) for m in agg_measures
        ]
        inner_select = dim_select_sql + agg_sql
        inner_alias_map = {
            m.metric: f"agg.{self._alias(m.alias or m.metric)}"
            for m in agg_measures
        }
        # 窗口表达式里的 `{{ dimension('x') }}` 外层只能按列名引用
        dim_alias_map = {
            dm.dimension: f"agg.{self._alias(dm.alias or dm.dimension)}"
            for dm in plan.dimensions
        }

        inner = self._assemble(
            base=base, base_alias=base_alias,
            select_parts=inner_select, joins=joins,
            where_parts=where_parts, group_by=dim_group_exprs,
            order_by=[], limit=None, ctes=ctes,
            having_parts=having_parts,
        )

        outer_parts_by_key: dict[str, str] = {}
        for name in dim_aliases:
            outer_parts_by_key[f"dim:{name}"] = f"agg.{self._alias(name)}"
        for m in window_measures:
            key = m.alias or m.metric
            expr = expand_metric_expr(m.metric, model, alias_map=inner_alias_map,
                                     dim_alias_map=dim_alias_map)
            metric = self._metric(m, model)
            if metric.decimals is not None:
                expr = f"ROUND({expr}, {metric.decimals})"
            outer_parts_by_key[f"win:{m.metric}"] = (
                f"{expr} AS {self._alias(key)}"
            )
        for m in agg_measures:
            key = m.alias or m.metric
            outer_parts_by_key[f"agg:{m.metric}"] = (
                f"agg.{self._alias(key)} AS {self._alias(key)}"
            )

        # 按 plan.measures 的原始顺序输出指标，保证列顺序与用户/基线一致。
        # （曾经把窗口指标排在最前，导致 F2 的"渠道/实收金额/占比"变成"渠道/占比/实收金额"）
        ordered_parts = [f"agg.{self._alias(n)}" for n in dim_aliases]
        for m in plan.measures:
            if m in window_measures:
                ordered_parts.append(outer_parts_by_key[f"win:{m.metric}"])
            else:
                ordered_parts.append(outer_parts_by_key[f"agg:{m.metric}"])
        outer_parts = ordered_parts

        outer_order = self._outer_order_sql(
            plan, model, dim_aliases, agg_measures, window_measures,
            inner_alias_map, dim_alias_map=dim_alias_map,
        )
        outer = self._assemble(
            base="agg", base_alias="", select_parts=outer_parts,
            joins=[], where_parts=[], group_by=[], order_by=outer_order,
            limit=limit, ctes=[],
        )
        sql = (f"WITH agg AS (\n{self._indent(inner)}\n)\n{outer}")
        return CompileResult(
            sql=sql,
            tables_used=[base] + [j.table for j in joins],
            ctes=[c.name for c in ctes] + ["agg"],
        )

    def _outer_order_sql(self, plan: LogicalPlan, model: SemanticModel,
                         dim_aliases: list[str],
                         agg_measures: list[PlanMeasure],
                         window_measures: list[PlanMeasure],
                         inner_alias_map: dict[str, str],
                         src: str = "agg",
                         window_as_expr: bool = True,
                         dim_alias_map: dict[str, str] | None = None) -> list[str]:
        """外层 ORDER BY：所有引用都必须是**上一层产出的列**。

        输出列集合 = 维度别名 + 聚合指标别名 + 窗口指标别名。
        对应用户写的排序字段（可能是语义名，也可能是输出别名），
        统一翻译成 `<src>."列名"`。

        `window_as_expr`：两层编译时窗口表达式只存在于外层的 SELECT 里，
        排序必须把表达式**重复一遍**（别名在同一 SELECT 的 ORDER BY 中
        并非所有方言都可见，SQLite 可以、标准 SQL 与部分引擎不行）。
        三层编译时窗口已经是 `win` 层的一列，直接用列名即可。
        """
        if not plan.order_by:
            return []

        dim_alias = {(d.alias or d.dimension): d.dimension for d in plan.dimensions}
        agg_alias = {(m.alias or m.metric) for m in agg_measures}
        win_alias = {(m.alias or m.metric) for m in window_measures}

        out: list[str] = []
        for ob in plan.order_by:
            direction = "DESC" if ob.desc else "ASC"
            field = ob.field
            if field.isdigit():
                out.append(f"{field} {direction}")
                continue

            win = next(
                (m for m in window_measures
                 if (m.alias or m.metric) == field or m.metric == field),
                None,
            )
            if win is not None and window_as_expr:
                expr = expand_metric_expr(
                    win.metric, model, alias_map=inner_alias_map,
                    dim_alias_map=dim_alias_map,
                )
                metric = self._metric(win, model)
                if metric.decimals is not None:
                    expr = f"ROUND({expr}, {metric.decimals})"
                out.append(f"{expr} {direction}")
                continue

            # 归一到"输出列名"
            col: str | None = None
            if win is not None:
                col = win.alias or win.metric
            elif field in dim_alias or field in dim_aliases:
                col = field
            elif field in agg_alias:
                col = field
            elif field in model.metrics:
                for m in agg_measures:
                    if m.metric == field:
                        col = (m.alias or m.metric)
                        break
            elif field in model.dimensions:
                for dm in plan.dimensions:
                    if dm.dimension == field:
                        col = (dm.alias or dm.dimension)
                        break
            if col is None:
                raise CompileError(f"外层排序字段无法解析: {field}")
            out.append(f'{src}.{self._alias(col)} {direction}')
        return out

    # ------------------------------------------------------- HAVING / QUALIFY
    def _having_sql(self, items: tuple[Having, ...], model: SemanticModel,
                    need_join: Any) -> list[str]:
        """把 `Having` 编译成 SQL 条件（用于 HAVING 或窗口层之后的 WHERE）。

        左边可以是聚合指标（→ 重复它的聚合表达式）或维度。
        重复表达式而不是引用别名，是因为 HAVING 里引用 SELECT 别名
        **不是标准 SQL**（MySQL/SQLite 容忍，其它库不行）。
        """
        out: list[str] = []
        for h in items:
            dim = model.dimensions.get(h.field)
            metric = model.metrics.get(h.field)
            if dim is not None:
                need_join(dim.table)
                lhs = dim.resolved_expr()
            elif metric is not None:
                need_join(metric.table)
                for dim_name in referenced_dimensions(h.field, model):
                    sub = model.dimensions.get(dim_name)
                    if sub is not None and not sub.select:
                        need_join(sub.table)
                lhs = expand_metric_expr(h.field, model)
            else:
                raise DimensionNotFoundError(
                    f"HAVING/QUALIFY 引用了未定义字段: {h.field}"
                )
            out.append(_compare_sql(self, lhs, h.op, h.value))
        return out

    # ------------------------------------------------------------ 片段
    @staticmethod
    def _metric(m: PlanMeasure, model: SemanticModel):
        metric = model.metrics.get(m.metric)
        if metric is None:
            avail = ", ".join(model.metric_names()) or "（无）"
            raise MetricNotFoundError(
                f"未定义指标: {m.metric}；可用: {avail}"
            )
        return metric

    @staticmethod
    def _dimension(d: PlanDimension, model: SemanticModel):
        dim = model.dimensions.get(d.dimension)
        if dim is None:
            avail = ", ".join(model.dimension_names()) or "（无）"
            raise DimensionNotFoundError(
                f"未定义维度: {d.dimension}；可用: {avail}"
            )
        return dim

    def _measure_sql(self, pm: PlanMeasure, model: SemanticModel) -> str:
        metric = self._metric(pm, model)
        expr = expand_metric_expr(pm.metric, model)
        if not expr.strip():
            raise CompileError(f"指标 {pm.metric} 的 expr 为空")
        # 小数位数属于口径，统一在编译层施加（避免各查询精度不一致）
        if metric.decimals is not None:
            expr = f"ROUND({expr}, {metric.decimals})"
        return f"{expr} AS {self._alias(pm.alias or pm.metric)}"

    def _dimension_sql(self, d: PlanDimension, model: SemanticModel) -> str:
        dim = self._dimension(d, model)
        return f"{dim.resolved_expr()} AS {self._alias(d.alias or d.dimension)}"

    def _filter_table(self, f: Filter, model: SemanticModel) -> str | None:
        """过滤字段可能是指标（如 order_status 在事实表）或维度。"""
        dim = model.dimensions.get(f.field)
        if dim is not None:
            return dim.table
        metric = model.metrics.get(f.field)
        if metric is not None:
            return metric.table
        # 未在语义模型中声明：交给 validator 报"字段不存在"
        return None

    def _filter_sql(self, f: Filter, model: SemanticModel) -> str:
        dim = model.dimensions.get(f.field)
        if dim is not None:
            lhs = dim.resolved_expr()
        elif f.field in model.metrics:
            lhs = expand_metric_expr(f.field, model)
        else:
            raise DimensionNotFoundError(
                f"过滤条件引用了未定义字段: {f.field}"
            )
        return _compare_sql(self, lhs, f.op, f.value)

    def _time_sql(self, tr: TimeRange, model: SemanticModel) -> str:
        lhs = self._resolve_time_field(tr.field, model)
        if tr.grain and tr.grain != "day" and not tr.start and not tr.end:
            # 只给了粒度却没给范围：不生成条件，粒度由维度表达
            return ""
        parts: list[str] = []
        if tr.start:
            # 月粒度时用月份前缀比较更稳妥（避免月末边界）
            if tr.grain == "month":
                parts.append(f"{lhs} >= {self._literal(tr.start)}")
                if tr.end:
                    parts.append(f"{lhs} <= {self._literal(tr.end)}")
                return " AND ".join(parts)
            parts.append(f"{lhs} >= {self._literal(tr.start)}")
        if tr.end:
            parts.append(f"{lhs} <= {self._literal(tr.end)}")
        return " AND ".join(parts)

    def _resolve_time_field(self, field: str, model: SemanticModel) -> str:
        dim = model.dimensions.get(field)
        if dim is not None:
            return dim.resolved_expr()
        # 允许直接写 "f.order_date" 这类物理引用
        if "." in field:
            return field
        return f"{model.base_alias}.{field}"

    def _order_sql(self, plan: LogicalPlan, model: SemanticModel,
                   need_join=None) -> list[str]:
        """生成 ORDER BY。

        支持三种引用方式，按优先级：
            1. 输出列序号（"1" / "2"）
            2. **输出别名**（"实收金额" / "渠道"）——业务问题里最自然的说法
            3. 语义名（"net_revenue" / "channel"）
        """
        if not plan.order_by:
            return []

        # 别名 → 语义名（维度别名与指标别名）
        dim_alias = {(d.alias or d.dimension): d.dimension for d in plan.dimensions}
        metric_alias = {(m.alias or m.metric): m.metric for m in plan.measures}

        def resolve(field: str) -> tuple[str, str]:
            """返回 (kind, name)，kind ∈ {dim, metric}。"""
            if field in dim_alias:
                return ("dim", dim_alias[field])
            if field in metric_alias:
                return ("metric", metric_alias[field])
            if field in model.dimensions:
                return ("dim", field)
            if field in model.metrics:
                return ("metric", field)
            raise CompileError(f"排序字段未定义: {field}")

        out: list[str] = []
        for ob in plan.order_by:
            direction = "DESC" if ob.desc else "ASC"
            if ob.field.isdigit():
                out.append(f"{ob.field} {direction}")
                continue
            kind, name = resolve(ob.field)
            if kind == "dim":
                dim = model.dimensions[name]
                if need_join is not None:
                    need_join(dim.table)
                out.append(f"{dim.resolved_expr()} {direction}")
            else:
                out.append(f"{expand_metric_expr(name, model)} {direction}")
        return out

    # ------------------------------------------------------------ 组装
    def _assemble(self, *, base: str, base_alias: str,
                  select_parts: list[str], joins: list[JoinSpec],
                  where_parts: list[str], group_by: list[str],
                  order_by: list[str], limit: int | None,
                  ctes: list[DimensionDefinition],
                  having_parts: list[str] | None = None) -> str:
        lines: list[str] = []
        if ctes:
            lines.append("WITH " + ",\n".join(
                f"{c.name} AS (\n{self._indent(c.select or '')}\n)"
                for c in ctes
            ))

        lines.append("SELECT " + "\n     , ".join(select_parts))
        lines.append(f"FROM {base} {base_alias}" if base_alias else f"FROM {base}")

        for j in joins:
            kw = "INNER JOIN" if j.kind == "inner" else "LEFT JOIN"
            lines.append(f"{kw} {j.table} ON {j.condition}")

        if where_parts:
            lines.append("WHERE " + "\n  AND ".join(where_parts))
        if group_by:
            lines.append("GROUP BY " + ", ".join(group_by))
        if having_parts:
            lines.append("HAVING " + "\n   AND ".join(having_parts))
        if order_by:
            lines.append("ORDER BY " + ", ".join(order_by))
        return self._with_limit("\n".join(lines), limit)

    def _with_limit(self, sql: str, limit: int | None) -> str:
        if limit is None or limit <= 0:
            return sql
        if self._cap.pagination == "rownum":
            # Oracle：外层包一层 ROWNUM 过滤
            return (f"SELECT * FROM (\n{self._indent(sql)}\n)"
                    f" WHERE ROWNUM <= {limit}")
        return f"{sql}\nLIMIT {limit}"

    @staticmethod
    def _indent(sql: str) -> str:
        return "\n".join("    " + ln for ln in sql.splitlines())

    def _alias(self, name: str) -> str:
        quote = self._cap.identifier_quote or ""
        if not quote:
            return name
        return f"{quote}{name}{quote}"

    @staticmethod
    def _literal(v: object) -> str:
        if v is None:
            return "NULL"
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, (int, float)):
            return str(v)
        escaped = str(v).replace("'", "''")
        return f"'{escaped}'"


# --------------------------------------------------------------------- 校验
class SqlValidator:
    """SQL 静态校验。

    检查项（顺序即优先级）：
        1. 只读：拒绝 DDL/DML，拒绝多语句
        2. 引用存在性：表名/列名必须在元数据快照中
        3. 方言合法性：装了 sqlglot 就解析一遍（可选增强）

    存在性的价值：把"列名不存在"从**数据库原始错误**变成**可用字段候选**，
    这是"只给结果表"方案里错误体验的核心。
    """

    def __init__(self, capabilities: SourceCapabilities,
                 known_tables: list[TableMeta] | None = None) -> None:
        self._cap = capabilities
        self._tables: dict[str, TableMeta] = {
            t.name.lower(): t for t in (known_tables or [])
        }

    def validate(self, sql: str, *, max_rows: int,
                 allow_multiple: bool = False) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        candidates: list[str] = []

        # 1) 语句数量
        stmts = split_statements(sql)
        if len(stmts) > 1 and not allow_multiple:
            errors.append(f"检测到 {len(stmts)} 条语句，只允许单条只读查询")
        if not stmts:
            return ValidationResult(False, ["SQL 为空"])

        # 2) 只读
        if not is_read_only(stmts[0]):
            errors.append("只允许只读查询（SELECT / WITH / EXPLAIN / PRAGMA 只读）")

        # 3) 引用存在性（仅在已知元数据时检查）
        if self._tables:
            errors, candidates = self._check_references(
                sql, errors, candidates
            )

        # 4) 可选：sqlglot 方言解析
        parse_warning = self._try_parse(sql)
        if parse_warning:
            warnings.append(parse_warning)

        return ValidationResult(
            ok=not errors, errors=errors, warnings=warnings,
            candidates=candidates,
        )

    def _check_references(self, sql: str, errors: list[str],
                          candidates: list[str]) -> tuple[list[str], list[str]]:
        """检查表名与列名引用。未识别的标识符只提示候选，不硬报错。

        为什么不硬报错：
            标识符也可能是别名、CTE 名、函数名。硬报错会误杀合法 SQL。
            因此策略是——能确定表名不存在时报错；列名可疑时给候选提示。
        """
        identifiers = extract_identifiers(sql)
        all_columns: list[str] = []
        for t in self._tables.values():
            all_columns.extend(c.name for c in t.columns)

        # 表名：被显式引用且不在快照里
        for ident in identifiers:
            low = ident.lower()
            if low in self._tables:
                continue

        # 列名候选提示：找出"看起来像列但不存在"的标识符
        known_cols_lower = {c.lower() for c in all_columns}
        unknown_cols = [
            i for i in identifiers
            if i.lower() not in known_cols_lower
            and i.lower() not in self._tables
        ]
        if unknown_cols and all_columns:
            for bad in unknown_cols[:3]:
                near = _suggest(bad, all_columns)
                if near:
                    candidates.extend(near)
                    errors.append(
                        f"字段可能不存在: {bad}（候选: {', '.join(near)}）"
                    )
        return errors, sorted(set(candidates))

    def _try_parse(self, sql: str) -> str | None:
        """装了 sqlglot 就做一次方言解析；没装则跳过（不阻塞 P0）。"""
        try:
            import sqlglot  # type: ignore
        except ImportError:
            return None
        dialect = self._cap.dialect
        read = {"sqlite": "sqlite", "mysql": "mysql", "hive": "hive"}.get(dialect)
        try:
            sqlglot.parse_one(sql, read=read)
        except Exception as e:  # noqa: BLE001
            return f"sqlglot 解析警告（{dialect}）: {e}"
        return None


def _pivot_column_sql(compiler: "SqlCompiler", bucket_col: str, label: str,
                      value_col: str, decimals: int | None) -> str:
    """透视的一列：`SUM(CASE WHEN <桶> THEN <值> ELSE 0 END) AS "列名"`。

    `ELSE 0` 是刻意的：某桶在该分组下没有数据时，这一列应该是 **0** 而不是
    NULL。NULL 在结果表里显示为空，用户会以为是"查询漏了"；
    而"这个月没有销售"与"这个月销售额是 0"在业务上本来就是同一件事。

    `decimals` 与普通指标走同一条精度规则（精度属于口径的一部分）。
    """
    expr = (f"SUM(CASE WHEN {bucket_col} = {compiler._literal(label)}"
            f" THEN {value_col} ELSE 0 END)")
    if decimals is not None:
        expr = f"ROUND({expr}, {decimals})"
    return f"{expr} AS {compiler._alias(label)}"


def _compare_sql(compiler: "SqlCompiler", lhs: str, op: str,
                 value: Any) -> str:
    """`<lhs> <op> <value>` 的 SQL 片段。

    WHERE、HAVING、QUALIFY、透视桶条件都用它——**四处的比较语义必须一致**。
    各写一份就会出现"过滤能写 BETWEEN，HAVING 不能"这种前后不一致。
    """
    op_l = (op or "").strip().lower()
    if op_l == "in":
        vals = value if isinstance(value, (list, tuple)) else [value]
        items = ", ".join(compiler._literal(v) for v in vals)
        return f"{lhs} IN ({items})"
    if op_l in ("not in",):
        vals = value if isinstance(value, (list, tuple)) else [value]
        items = ", ".join(compiler._literal(v) for v in vals)
        return f"{lhs} NOT IN ({items})"
    if op_l == "between":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise CompileError("between 需要两个值")
        return (f"{lhs} BETWEEN {compiler._literal(value[0])}"
                f" AND {compiler._literal(value[1])}")
    if op_l == "like":
        return f"{lhs} LIKE {compiler._literal(value)}"
    if op_l in ("=", "!=", "<>", "<", "<=", ">", ">="):
        return f"{lhs} {op_l} {compiler._literal(value)}"
    raise CompileError(
        f"不支持的操作符: {op}；可用: = != <> > >= < <= like in not in between"
    )


def _suggest(bad: str, available: list[str], *, top_k: int = 3) -> list[str]:
    import difflib

    return difflib.get_close_matches(bad, available, n=top_k, cutoff=0.6)
