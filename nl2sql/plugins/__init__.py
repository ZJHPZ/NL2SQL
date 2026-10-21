"""插件可以依赖的**稳定接口**。

这是插件与核心之间的契约面。插件**只允许** import 本模块，
不得 import `nl2sql` 的其它子模块——层检查会强制这条规则
（见 `scripts/check_layers.py`）。

为什么需要单独一层
------------------
如果插件可以直接 import 核心内部任意模块，那么：
    * 核心重构会破坏所有插件（无法演进）
    * 沙箱形同虚设（插件能绕过能力检查直接拿适配器）
    * 能力白名单失去意义（插件自己就能调用底层）

因此这里只导出"插件确实需要"的东西，并保持刻意小：
    ToolSpec / ToolResult / ToolContext  —— 写工具要用
    Capability / CAPABILITIES            —— 声明能力要用
    ToolRegistry                         —— 注册工具要用
    PluginManifest                       —— 声明元信息要用
"""
from __future__ import annotations

from ..contracts.tool import (
    CAPABILITIES,
    Capability,
    Tool,
    ToolContext,
    ToolResult,
    ToolSpec,
)

__all__ = [
    "Tool",
    "ToolSpec",
    "ToolResult",
    "ToolContext",
    "Capability",
    "CAPABILITIES",
    "ToolRegistry",
]


class ToolRegistry:
    """工具注册表（实例级，非模块级全局）。

    每个 `NL2SQL` 实例持有自己的注册表，理由与组件注册表一致：
    多实例（测试、多数据源、多工作区）互不污染，且核心无全局状态。

    `register` 会做**能力校验**：工具声明的能力必须已被授予，
    否则拒绝注册。这条检查放在注册时而不是调用时，
    是为了让配置错误在启动阶段就暴露，而不是等用户问到那一句才炸。
    """

    def __init__(self, *, granted: tuple[Capability, ...] = (),
                 origin: str = "core") -> None:
        self._tools: dict[str, Tool] = {}
        self._origin: dict[str, str] = {}
        self._granted = {c.name for c in granted}
        self._origin_default = origin

    # ------------------------------------------------------------ 注册
    def register(self, tool: Tool, *, origin: str | None = None,
                 strict_capability: bool = True) -> None:
        spec = tool.spec
        if not spec.name or not spec.name.strip():
            raise ValueError("工具必须有 name")
        if spec.name in self._tools:
            raise ValueError(f"工具已存在同名实现: {spec.name}")

        if strict_capability:
            missing = [r for r in spec.requires if r not in self._granted]
            if missing:
                raise PermissionError(
                    f"工具 {spec.name} 需要能力 {missing}，但未授予。"
                    f"已授予: {sorted(self._granted)}"
                )
        self._tools[spec.name] = tool
        self._origin[spec.name] = origin or self._origin_default

    def grant(self, capability: str, *, granted_by: str = "config") -> None:
        """追加授予能力（会影响后续注册的校验）。"""
        self._granted.add(Capability(name=capability, granted_by=granted_by).name)

    # ------------------------------------------------------------ 查询
    def get(self, name: str) -> Tool:
        if name not in self._tools:
            avail = ", ".join(sorted(self._tools)) or "（空）"
            raise KeyError(f"未注册的工具: {name}；可用: {avail}")
        return self._tools[name]

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[Tool]:
        return [self._tools[n] for n in self.names()]

    def specs(self) -> list[ToolSpec]:
        return [t.spec for t in self.all()]

    def origin_of(self, name: str) -> str:
        return self._origin.get(name, "unknown")

    def describe(self) -> list[dict[str, object]]:
        return [
            {
                "name": t.spec.name,
                "description": t.spec.description,
                "kind": t.spec.kind,
                "requires": list(t.spec.requires),
                "requires_approval": t.spec.requires_approval,
                "origin": self._origin.get(t.spec.name, "unknown"),
            }
            for t in self.all()
        ]
