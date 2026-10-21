"""存储实现层：元数据、知识、历史、缓存、向量。

对外暴露 `build_storage(config)` 装配一个 `StorageBundle`。
默认全 SQLite —— 个人场景零配置优先。

设计：
    * 元数据 / 知识 / 缓存共用**同一个 SQLite 文件**（`<state_dir>/app.db`），
      便于备份，也避免个人场景出现多个文件。
    * `MemoryVectorStore` + `HashEmbedder` 是免依赖默认实现，
      可通过配置切换到 sqlite-vec / lancedb / 本地 embedding 模型。

约束：不 import 任何 Web 框架；不持有模块级连接。
"""
from __future__ import annotations

import os
from typing import Callable

from ..config import NL2SQLConfig
from ..contracts.storage import StorageBundle
from ..errors import NotRegisteredError
from .memory_vector import HashEmbedder, MemoryVectorStore
from .sqlite_cache import SqliteResultCache
from .sqlite_knowledge import SqliteKnowledgeStore
from .sqlite_meta import SqliteMetaStore
from .sqlite_session import SqliteSessionStore

__all__ = [
    "build_storage",
    "app_db_path",
    "SqliteMetaStore",
    "SqliteKnowledgeStore",
    "SqliteResultCache",
    "SqliteSessionStore",
    "MemoryVectorStore",
    "HashEmbedder",
    "normalize_value",
    "normalize_rows",
]

from .sqlite_cache import normalize_rows, normalize_value  # noqa: E402  (重导出)


def app_db_path(config: NL2SQLConfig) -> str:
    """元数据 / 知识 / 缓存的统一落盘位置。"""
    return str(config.state_path("app.db"))


def build_storage(
    config: NL2SQLConfig,
    *,
    vector_factory: Callable[[object], object] | None = None,
    embedder_factory: Callable[[], object] | None = None,
) -> StorageBundle:
    """按配置装配存储集合。

    Args:
        config: 核心配置，决定状态目录与向量/向量化器选型。
        vector_factory: 覆盖默认向量库构造（接收 embedder 实例）。
        embedder_factory: 覆盖默认向量化器构造。

    Returns:
        `StorageBundle`。调用方负责在退出时逐个 `close()`。
    """
    config.validate()
    db_path = app_db_path(config)

    meta = SqliteMetaStore(db_path)
    knowledge = SqliteKnowledgeStore(db_path)
    cache = SqliteResultCache(db_path)
    sessions = SqliteSessionStore(db_path)

    vector = None
    embedder = None
    if config.knowledge.enabled:
        embedder = (
            embedder_factory() if embedder_factory
            else _default_embedder(config.knowledge.embedder)
        )
        vector = (
            vector_factory(embedder) if vector_factory
            else _default_vector_store(config.knowledge.vector_store, embedder)
        )

    return StorageBundle(
        meta=meta, knowledge=knowledge, cache=cache,
        vector=vector, embedder=embedder, sessions=sessions,
    )


def _default_embedder(name: str) -> object:
    if name == "hash":
        return HashEmbedder()
    raise NotRegisteredError(
        f"未实现的向量化器: {name}；当前内置: hash（其余需在 registry 中注册后传入）"
    )


def _default_vector_store(name: str, embedder: object) -> object:
    if name == "memory":
        return MemoryVectorStore(embedder)
    raise NotRegisteredError(
        f"未实现的向量库: {name}；当前内置: memory（其余需在 registry 中注册后传入）"
    )


def close_storage(bundle: StorageBundle) -> None:
    """关闭全部存储。单个失败不阻断其余关闭。"""
    for component in (bundle.meta, bundle.knowledge, bundle.cache,
                      bundle.sessions):
        closer = getattr(component, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 - 关闭失败不应阻断退出
                pass


def state_dir_ready(config: NL2SQLConfig) -> bool:
    return os.path.isdir(config.state_dir)
