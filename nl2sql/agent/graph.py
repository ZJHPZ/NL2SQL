"""LangGraph 执行器：把 agent 的一步一步铺成图。

为什么要有第二个执行器
----------------------
M1 结束时，项目里"LangGraph"只出现在三处注释和一个 `raise NotImplementedError`
里。一个声称"基于 LangGraph 的智能体"的系统，如果真正的执行器是手写 while
循环，那么设计文档里那句话就是空的。M2 把它补上。

但补上不等于"把 while 换成 graph 就完事"。这里有一条硬约束：

    **两个执行器必须给出同一个结果，逐字节。**

否则"用没用 LangGraph"就变成一个关于代码形态的问题，而用户看到的行为
会随机依赖我们上次改了哪个执行器。所以两个执行器共享 `machine.AgentCore`
的全部逻辑，`graph.py` 只负责**调度**：决定下一步进哪个节点。

图长这样（节点 = 一步，边 = 路由）
----------------------------------
         ┌──────────────────────────── retry ────────────────────────────┐
         │                                                               │
    ┌────▼────┐  review  ┌────────┐  tool   ┌─────┐  continue  ┌────┐   │
    │ decide  ├─────────►│ review ├────────►│ act ├───────────►│(回)│───┘
    └────┬────┘          └───┬────┘         └──┬──┘            └────┘
         │ exhausted         │ final/clarify   │ stop
         │                   ▼                 ▼
         └──────────────►┌──────────────────────────┐
                         │ finish （补收尾 + 出结果）│
                         └────────────┬─────────────┘
                                      ▼
                                     END

`review` 是 7 道护栏所在的地方：它可能把模型顶回去（retry），
也可能判定可以收尾（final）。**护栏在图上是一个节点**，这比藏在
`for` 循环体的 `continue` 里更容易讲清楚——这一点是换图真正的收益，
不是"用上了框架"。

关于 checkpoint（为什么默认不开）
--------------------------------
LangGraph 的 checkpointer 用于**中断与续跑**（比如等人审批、进程重启后继续）。
我们这一轮没有这些场景：一次运行必然在 max_steps 内终止，状态也不跨请求。
开了 checkpointer 只会让每次运行的内存留到进程结束（MemorySaver 没有回收接口），
所以默认 `checkpointer=None`，需要时由构造参数注入。
这是"这个能力现在没用"而不是"这个能力做不到"——测试里有一条用
MemorySaver 跑的用例，证明它能接上。
"""
from __future__ import annotations

import inspect
import time
import uuid
from typing import TYPE_CHECKING, Any, Callable

from ..contracts.pipeline import Edge, GraphSpec, NodeSpec, QueryOutcome
from ..contracts.session import SessionState
from .machine import AgentCore, Route, RunConfig, RunContext

if TYPE_CHECKING:  # 仅用于注解；运行期不依赖 langchain_core
    from langchain_core.runnables import RunnableConfig

__all__ = [
    "build_langgraph", "build_agent_graph_spec", "LangGraphRuntime",
    "langgraph_available", "AGENT_GRAPH_NAME",
]

AGENT_GRAPH_NAME = "agent-loop"

# 图状态里放 RunContext 的键。**不放进 state**：会话与工具上下文
# 是应用层对象，混进图状态会让 checkpoint 无法序列化。
_RCTX_KEY = "rctx"


def langgraph_available() -> bool:
    """langgraph 是否可用（可选依赖，没装时退化到顺序执行器）。"""
    try:
        import langgraph.graph  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


