"""插件加载：扫描 `plugins/`、解析声明、注册工具、按声明授予能力。

为什么要有插件机制
------------------
"一切皆插件"不是口号，它需要三个具体机制：
    1. **发现**：启动时扫描目录，而不是写死在代码里
    2. **声明**：插件自己说要什么能力（`plugin.toml`），而不是运行时随便拿
    3. **隔离**：插件只能依赖 `nl2sql.plugin_api`，不能 import 核心私有模块

第 3 条是关键。没有它，"插件"就只是"把代码放在另一个文件夹里"，
沙箱、只读边界、审计全都会失效。

安全边界（必须明说）
--------------------
**不提供"在进程内执行用户任意代码"的能力。**
自定义逻辑只能通过：注册工具（走能力白名单）+ 改 Prompt（纯声明）。
允许 `eval`/`exec` 用户代码等于取消所有沙箱，这个口子不能开。
"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..contracts.tool import Capability, Tool, ToolSpec
from ..errors import NL2SQLError

__all__ = ["PluginManifest", "LoadedPlugin", "PluginLoader", "PluginError",
           "read_toml", "discover_manifests"]


def read_toml(path: str | Path) -> dict[str, Any]:
    """读 TOML 文件（公开包装，供管理界面读取插件声明）。"""
    return _read_toml(Path(path))


def discover_manifests(
    plugins_dir: str | Path,
) -> tuple[list["PluginManifest"], list[str]]:
    """只**发现**插件，不导入其代码。

    管理界面需要展示"磁盘上有哪些插件"——包括未启用的。
    这里刻意不导入模块：**列个清单不该有执行别人代码的副作用**。
    """
    loader = PluginLoader()
    manifests = loader.discover(plugins_dir)
    return manifests, list(loader.errors)


class PluginError(NL2SQLError):
    """插件声明或加载失败。"""


@dataclass
class PluginManifest:
    """`plugin.toml` 的解析结果。"""

    name: str
    version: str = "0.1.0"
    description: str = ""
    author: str = ""
    entry: str = "plugin.py"                 # 入口模块文件名
    requires: tuple[str, ...] = ()           # 声明需要的核心能力
    tool_names: tuple[str, ...] = ()
    enabled: bool = True
    path: Path | None = None


@dataclass
class LoadedPlugin:
    """加载成功的插件。`tools` 是它贡献的工具。"""

    manifest: PluginManifest
    tools: list[Tool] = field(default_factory=list)
    capabilities: list[Capability] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _read_toml(path: Path) -> dict[str, Any]:
    """读 TOML。优先标准库 `tomllib`（3.11+），退化用 `tomli`/手写。"""
    try:
        import tomllib  # type: ignore[import-not-found]

        with open(path, "rb") as f:
            return tomllib.load(f)
    except ImportError:
        pass
    for mod_name in ("tomli", "toml"):
        try:
            mod = importlib.import_module(mod_name)
            with open(path, encoding="utf-8") as f:
                return (mod.load(f) if mod_name == "tomli" else mod.load(f))
        except ImportError:
            continue
    raise PluginError(
        f"无法解析 {path}：当前 Python 没有 tomllib（需 3.11+），"
        f"也未安装 tomli/toml"
    )


class PluginLoader:
    """扫描并加载插件。

    加载失败**不阻断启动**——个人场景下用户可能刚写了一半插件，
    因为一个坏插件整个工具起不来是无法接受的。失败会记进 `errors`。
    """

    def __init__(self, *, core_version: str = "0.1.0") -> None:
        self._core_version = core_version
        self.errors: list[str] = []

    # ------------------------------------------------------------ 扫描
    def discover(self, plugins_dir: str | Path) -> list[PluginManifest]:
        root = Path(plugins_dir)
        if not root.is_dir():
            return []
        manifests: list[PluginManifest] = []
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith((".", "_")):
                continue
            toml_path = child / "plugin.toml"
            if not toml_path.is_file():
                self.errors.append(f"{child.name}: 缺少 plugin.toml，跳过")
                continue
            try:
                manifests.append(self._parse_manifest(toml_path, child))
            except PluginError as e:
                self.errors.append(str(e))
        return manifests

    def _parse_manifest(self, toml_path: Path, base: Path) -> PluginManifest:
        try:
            raw = _read_toml(toml_path)
        except PluginError:
            raise
        except Exception as e:  # noqa: BLE001
            raise PluginError(f"{base.name}: plugin.toml 解析失败: {e}") from e

        plugin = raw.get("plugin") or {}
        name = str(plugin.get("name") or base.name).strip()
        if not name:
            raise PluginError(f"{base.name}: plugin.toml 缺少 [plugin].name")

        requires = plugin.get("requires") or raw.get("capabilities", {}).get(
            "requires", []
        )
        if isinstance(requires, str):
            requires = [requires]
        tools = plugin.get("tools") or raw.get("tools", {}).get("names", [])
        if isinstance(tools, str):
            tools = [tools]

        return PluginManifest(
            name=name,
            version=str(plugin.get("version", "0.1.0")),
            description=str(plugin.get("description", "")),
            author=str(plugin.get("author", "")),
            entry=str(plugin.get("entry", "plugin.py")),
            requires=tuple(str(r) for r in requires),
            tool_names=tuple(str(t) for t in tools),
            enabled=bool(plugin.get("enabled", True)),
            path=base,
        )

    # ------------------------------------------------------------ 加载
    def load(self, manifest: PluginManifest) -> LoadedPlugin:
        """加载单个插件，返回它贡献的工具与已授予能力。"""
        assert manifest.path is not None
        result = LoadedPlugin(manifest=manifest)

        if not manifest.enabled:
            result.warnings.append("插件已禁用，跳过加载")
            return result

        # 能力校验：未声明的能力一律不授予（核心拒绝越权）
        caps: list[Capability] = []
        for cap_name in manifest.requires:
            try:
                caps.append(Capability(name=cap_name, granted_by=f"plugin:{manifest.name}"))
            except ValueError as e:
                raise PluginError(f"{manifest.name}: {e}") from e
        result.capabilities = caps

        entry_path = manifest.path / manifest.entry
        if not entry_path.is_file():
            raise PluginError(f"{manifest.name}: 入口文件不存在 {entry_path}")

        module = self._import_module(entry_path, manifest.name)
        register = getattr(module, "register", None)
        if not callable(register):
            raise PluginError(
                f"{manifest.name}: {manifest.entry} 必须定义 "
                f"`def register() -> list[Tool]`"
            )
        try:
            tools = register()
        except Exception as e:  # noqa: BLE001
            raise PluginError(f"{manifest.name}: register() 执行失败: {e}") from e

        if not isinstance(tools, (list, tuple)):
            raise PluginError(f"{manifest.name}: register() 必须返回工具列表")

        for t in tools:
            spec = getattr(t, "spec", None)
            if not isinstance(spec, ToolSpec):
                raise PluginError(
                    f"{manifest.name}: 返回的对象缺少合法的 ToolSpec"
                )
            # 插件声明的能力必须覆盖其工具所需能力
            missing = [r for r in spec.requires if r not in manifest.requires]
            if missing:
                raise PluginError(
                    f"{manifest.name}: 工具 {spec.name} 需要能力 {missing}，"
                    f"但插件未在 plugin.toml 的 requires 中声明。"
                    f"（能力必须显式声明才能被授予）"
                )
            result.tools.append(t)

        if manifest.tool_names:
            declared = set(manifest.tool_names)
            actual = {t.spec.name for t in result.tools}
            extra = actual - declared
            if extra:
                result.warnings.append(
                    f"plugin.toml 未声明这些工具，但仍会注册: {sorted(extra)}"
                )
        return result

    @staticmethod
    def _import_module(path: Path, plugin_name: str) -> Any:
        """以独立模块名导入插件入口。

        用独立的模块名（`nl2sql_plugin_<name>`）而不是裸文件名，
        避免两个插件因同名文件互相覆盖 `sys.modules`。
        """
        mod_name = f"nl2sql_plugin_{plugin_name}"
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            raise PluginError(f"无法为 {path} 建立模块规格")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as e:  # noqa: BLE001
            sys.modules.pop(mod_name, None)
            raise PluginError(f"导入插件 {plugin_name} 失败: {e}") from e
        return module

    # ------------------------------------------------------------ 批量
    def load_all(self, plugins_dir: str | Path) -> list[LoadedPlugin]:
        loaded: list[LoadedPlugin] = []
        for manifest in self.discover(plugins_dir):
            try:
                loaded.append(self.load(manifest))
            except PluginError as e:
                # 单个插件坏掉不该让整个工具起不来
                self.errors.append(str(e))
        return loaded
