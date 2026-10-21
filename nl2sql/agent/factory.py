"""执行器工厂：按名字造出 agent 运行时。

为什么要有工厂而不是在 `api.py` 里写 if/else
--------------------------------------------
"用哪个执行器"是一个**配置**，不是一段代码分支。放在一处，三个地方受益：
① `NL2SQL_RUNTIME` 环境变量与 `AgentConfig.runtime` 都走同一条路；
② 降级只写一遍（图执行器要 langgraph，没装就退顺序执行器）；
③ 测试可以对着工厂断言"两个执行器出口一致"，不用各自 import 一遍。

降级必须**留痕**
----------------
`langgraph` 是可选依赖。没装时不能启动失败（那样一个 pip 就能解决的
问题会挡掉整个系统），但也不能**默默**换成顺序执行器——用户以为自己在跑
图执行器，实际没有，这种沉默是最难排查的。所以 `make_runtime` 返回
`(runtime, note)`，`note` 非空就说明降级了，上层把它显示出来。
"""
from __future__ import annotations

from typing import Any

from .graph import LangGraphRuntime, langgraph_available
from .runtime import AgentRuntime

__all__ = ["make_runtime", "available_runtimes", "VALID_RUNTIMES"]

VALID_RUNTIMES = ("auto", "loop", "langgraph")


def available_runtimes() -> dict[str, bool]:
    """哪些执行器现在真的能用。管理界面用它决定显示什么。"""
    return {"loop": True, "langgraph": langgraph_available()}


def make_runtime(name: str | None, **kwargs: Any) -> tuple[Any, str]:
    """按名字造执行器，返回 `(runtime, note)`。

    `note` 非空表示"你要的没给成，给了个能跑的"，必须显示给用户。

    认不出的名字**报错**而不是退回默认值：写错配置却拿到一个"能用"的
    执行器，是把错误藏起来（与 `resolve_secret` 处理认不出的
    `credential_ref` 同一条原则）。
    """
    requested = (name or "auto").strip().lower()
    if requested not in VALID_RUNTIMES:
        raise ValueError(
            f"不认识的执行器 {name!r}；可选 {', '.join(VALID_RUNTIMES)}"
        )

    if requested == "loop":
        return AgentRuntime(**kwargs), ""

    if requested == "langgraph":
        if langgraph_available():
            return LangGraphRuntime(**kwargs), ""
        return AgentRuntime(**kwargs), (
            "已请求 langgraph 执行器但未安装 langgraph，已降级为 loop。"
            "安装：pip install langgraph"
        )

    # auto：装了就用图执行器（这是论文题目里的那套），没装就顺序执行
    if langgraph_available():
        return LangGraphRuntime(**kwargs), ""
    return AgentRuntime(**kwargs), ""
