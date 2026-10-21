"""agent 运行时：顺序执行器。

这是把"LLM 调用"升级为"智能体"的入口。分野只有一句话：

    **工具执行的结果要回到模型眼前，模型据此决定下一步。**

这个文件现在只剩**驱动逻辑**：什么时候调 `decide`、什么时候 `review`、
什么时候 `act`。真正的一步（含 7 道护栏）在 `machine.AgentCore` 里，
LangGraph 执行器用的是同一份——见 `graph.py`。

为什么这样分
------------
第一版把循环体写在这里（约 250 行，7 道护栏全inline）。M2 要加第二个
执行器时有两个选择：

    复制一份循环体    两个执行器各改各的，差异会等到某次改护栏才暴露
    抽出单步状态机    两种调度方式驱动同一份代码

选了后者。代价是多一层间接；收益是**等价性可以被论证**，而不是只被测试。

执行器怎么选
-----------
`NL2SQL_RUNTIME=loop|langgraph`（默认 `loop`）。
两者产出同一个 `QueryOutcome`；切换只影响"谁在驱动"，不影响"做什么"。
"""
from __future__ import annotations

import time
from typing import Any, Callable

from ..contracts.llm import LLMProvider
from ..contracts.pipeline import QueryOutcome
from ..contracts.session import SessionState
from ..contracts.tool import Capability
from ..plugins import ToolRegistry
from .machine import (  # noqa: F401 - 这些名字从这里再导出，保持旧的 import 路径可用
    DECISION_PROMPT,
    AgentCore,
    Decision,
    Route,
    RunConfig,
    RunContext,
    parse_decision,
)

__all__ = ["AgentRuntime", "RunConfig", "Decision", "parse_decision",
           "AgentCore", "RunContext", "Route"]

# 旧名兼容：外部代码若引用过 `runtime._DECISION_PROMPT`，这里仍然可用。
_DECISION_PROMPT = DECISION_PROMPT


class AgentRuntime:
    """顺序执行器（工具循环）。

    依赖全部构造注入，运行时无全局状态。
    与 LangGraph 执行器共享 `AgentCore`，所以护栏、打转检测、结果挑选
    的行为**不可能是两份实现**。
    """

    name = "loop"

    def __init__(self, *, provider: LLMProvider, registry: ToolRegistry,
                 config: RunConfig | None = None,
                 capabilities: tuple[Capability, ...] = (),
                 system_prompt: str = "",
                 schema_brief: str = "",
                 on_event: Callable[[dict[str, Any]], None] | None = None,
                 on_tool_call: Callable[[dict[str, Any]], None] | None = None
                 ) -> None:
        self._core = AgentCore(
            provider=provider, registry=registry, config=config,
            capabilities=capabilities, system_prompt=system_prompt,
            schema_brief=schema_brief, on_event=on_event,
            on_tool_call=on_tool_call,
        )

    # ------------------------------------------------------------ 透传
    @property
    def core(self) -> AgentCore:
        return self._core

    @property
    def config(self) -> RunConfig:
        return self._core.config

    @property
    def registry(self) -> ToolRegistry:
        return self._core.registry

    def available(self) -> bool:
        return self._core.available()

    def set_system_prompt(self, prompt: str) -> None:
        self._core.set_system_prompt(prompt)

    def set_schema_brief(self, brief: str) -> None:
        self._core.set_schema_brief(brief)

    def set_on_event(self,
                     cb: Callable[[dict[str, Any]], None] | None) -> None:
        self._core.set_on_event(cb)

    # ------------------------------------------------------------ 主循环
    def run(self, question: str, *, session: SessionState,
            resources: dict[str, Any] | None = None,
            on_event: Callable[[dict[str, Any]], None] | None = None
            ) -> QueryOutcome:
        """跑一轮 agent。

        返回的 `QueryOutcome` 可能是：
            status="ok"                  已给出答案
            status="need_clarification"  agent 在追问，等用户回答
            status="unresolved"          达到步数上限仍未定论
            status="error"               运行时/模型失败
        """
        t0 = time.perf_counter()

        if not self.available():
            return _llm_unavailable(question, session)

        core = self._core
        state, rctx = core.begin(question, session=session,
                                 resources=resources, on_event=on_event)

        while True:
            if core.decide(state, rctx) == Route.EXHAUSTED:
                break
            route = core.review(state, rctx)
            if route == Route.RETRY:
                continue
            if route in (Route.FINAL, Route.CLARIFY):
                break
            if core.act(state, rctx) == Route.STOP:
                break

        core.finish(state, rctx)
        return core.to_outcome(
            state, rctx, elapsed_ms=int((time.perf_counter() - t0) * 1000))

    # ------------------------------------------------------------ 旧接口
    def _decide(self, history: list[str]) -> Decision:
        """保留旧方法名：单测/调试脚本可能直接调它验证解析。"""
        return self._core._ask_model(history)  # noqa: SLF001


def _llm_unavailable(question: str, session: SessionState) -> QueryOutcome:
    return QueryOutcome(
        status="error", question=question,
        source_id=session.source_id, error_code="LLM_UNAVAILABLE",
        message="模型不可用，无法运行智能体循环",
        suggestions=["配置 NL2SQL_LLM_API_KEY，或改用 rule 规划器"],
    )
