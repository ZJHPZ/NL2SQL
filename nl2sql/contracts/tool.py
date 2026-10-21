"""工具契约：agent 的"手脚"。

为什么需要这一层
----------------
当前项目是「LLM 单次调用 + 硬编码流水线」：`plan → compile → guard → execute`
顺序写死，`compile` 只调用一次，失败就结束。

**那不是 agent。** agent 与 pipeline 的分野只有一句话：

    工具执行的结果要回到模型眼前，模型据此决定下一步。

有了这条，才可能出现：生成的 SQL 报错 → 模型看到 `no such column: c.region`
→ 决定先调 `schema.describe` 查真实字段 → 重写 SQL。

设计要点
--------
1. **`ToolContext` 是能力的显式传递**：工具不能自己去摸全局，
   只能拿到被授予的东西。这是沙箱边界成立的前提。
2. **`ToolResult` 能表达"部分成功"与"给模型的提示"**：
   因为循环的下一步就靠它，错误信息必须是结构化的、可读的、可行动的。
3. **不给工具自由 import 核心的权力**：插件只能依赖 `nl2sql.plugin_api`。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

__all__ = [
    "ToolKind",
    "ToolSpec",
    "ToolResult",
    "ToolContext",
    "Tool",
    "Capability",
    "CAPABILITIES",
]

# 工具的副作用类型。**这决定它是否需要审批**，因此必须显式声明，
# 不能靠调用方猜。
ToolKind = Literal["read", "write", "none"]

# 能力清单：插件必须声明它要什么，运行时按声明授予。
# 不声明就拿不到——这是插件沙箱的核心机制。
CAPABILITIES: dict[str, str] = {
    "source.read": "只读访问数据源（执行 SELECT、读元数据）",
    "source.write": "写数据源（执行 INSERT/UPDATE/DELETE/DDL）——需审批",
    "semantic.read": "读取语义模型（指标、维度、口径）",
    "semantic.write": "修改语义模型（新增/覆盖指标）",
    "knowledge.read": "检索知识库",
    "knowledge.write": "写入知识库条目",
    "cache.read": "读取结果缓存",
    "cache.write": "写入结果缓存",
    "session.read": "读取当前会话上下文",
    "session.write": "写入会话上下文",
    "llm.invoke": "调用大模型",
    "clarify.ask": "向用户提问并等待回答（回合制）",
    "ui.render": "向界面输出额外渲染内容（如图表）",
}


@dataclass(frozen=True)
class Capability:
    """一项被授予的能力。`granted_by` 用于审计——谁给的权限要能追溯。"""

    name: str
    granted_by: str = "config"
    scope: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.name not in CAPABILITIES:
            raise ValueError(
                f"未知能力: {self.name}；可用: {', '.join(sorted(CAPABILITIES))}"
            )


@dataclass(frozen=True)
class ToolSpec:
    """工具声明。**它同时是给模型看的说明书**，因此 description 要写清楚
    "什么时候该用它"，而不只是"它是什么"。
    """

    name: str
    description: str
    # JSON Schema 形态的参数定义，模型据此生成调用参数
    params: dict[str, Any] = field(default_factory=dict)
    requires: tuple[str, ...] = ()          # 需要的能力
    kind: ToolKind = "read"
    requires_approval: bool = False
    # 是否把结果回灌给模型。少数工具（如纯记录类）可设为 False
    feed_back: bool = True
    # 便于模型理解的使用示例
    examples: tuple[dict[str, Any], ...] = ()

    def to_json_schema(self) -> dict[str, Any]:
        """转成模型能用的 function-calling schema。"""
        properties = self.params.get("properties", {}) if self.params else {}
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": self.params.get("required", []) if self.params else [],
            },
        }


@dataclass
class ToolResult:
    """工具执行结果。

    `ok=False` 时**不要把原始异常直接丢给模型**：
    要给出"哪里错了 + 建议怎么改"，否则模型只能瞎猜。
    这与界面层的错误体验是同一条原则。
    """

    ok: bool
    data: Any = None
    error: str | None = None
    # 给模型的提示：怎么修正、下一步可考虑什么
    hint: str = ""
    # 结构化补充信息（如候选字段），便于模型精确使用
    candidates: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    # 工具是否要求"暂停并问用户"
    needs_clarification: bool = False
    question: str = ""
    options: list[str] = field(default_factory=list)

    def to_observation(self) -> str:
        """渲染成回灌给模型的**观察文本**。

        刻意做成简短结构化，而不是 JSON dump：
        观察文本会进上下文，越长越贵，也越容易淹没关键信息。
        """
        if self.needs_clarification:
            return f"[需要澄清] {self.question}"
        if not self.ok:
            parts = [f"[失败] {self.error or '未知错误'}"]
            if self.hint:
                parts.append(f"建议: {self.hint}")
            if self.candidates:
                parts.append("候选: " + "、".join(self.candidates[:8]))
            return "\n".join(parts)
        text = self.data if isinstance(self.data, str) else _compact(self.data)
        return f"[成功] {text}"


def _compact(value: Any, *, max_len: int = 1200, preview_rows: int = 8) -> str:
    """把结果压成紧凑文本，避免超长结果淹没上下文。

    关于"显示前几行"的措辞（踩过的坑）：
        最初写"前3行"，结果模型误以为**数据被截断了**，
        于是回答里说"工具只展示了前 3 行，未按降序排列"——
        它把展示预览当成了结果本身有问题。
        现在明确写出总行数、并说明整份结果已经拿到、预览与排序无关。

    注意：必须**严格校验类型**再切片。曾经因为只检查了 key 是否存在，
    遇到 `{"columns": [...], "rows": 5}` 这种形状就在 `rows[:3]` 上崩了——
    而工具结果的形状由插件决定，不能假设。
    """
    import json

    if isinstance(value, dict):
        cols = value.get("cols") if "cols" in value else value.get("columns")
        rows = value.get("rows")
        if isinstance(rows, list):
            total = value.get("row_count", len(rows))
            head = rows[:preview_rows]
            if value.get("truncated"):
                tail_note = f"（结果已被行数上限截断，仅 {total} 行）"
            elif total > len(head):
                tail_note = (f"（此处仅为预览，完整 {total} 行已获取；"
                             f"回答时请基于全部行，不要认为数据缺失）")
            else:
                tail_note = "（这是完整结果）"
            text = (f"列: {list(cols) if isinstance(cols, list) else cols}\n"
                    f"总行数: {total} {tail_note}\n"
                    f"数据: {json.dumps(head, ensure_ascii=False, default=str)}")
            return text[:max_len]
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text[:max_len]


class ToolContext:
    """工具运行上下文：**能力的显式载体**。

    工具只能通过它取用被授予的资源，不能 import 核心内部模块、
    也不能访问模块级全局。这样"插件沙箱"才是真的。

    实现上它是一个显式依赖包，而不是一个能翻遍全局的对象。
    """

    __slots__ = ("_caps", "_resources", "session_id", "source_id",
                 "now", "question", "trace")

    def __init__(self, *, capabilities: tuple[Capability, ...] = (),
                 session_id: str = "", source_id: str = "",
                 now: str | None = None, question: str = "") -> None:
        self._caps = {c.name for c in capabilities}
        self._resources: dict[str, Any] = {}
        self.session_id = session_id
        self.source_id = source_id
        self.now = now
        self.question = question
        self.trace: list[dict[str, Any]] = []

    # ------------------------------------------------------------ 能力
    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset(self._caps)

    def has(self, capability: str) -> bool:
        return capability in self._caps

    def require(self, capability: str) -> None:
        if capability not in self._caps:
            raise PermissionError(
                f"缺少能力 {capability}；工具未声明或未被授予。"
                f"已授予: {sorted(self._caps)}"
            )

    # ------------------------------------------------------------ 资源
    def provide(self, name: str, resource: Any) -> None:
        """由运行时注入资源（适配器、语义模型、缓存…）。"""
        self._resources[name] = resource

    def get(self, name: str, capability: str) -> Any:
        """取用资源。**必须同时声明能力**，避免"忘记检查"这类疏漏。"""
        self.require(capability)
        if name not in self._resources:
            raise KeyError(f"资源未提供: {name}")
        return self._resources[name]

    def maybe(self, name: str) -> Any | None:
        return self._resources.get(name)

    def note(self, step: str, payload: dict[str, Any]) -> None:
        self.trace.append({"step": step, **payload})


@runtime_checkable
class Tool(Protocol):
    """工具契约。

    实现要求：
    * 无模块级可变状态；一切依赖来自 `ToolContext`
    * `run` 不抛异常，失败要返回 `ToolResult(ok=False, ...)`——
      因为 agent 循环需要看到失败并据此决策，异常会打断循环
    * 参数校验在 `run` 内完成，缺参数返回带 `hint` 的失败结果
    """

    spec: ToolSpec

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...
