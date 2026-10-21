"""会话契约：多轮上下文与回合制澄清。

为什么需要会话
--------------
`clarify.ask`（agent 主动追问）会打破"一问一答"的同步模型：
agent 跑到一半发现缺信息，必须停下来等用户。

两种实现方式：
    真暂停    运行时挂起、持久化中间状态、用户回答后从断点恢复
    回合制    立即返回"需要澄清"，用户回答作为**下一回合**携带上下文重开

本项目选**回合制**。理由：
    * 无需可恢复状态机，实现可靠得多
    * 天然支持"用户改主意、补充信息、换个说法"
    * 人跟人对话本来就是回合制的

代价：需要保存会话上下文，让下一轮能看到之前问了什么、答了什么、试过哪些工具。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

__all__ = ["TurnRole", "Turn", "SessionState", "SessionStore"]

TurnRole = Literal["user", "assistant", "tool", "clarify"]


@dataclass
class Turn:
    """一轮对话中的一个事件。

    `tool` 轮用于记录"调了哪个工具、参数是什么、观察到了什么"——
    这是 agent 循环的短期记忆，也是"为什么给出这个答案"的追溯依据。
    """

    role: TurnRole
    content: str = ""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    ok: bool | None = None
    created_at: str = ""

    def render(self) -> str:
        """渲染成给模型看的文本行。"""
        if self.role == "user":
            return f"用户: {self.content}"
        if self.role == "assistant":
            return f"助手: {self.content}"
        if self.role == "clarify":
            return f"助手(追问): {self.content}"
        status = "" if self.ok is None else ("成功" if self.ok else "失败")
        return f"工具[{self.tool_name}]{status}: {self.content}"


@dataclass
class SessionState:
    """一个会话的完整状态。"""

    session_id: str
    source_id: str = ""
    turns: list[Turn] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    # 待回答的澄清问题；回答后清空
    pending_question: str = ""
    pending_options: list[str] = field(default_factory=list)
    # 业务上下文（相对时间锚点等）——必须随会话保留，
    # 否则用户回答澄清问题后，「本月」会变成另一个月份
    now: str | None = None
    # 供界面展示的最近一次结果摘要
    last_status: str = ""

    def recent(self, n: int = 20) -> list[Turn]:
        return self.turns[-n:]

    def render_context(self, limit: int = 16) -> str:
        """渲染最近若干轮，供模型参考。"""
        return "\n".join(t.render() for t in self.recent(limit))

    def add(self, turn: Turn) -> None:
        self.turns.append(turn)


@runtime_checkable
class SessionStore(Protocol):
    """会话持久化契约。

    持久化是**必需**的而非可选：回合制澄清要求下一轮能读到上一轮，
    而请求之间进程可能重启（开发时尤其常见）。
    """

    def load(self, session_id: str) -> SessionState | None: ...
    def save(self, state: SessionState) -> None: ...
    def delete(self, session_id: str) -> None: ...
    def list_recent(self, *, limit: int = 20) -> list[SessionState]: ...