# --------------------------------------------------------------- 通用编译器
def build_langgraph(spec: GraphSpec, nodes: dict[str, Any], *,
                    entry: str | None = None,
                    router: Callable[[dict[str, Any]], str] | None = None,
                    state_schema: Any = None,
                    checkpointer: Any = None,
                    route_key: str = "route") -> Any:
    """把 `GraphSpec` 编译成 LangGraph 图。

    `nodes` 是 名字 → 节点对象（有 `.run(state)`）或可调用对象的映射。
    节点函数可以只收 `state`，也可以再收一个 `config`——后者用于拿
    每次运行特有的上下文（我们用它传 `RunContext`），这样不必把
    不可序列化的对象塞进图状态。

    边：
        `Edge(source, target)`                  无条件
        `Edge(source, target, condition="x")`   条件：节点把 `state["route"]`
                                                设成 "x" 时走这条

    同一节点上混用无条件边与条件边会被拒绝——那种图的语义是模糊的
    （"永远走"和"满足条件才走"同时成立时，到底走几条？）。
    """
    try:
        from langgraph.graph import END, StateGraph
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "未安装 langgraph，无法使用图执行器；"
            "请 pip install langgraph 或改用 NL2SQL_RUNTIME=loop"
        ) from e

    if spec is None or not spec.nodes:
        raise ValueError("GraphSpec 为空，无法编译")
    names = [n.name for n in spec.nodes]
    missing = [n for n in names if n not in nodes]
    if missing:
        raise ValueError(f"以下节点缺少实现: {missing}")
    if len(set(names)) != len(names):
        raise ValueError(f"节点名重复: {names}")

    schema = state_schema if state_schema is not None else dict
    graph = StateGraph(schema)
    for spec_node in spec.nodes:
        graph.add_node(spec_node.name, _wrap_node(nodes[spec_node.name]))

    # ---- 按源节点归组边
    unconditional: dict[str, list[str]] = {}
    conditional: dict[str, dict[str, str]] = {}
    for edge in spec.edges:
        if edge.source not in names:
            raise ValueError(f"边引用了不存在的节点: {edge.source}")
        target = END if edge.target.lower() in ("end", "__end__") else edge.target
        if target is not END and target not in names:
            raise ValueError(f"边引用了不存在的节点: {edge.target}")
        if edge.condition is None:
            unconditional.setdefault(edge.source, []).append(target)
        else:
            conditional.setdefault(edge.source, {})[edge.condition] = target

    both = sorted(set(unconditional) & set(conditional))
    if both:
        raise ValueError(
            f"节点 {both} 上同时有无条件边和条件边，语义不明确；"
            f"请只保留一种，或把无条件边改成一个显式条件"
        )

    if entry is None:
        targets = {t for ts in unconditional.values() for t in ts if t is not END}
        targets |= {t for m in conditional.values() for t in m.values() if t is not END}
        starts = [n for n in names if n not in targets]
        entry = starts[0] if starts else names[0]
    if entry not in names:
        raise ValueError(f"入口节点不存在: {entry}")
    graph.set_entry_point(entry)

    for source, targets in unconditional.items():
        if len(targets) > 1:
            raise ValueError(
                f"节点 {source} 有多条无条件边 {targets}；"
                f"用条件边来表达分支"
            )
        graph.add_edge(source, targets[0])

    route_fn = router or (lambda s: s.get(route_key, ""))
    for source, mapping in conditional.items():
        graph.add_conditional_edges(source, route_fn, mapping)

    return graph.compile(checkpointer=checkpointer)


