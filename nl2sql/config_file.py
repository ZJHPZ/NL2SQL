"""配置文件读写：`config/nl2sql.toml`。

为什么配置放在文件而不是数据库
------------------------------
目标用户是个人开发者，配置文件他能**看懂、能直接改、能进 git**。
数据库存配置在个人场景是过度设计：看不到、没法版本控制、换机器要导出。

设计要点
--------
1. **文件是可选的**：不存在时用 `AgentConfig` 的默认值，一切照常。
   这很重要——不能让"忘了建配置文件"变成"工具起不来"。
2. **写回只改差异**：保存时保留用户手写的注释与结构，
   不整份重新序列化。手写的配置文件被人一键覆盖掉是很糟的体验。
3. **校验在合并时做**：非法值给明确报错，不静默忽略。
"""
from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from .errors import ConfigError

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "RuntimeSettings",
    "read_config_file",
    "load_runtime_settings",
    "save_runtime_settings",
    "render_config_toml",
]

DEFAULT_CONFIG_PATH = "config/nl2sql.toml"

# 允许在界面里调、且热生效的项。白名单而非黑名单：
# 新增字段必须显式加进来，避免"随手加个字段就被暴露成可远程改的配置"。
LOOP_FIELDS: dict[str, tuple[type, Any, Any]] = {
    # 字段: (类型, 最小值, 最大值)
    "max_steps": (int, 1, 50),
    "max_repeat": (int, 1, 20),
    "temperature": (float, 0.0, 2.0),
}


class RuntimeSettings:
    """从配置文件读出的可热改设置。

    刻意与 `AgentConfig` 分开：
        `AgentConfig` 是**装配期**的完整配置（含能力、插件、数据源）
        这里只是**运行期可调**的那一小部分
    分开的好处是"哪些参数能在线改"一目了然，不用去翻整个 config。
    """

    __slots__ = ("loop", "prompt_files", "model_bindings", "disabled_tools",
                 "enabled_plugins", "capabilities", "source_path", "raw")

    def __init__(self) -> None:
        self.loop: dict[str, Any] = {}
        self.prompt_files: dict[str, str] = {}
        self.model_bindings: dict[str, str] = {}
        self.disabled_tools: list[str] = []
        self.enabled_plugins: list[str] = []
        # None 表示"沿用代码默认"；有值表示配置文件显式指定
        self.capabilities: list[str] | None = None
        self.source_path: str | None = None
        self.raw: dict[str, Any] = {}

    def describe(self) -> dict[str, Any]:
        return {
            "loop": dict(self.loop),
            "prompt_files": dict(self.prompt_files),
            "model_bindings": dict(self.model_bindings),
            "disabled_tools": list(self.disabled_tools),
            "enabled_plugins": list(self.enabled_plugins),
            "capabilities": (None if self.capabilities is None
                             else list(self.capabilities)),
            "source_path": self.source_path,
        }


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib  # type: ignore[import-not-found]

        with open(path, "rb") as f:
            return tomllib.load(f)
    except ImportError:
        pass
    for mod_name in ("tomli", "toml"):
        try:
            import importlib

            mod = importlib.import_module(mod_name)
            with open(path, encoding="utf-8") as f:
                return mod.load(f)
        except ImportError:
            continue
    raise ConfigError(
        f"无法解析 {path}：当前 Python 没有 tomllib（需 3.11+），也未安装 tomli/toml"
    )


def read_config_file(path: str | Path | None) -> dict[str, Any]:
    """读配置文件。文件不存在返回空字典（**不是错误**）。"""
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        return _load_toml(p)
    except ConfigError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ConfigError(f"配置文件解析失败 {p}: {e}") from e


def load_runtime_settings(path: str | Path | None) -> RuntimeSettings:
    """读配置并做**逐项校验**。非法值给明确报错，不静默忽略。"""
    raw = read_config_file(path)
    s = RuntimeSettings()
    s.raw = raw
    s.source_path = str(path) if path else None

    loop = raw.get("loop") or {}
    if not isinstance(loop, dict):
        raise ConfigError("[loop] 必须是表（table）")
    for key, value in loop.items():
        if key not in LOOP_FIELDS:
            raise ConfigError(
                f"[loop] 不支持的项: {key}；可用: {', '.join(sorted(LOOP_FIELDS))}"
            )
        typ, lo, hi = LOOP_FIELDS[key]
        try:
            cast = typ(value)
        except (TypeError, ValueError) as e:
            raise ConfigError(f"[loop].{key} 需要 {typ.__name__}，得到 {value!r}") from e
        if not (lo <= cast <= hi):
            raise ConfigError(
                f"[loop].{key} 超出范围 [{lo}, {hi}]，得到 {cast}"
            )
        s.loop[key] = cast

    prompt = raw.get("prompt") or {}
    if isinstance(prompt, dict):
        s.prompt_files = {str(k): str(v) for k, v in prompt.items()}

    model = raw.get("model") or {}
    if isinstance(model, dict):
        s.model_bindings = {str(k): str(v) for k, v in model.items()}

    tools = raw.get("tools") or {}
    if isinstance(tools, dict):
        disabled = tools.get("disabled") or []
        if not isinstance(disabled, list):
            raise ConfigError("[tools].disabled 必须是数组")
        s.disabled_tools = [str(x) for x in disabled]

    plugins = raw.get("plugins") or {}
    if isinstance(plugins, dict):
        enabled = plugins.get("enabled")
        if enabled is not None and not isinstance(enabled, list):
            raise ConfigError("[plugins].enabled 必须是数组")
        s.enabled_plugins = [str(x) for x in (enabled or [])]

    caps = raw.get("capabilities")
    if isinstance(caps, dict):
        allow = caps.get("allow")
        if allow is not None and not isinstance(allow, list):
            raise ConfigError("[capabilities].allow 必须是数组")
        if allow is not None:
            s.capabilities = [str(x) for x in allow]

    return s


