"""LLM 契约：可替换的模型提供者。

设计要点
--------
1. **模型只是组件之一**：语义规划既可以由规则完成，也可以由模型完成。
   `Planner` 是个契约，`RulePlanner`（确定性）与 `LLMPlanner`（模型驱动）
   是它的两个实现。这样模型能力与链路正确性可分开度量。
2. **不绑定厂商**：统一走 OpenAI 兼容协议，换厂商只改 base_url 与 model。
3. **同步接口**：核心链路是同步的；异步由壳层包装（避免核心染上 asyncio）。
4. **不读环境变量**：API Key 由壳层读取后显式注入（与 config.py 的约定一致）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence, runtime_checkable

from .semantic import LogicalPlan

__all__ = [
    "ChatMessage",
    "LLMResponse",
    "LLMProvider",
    "Planner",
    "PlannerContext",
]


@dataclass
class ChatMessage:
    role: str          # "system" | "user" | "assistant"
    content: str


@dataclass
class LLMResponse:
    text: str
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    finish_reason: str = ""
    raw: dict | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@runtime_checkable
class LLMProvider(Protocol):
    """模型提供者契约。

    实现要求：
    * 无模块级全局状态；API Key 由实例持有
    * 失败时抛 LLMError，由上层决定降级（例如回退到规则规划）
    * 不负责 Prompt 组装（那是 Planner 的事）
    """

    name: str
    model: str

    def chat(self, messages: Sequence[ChatMessage], *,
             temperature: float = 0.0, max_tokens: int | None = None,
             json_mode: bool = False) -> LLMResponse: ...

    def available(self) -> bool:
        """是否具备调用条件（有 Key、有 base_url 等）。用于启动自检。"""
        ...


@dataclass
class PlannerContext:
    """规划所需的全部上下文，**显式传入**，不读全局、不读系统时间。

    `now` 是相对时间（本月/上月）的锚点。缺了它，同一问题在不同日期
    会得到不同计划——评测不可复现、缓存语义错乱。
    """

    source_id: str
    now: str | None = None
    # 知识库召回结果（文本片段），供 Prompt 拼装
    knowledge: list[str] = field(default_factory=list)
    # 相似的历史 (问题, SQL) 对，作为 few-shot 示例。
    # 这是知识库对准确率的主要贡献路径：模型照着改，而不是从零推导。
    examples: list[tuple[str, str]] = field(default_factory=list)
    # 精简 DDL 或表结构文本，供自由生成使用
    schema_text: str = ""
    locale: str = "zh"


@runtime_checkable
class Planner(Protocol):
    """问题 → 逻辑计划。

    两个实现，用途不同：
        `RulePlanner`  确定性，零模型依赖。衡量**语义层覆盖率**。
        `LLMPlanner`   模型驱动。衡量**模型能力**。

    两者产出同一类型，因此下游（编译/守门/执行/缓存）完全复用。
    """

    name: str

    def resolve(self, question: str, ctx: PlannerContext) -> LogicalPlan: ...