def _wrap_node(node: Any) -> Callable[..., Any]:
    """把一个节点对象包成 LangGraph 能调的函数。

    LangGraph 通过**函数签名**判断要不要传 config：签名里有一个
    名为 `config` 的参数才传。所以这里需要显式分两种形态，
    否则节点拿不到本轮上下文。
    """
    fn = node.run if hasattr(node, "run") else node
    try:
        params = [
            p for p in inspect.signature(fn).parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        wants_config = len(params) >= 2
    except (TypeError, ValueError):  # 内建函数等拿不到签名的情形
        wants_config = False

    if wants_config:
        # 注意这个注解：LangGraph **按签名注解**判断要不要注入 config，
        # 而且只认 `RunnableConfig` / `Optional[RunnableConfig]` 这几种写法。
        # 写成 `Any` 会被当成"这个函数不要 config"，于是调用时少传参数、
        # 报 `missing 1 required positional argument: 'config'`。
        # 本模块有 `from __future__ import annotations`，注解是字符串，
        # 正好命中它接受的字符串形式。运行期不需要真的 import。
        def _call(state: Any, config: RunnableConfig) -> Any:  # noqa: F821
            return fn(state, config)
    else:
        def _call(state: Any) -> Any:
            return fn(state)
    _call.__name__ = getattr(fn, "__name__", "node")
    return _call


# --------------------------------------------------------------- agent 图
def build_agent_graph_spec() -> GraphSpec:
    """agent 循环的图定义。**纯数据**，可以展示、可以改、可以被测试断言。"""
    nodes = [
        NodeSpec(name="decide", kind="custom", label="问模型下一步"),
        NodeSpec(name="review", kind="custom", label="复核并顶回（护栏）"),
        NodeSpec(name="act", kind="custom", label="执行工具"),
        NodeSpec(name="finish", kind="custom", label="收尾出结果"),
    ]
    edges = [
        Edge(source="decide", target="review", condition=Route.REVIEW),
        Edge(source="decide", target="finish", condition=Route.EXHAUSTED),
        Edge(source="review", target="decide", condition=Route.RETRY),
        Edge(source="review", target="act", condition=Route.TOOL),
        Edge(source="review", target="finish", condition=Route.FINAL),
        Edge(source="review", target="finish", condition=Route.CLARIFY),
        Edge(source="act", target="decide", condition=Route.CONTINUE),
        Edge(source="act", target="finish", condition=Route.STOP),
        Edge(source="finish", target="__end__"),
    ]
    return GraphSpec(name=AGENT_GRAPH_NAME, nodes=nodes, edges=edges, version="v1")


class _AgentNode:
    """把 `AgentCore` 的一个阶段包成图节点。

    节点只做两件事：调 core、把路由结果写进 `state["route"]`。
    **不含任何业务判断**——判断都在 core 里，两个执行器共享。
    """

    def __init__(self, name: str, label: str,
                 impl: Callable[[dict[str, Any], RunContext], str]) -> None:
        self.spec = NodeSpec(name=name, kind="custom", label=label)
        self._impl = impl

    def run(self, state: dict[str, Any] | None,
            config: Any = None) -> dict[str, Any]:
        rctx = _rctx_from(config)
        new_state = dict(state or {})
        # 保证子结构存在：从 checkpoint 恢复出来的状态可能是残缺的
        new_state.setdefault("history", [])
        new_state.setdefault("notes", [])
        new_state.setdefault("trace", {})
        new_state.setdefault("seen", {})
        new_state.setdefault("used", {})
        new_state["route"] = self._impl(new_state, rctx)
        return new_state


def _rctx_from(config: Any) -> RunContext:
    if not isinstance(config, dict):
        raise RuntimeError("图执行器需要 config['configurable']['rctx']")
    rctx = (config.get("configurable") or {}).get(_RCTX_KEY)
    if not isinstance(rctx, RunContext):
        raise RuntimeError("图执行器需要 config['configurable']['rctx']")
    return rctx


class LangGraphRuntime:
    """LangGraph 执行器。

    对外接口与 `AgentRuntime` 一致（`run / available / set_system_prompt /
    set_schema_brief / config / registry`），所以上层不需要知道自己拿的是哪个。
    """

    name = "langgraph"

    def __init__(self, *, provider: Any, registry: Any,
                 config: RunConfig | None = None,
                 capabilities: tuple[Any, ...] = (),
                 system_prompt: str = "",
                 schema_brief: str = "",
                 on_event: Callable[[dict[str, Any]], None] | None = None,
                 on_tool_call: Callable[[dict[str, Any]], None] | None = None,
                 checkpointer: Any = None,
                 recursion_limit: int | None = None) -> None:
        self._core = AgentCore(
            provider=provider, registry=registry, config=config,
            capabilities=capabilities, system_prompt=system_prompt,
            schema_brief=schema_brief, on_event=on_event,
            on_tool_call=on_tool_call,
        )
        self._checkpointer = checkpointer
        self._app: Any = None
        # 图执行有自己的递归上限（LangGraph 默认 25）。一次运行最多
        # max_steps 次 decide，每次最多 2 个节点，所以 2*max_steps+5 够用。
        # **不设这个会踩坑**：把 max_steps 调大到 20 时，图会先于我们的
        # 步数上限报 GraphRecursionError，用户看到的是框架内部错误。
        self._recursion_limit = recursion_limit

    # ------------------------------------------------------------ 透传
    @property
    def core(self) -> AgentCore:
        return self._core

    @property
    def config(self) -> RunConfig:
        return self._core.config

    @property
    def registry(self) -> Any:
        return self._core.registry

    @property
    def graph_spec(self) -> GraphSpec:
        return build_agent_graph_spec()

    def available(self) -> bool:
        return self._core.available() and langgraph_available()

    def set_system_prompt(self, prompt: str) -> None:
        self._core.set_system_prompt(prompt)

    def set_schema_brief(self, brief: str) -> None:
        self._core.set_schema_brief(brief)

    def set_on_event(self,
                     cb: Callable[[dict[str, Any]], None] | None) -> None:
        self._core.set_on_event(cb)

    # ------------------------------------------------------------ 编译
    def compile(self) -> Any:
        """编译图（惰性，只做一次）。"""
        if self._app is None:
            core = self._core
            nodes = {
                "decide": _AgentNode("decide", "问模型下一步", core.decide),
                "review": _AgentNode("review", "复核并顶回", core.review),
                "act": _AgentNode("act", "执行工具", core.act),
                "finish": _AgentNode(
                    "finish", "收尾出结果",
                    lambda state, rctx: (core.finish(state, rctx), "end")[1]),
            }
            self._app = build_langgraph(
                build_agent_graph_spec(), nodes,
                checkpointer=self._checkpointer,
            )
        return self._app

    # ------------------------------------------------------------ 主流程
    def run(self, question: str, *, session: SessionState,
            resources: dict[str, Any] | None = None,
            on_event: Callable[[dict[str, Any]], None] | None = None
            ) -> QueryOutcome:
        t0 = time.perf_counter()

        if not self._core.available():
            return _llm_unavailable(question, session)
        if not langgraph_available():
            return QueryOutcome(
                status="error", question=question,
                source_id=session.source_id, error_code="RUNTIME_UNAVAILABLE",
                message="未安装 langgraph，无法使用图执行器",
                suggestions=["pip install langgraph", "或设 NL2SQL_RUNTIME=loop"],
            )

        app = self.compile()
        core = self._core
        state, rctx = core.begin(question, session=session,
                                 resources=resources, on_event=on_event)

        limit = self._recursion_limit or (2 * core.config.max_steps + 5)
        cfg = {
            "configurable": {_RCTX_KEY: rctx,
                             "thread_id": f"run-{uuid.uuid4().hex[:12]}"},
            "recursion_limit": limit,
        }
        final_state = app.invoke(state, cfg)
        if not isinstance(final_state, dict):  # 理论上不会，兜底不掩盖
            final_state = state
        return core.to_outcome(
            final_state, rctx,
            elapsed_ms=int((time.perf_counter() - t0) * 1000))

    # ------------------------------------------------------------ 观测
    def describe(self) -> dict[str, Any]:
        """图的自述：节点、边、入口。管理界面与测试都用它。"""
        spec = build_agent_graph_spec()
        return {
            "name": spec.name,
            "version": spec.version,
            "nodes": [{"name": n.name, "label": n.label} for n in spec.nodes],
            "edges": [
                {"source": e.source, "target": e.target,
                 "condition": e.condition}
                for e in spec.edges
            ],
        }


def _llm_unavailable(question: str, session: SessionState) -> QueryOutcome:
    return QueryOutcome(
        status="error", question=question,
        source_id=session.source_id, error_code="LLM_UNAVAILABLE",
        message="模型不可用，无法运行智能体循环",
        suggestions=["配置 NL2SQL_LLM_API_KEY，或改用 rule 规划器"],
    )