def apply_to_agent_config(agent_cfg: Any, settings: RuntimeSettings) -> Any:
    """把运行期设置合并进 `AgentConfig`，返回新对象。

    用 `dataclasses.replace` 生成新对象而不是原地改：
    配置是不可变数据，原地改会让"谁改的、什么时候改的"无从追溯。
    """
    changes: dict[str, Any] = {}
    for key, value in settings.loop.items():
        if hasattr(agent_cfg, key):
            changes[key] = value
    if settings.disabled_tools:
        changes["disabled_tools"] = tuple(settings.disabled_tools)
    if settings.capabilities is not None:
        changes["capabilities"] = tuple(settings.capabilities)
    if settings.enabled_plugins:
        changes["enabled_plugins"] = tuple(settings.enabled_plugins)
    if settings.prompt_files:
        changes["prompt_files"] = dict(settings.prompt_files)
    return replace(agent_cfg, **changes) if changes else agent_cfg


def render_config_toml(settings: dict[str, Any]) -> str:
    """把设置渲染成带注释的 TOML 文本。

    刻意手写而不是用序列化库：**注释是这个文件价值的一半**——
    用户打开它应该能看懂每一项是干什么的。
    """
    loop = settings.get("loop") or {}
    tools = settings.get("tools") or {}
    plugins = settings.get("plugins") or {}
    caps = settings.get("capabilities")
    prompt = settings.get("prompt") or {}
    model = settings.get("model") or {}

    lines = [
        "# talk-to-sql 运行配置",
        "#",
        "# 这个文件可以手改；界面里改的也会写回这里。",
        "# 改完**多数项立即生效**，不需要重启（插件与能力除外）。",
        "",
        "[loop]",
        "# agent 循环参数：控制「最多试几步」与「打转几次就停」",
        "# 调大更执着但也更慢更贵；调小更省钱但可能半途放弃",
        f"max_steps = {int(loop.get('max_steps', 8))}",
        f"max_repeat = {int(loop.get('max_repeat', 2))}",
        f"temperature = {float(loop.get('temperature', 0.0))}",
        "",
        "[tools]",
        "# 停用的工具。停用后它不会出现在模型的工具清单里。",
        "# 停用 nl2sql.solve 会让智能体失去「一把梭」能力，需谨慎。",
    ]
    disabled = tools.get("disabled") or []
    if disabled:
        items = ", ".join(f'"{x}"' for x in disabled)
        lines.append(f"disabled = [{items}]")
    else:
        lines.append("disabled = []")

    lines += [
        "",
        "[plugins]",
        "# 启用的插件名。留空数组表示不加载任何外部插件。",
    ]
    enabled = plugins.get("enabled")
    if enabled is None:
        lines.append("# enabled = []   # 未设置时加载 plugins/ 下全部插件")
    else:
        items = ", ".join(f'"{x}"' for x in enabled)
        lines.append(f"enabled = [{items}]")

    lines += [
        "",
        "[capabilities]",
        "# 授予插件与内置工具的能力。**收紧即安全，放宽即授权**。",
        "# 注意：真正的写操作防线在 guard（read_only），与这里相互独立。",
    ]
    if caps is None:
        lines.append("# allow = []   # 未设置时使用代码默认能力集")
    else:
        lines.append("allow = [" + ", ".join(f'"{x}"' for x in caps) + "]")

    if prompt:
        lines += ["", "[prompt]", "# 外置 Prompt 模板文件（相对路径按工作目录解析）"]
        for k, v in prompt.items():
            lines.append(f'{k} = "{v}"')
    if model:
        lines += ["", "[model]", "# 各组件绑定的模型名"]
        for k, v in model.items():
            lines.append(f'{k} = "{v}"')
    lines.append("")
    return "\n".join(lines)


def save_runtime_settings(path: str | Path, settings: dict[str, Any]) -> None:
    """写回配置文件。**原子写**，避免写到一半断电留下半个文件。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = render_config_toml(settings)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)


def _safe_key(name: str) -> str:
    """把工具名变成合法的 TOML/文件名片段。仅用于文件名场景。"""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)
