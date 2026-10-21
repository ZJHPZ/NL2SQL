"""数据源适配层：能力声明 + 元数据抽取 + 执行。

P0 只做 SQLite 与 MySQL。**新增数据源 = 加一个文件 + 在 registry 注册**，
不改核心任何代码。
"""
from __future__ import annotations

from ..config import SourceConfig
from ..contracts.source import SourceAdapter
from ..errors import ConfigError, NotRegisteredError
from ..registry import Registry
from .sqlite import SqliteAdapter

__all__ = ["build_adapter", "SqliteAdapter"]


def build_adapter(
    source_cfg: SourceConfig,
    *,
    registry: Registry | None = None,
    credentials: dict[str, str] | None = None,
) -> SourceAdapter:
    """按配置装配适配器。

    Args:
        source_cfg: 数据源配置。
        registry: 组件注册表；None 时使用内置适配器集合。
        credentials: 已解析的凭据（例如从 OS 凭据存储取出）。
            **密码不经过配置文件**，只在这里注入。

    Returns:
        实现 `SourceAdapter` 契约的适配器实例。
    """
    adapter_name = source_cfg.adapter

    if adapter_name == "sqlite":
        path = source_cfg.options.path
        if not path:
            raise ConfigError(f"[{source_cfg.source_id}] sqlite 缺少 options.path")
        return SqliteAdapter(path)

    # 其余适配器从注册表取构造器（MysqlAdapter 在 S8 注册）
    if registry is None:
        raise NotRegisteredError(
            f"适配器 {adapter_name} 需要显式注册；当前内置仅 sqlite"
        )
    factory = registry.adapters.get(adapter_name)
    return factory(source_cfg, credentials=credentials or {})
