"""语义层：业务概念 → 逻辑计划。

格式：**兼容 dbt metrics.yml，不自创 DSL**。
职责边界：只产出 LogicalPlan（要什么），不产出 SQL（怎么写）。
"""
from __future__ import annotations

from ..contracts.semantic import LogicalPlan, SemanticModel

__all__ = ["DbtMetricsProvider", "compile_plan_to_sql", "SemanticRegistry"]


class DbtMetricsProvider:
    """TODO(S4): 加载 dbt metrics.yml 并 resolve 成 LogicalPlan。

    要点：
      1. load(): 解析 yaml → SemanticModel（指标/维度/实体/同义词）
      2. resolve(): 问题 → LogicalPlan。确定性：同入参同指纹
      3. 未覆盖抛 SemanticResolutionError，由上层决定自由生成 + 标注未验证口径
      4. 相对时间（本月/上月）必须以显式 now 锚定，不许读系统时钟
    """

    def __init__(self, metrics_path: str, source_id: str) -> None:
        self._path = metrics_path
        self._source_id = source_id

    def load(self) -> SemanticModel:
        raise NotImplementedError("S4")

    def resolve(self, question: str, *, now: str | None = None) -> LogicalPlan:
        raise NotImplementedError("S4")


def compile_plan_to_sql(plan: LogicalPlan, model: SemanticModel, dialect: str) -> str:
    """逻辑计划 → 方言 SQL。

    TODO(S3): 先用字符串模板 + sqlglot 做方言转译，
    不要一上来就写完整的 SQL AST 生成器。
    """
    raise NotImplementedError("S3")


class SemanticRegistry:
    """多数据源的语义模型集合（每个 source_id 一个模型 + 版本）。"""

    def __init__(self) -> None:
        self._models: dict[str, SemanticModel] = {}
