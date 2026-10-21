"""指标依赖分析。

单独成文件的原因：编译器需要它，但它只依赖契约层，独立后更好测试，
也避免 `dialect/__init__.py` 继续膨胀。
"""
from __future__ import annotations

import re

from ..contracts.semantic import SemanticModel

__all__ = ["metric_refs", "metric_dependencies", "direct_dimension_refs"]

_METRIC_REF = re.compile(r"\{\{\s*metric\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")
_DIM_RE = re.compile(r"\{\{\s*dimension\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")


def metric_refs(expr: str) -> list[str]:
    """列出表达式里直接引用的指标名（保序去重）。"""
    out: list[str] = []
    for m in _METRIC_REF.finditer(expr or ""):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


def direct_dimension_refs(metric_name: str,
                          model: SemanticModel) -> list[str]:
    """列出某指标**自身表达式**里直接引用的维度名（保序去重）。

    与 `referenced_dimensions` 的区别：那个是**递归**的（含被引用指标的维度），
    这个只看这一层。两分层编译需要这个区分：

        累计指标 `SUM({{ metric('net_revenue') }}) OVER (ORDER BY {{ dimension('month') }})`

        它直接引用的只有 `month`（必须在分组维度里，外层才拿得到这一列）；
        `net_revenue` 内部的 `order_status`/`amount` 是**内层**的事，
        内层 CTE 已经把它们消化掉了，不需要出现在分组里。

    用递归版本去检查就会要求把 `order_status`、`amount` 也放进分组——
    那会把结果从 6 行变成上百行。
    """
    metric = model.metrics.get(metric_name)
    if metric is None:
        return []
    out: list[str] = []
    for m in _DIM_RE.finditer(metric.expr or ""):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


def metric_dependencies(name: str, model: SemanticModel,
                        _seen: frozenset[str] = frozenset()) -> list[str]:
    """递归列出某指标依赖的其它指标（不含自身）。

    两分层编译需要它：窗口指标（排名、占比）的表达式引用聚合指标，
    内层必须先把这些聚合算出来，外层才能引用它们的别名。
    否则外层会去引用基础表列，报 `no such column`。
    """
    if name in _seen:
        return []
    metric = model.metrics.get(name)
    if metric is None:
        return []
    seen = _seen | {name}
    out: list[str] = []
    for dep in metric_refs(metric.expr):
        if dep not in out:
            out.append(dep)
        for deeper in metric_dependencies(dep, model, seen):
            if deeper not in out:
                out.append(deeper)
    return out
