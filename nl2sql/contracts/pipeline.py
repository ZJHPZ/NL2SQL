"""管线契约：编排状态、节点、对外结果。

设计要点
--------
1. **错误交互是一等公民**：本项目只给"SQL + 结果表"，不给分析长文，
   因此错误与边界反馈**就是产品体验本身**，必须结构化而不是抛异常了事。
2. **DAG 引擎通用**：`NodeSpec` 不假定任何 RAG 专用字段。
   P0 只暴露 RAG 相关节点，但数据结构上任何组件都能成为节点——
   这样后期扩展是"加节点"而不是"重写引擎"。
3. **状态可序列化**：`GraphState` 是普通 dict 结构，便于持久化与回放。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from .knowledge import RetrievalHit
from .semantic import LogicalPlan
from .source import QueryResult

QueryStatus = Literal[
    "ok",               # 正常返回结果
    "empty",            # 执行成功但无匹配数据
    "truncated",        # 结果被 LIMIT 截断
    "unresolved",       # 问题无法映射到语义模型（未覆盖口径）
    "invalid_sql",      # 生成/编译出的 SQL 未通过校验
    "blocked",          # 被安全策略拦截
    "timeout",          # 执行超时
    "error",            # 其他执行错误
    # agent 主动追问、等待用户回答（回合制澄清）。
    # 它是**正常状态**而非错误：智能体在信息不足时选择不猜。
    "need_clarification",
]


@dataclass
class QueryOutcome:
    """对外统一返回。壳只负责渲染，不做判断。

    关键字段说明
    ----------
    * `suggestions`：面向用户的可操作建议。例如列名不存在时给出候选字段，
      超时时建议加时间范围——**不要直接抛数据库原始错误**。
    * `applied_filters`：结果为空时回显实际生效的过滤条件，
      让用户知道"是条件筛掉了"而不是"系统坏了"。
    * `notes`：例如"未验证口径"，必须透出，不能静默。
    """

    status: QueryStatus
    question: str
    source_id: str
    sql: str | None = None
    result: QueryResult | None = None
    plan: LogicalPlan | None = None
    hits: list[RetrievalHit] = field(default_factory=list)

    # 错误与边界反馈
    error_code: str | None = None
    message: str | None = None
    suggestions: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)   # 候选字段/表名
    applied_filters: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # 可观测
    plan_fingerprint: str | None = None
    cached: bool = False
    elapsed_ms: int = 0
    data_time: str | None = None
    trace: dict[str, Any] = field(default_factory=dict)   # 各节点耗时，供回放

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "empty", "truncated")


# --------------------------------------------------------------------- 节点
@dataclass
class NodeSpec:
    """DAG 节点声明。**通用结构**，不假定任何组件专属字段。

    用户自定义组件 = 注册一个 NodeSpec + 实现 `run`。
    可视化编排 = 编辑 nodes/edges，不需要改引擎。
    """

    name: str
    kind: str                          # "intent" | "retrieve" | "plan" | "compile" | "guard" | "execute" | "custom"
    label: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    # 允许替换的实现名（注册表键），支持用户自定义 Prompt/模型
    impl: str | None = None


@dataclass
class Edge:
    source: str
    target: str
    condition: str | None = None


@dataclass
class GraphSpec:
    """一条管线。P0 只暴露 RAG 节点，但结构上支持任意节点组合。"""

    name: str
    nodes: list[NodeSpec] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    version: str = "v1"


@runtime_checkable
class PipelineNode(Protocol):
    """节点执行契约。

    实现要求：
    * 纯函数式：读 state、返回 state 的增量（不原地改共享对象）
    * 不直接访问全局；所需依赖由构造时注入
    * 失败时写 `state["error"]`，不要抛异常打断整图（除不可恢复错误）
    """

    spec: NodeSpec

    def run(self, state: dict[str, Any]) -> dict[str, Any]: ...


# --------------------------------------------------------------------- 状态
# LangGraph 的 State 用 TypedDict 描述；这里保持纯类型定义，不引入 langgraph 依赖
GraphState = dict[str, Any]
# 约定键（非强制，但节点之间靠这些键通信）：
#   question: str
#   source_id: str
#   now: str                    相对时间锚点
#   intent: str
#   plan: LogicalPlan
#   sql: str
#   result: QueryResult
#   hits: list[RetrievalHit]
#   outcome: QueryOutcome
#   error: str | None
#   trace: dict[str, int]


class EngineError(Exception):
    """核心可预期错误的基类。"""
