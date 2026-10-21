"""存储契约：元数据、知识、历史、缓存、向量。

设计要点
--------
1. **存储可插拔**：默认 SQLite，但不写死。个人场景零配置优先。
2. **单一状态目录**：所有持久化落在同一个 `data/` 下，便于备份与清理。
3. **向量与元数据分离**：向量库单独一个契约，允许换实现。
4. **缓存按数据源隔离**：契约层面强制 `source_id` 参与键，不允许省略。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

from .knowledge import KnowledgeEntry, KnowledgeType, Revision
from .source import QueryResult, SnapshotMeta, TableMeta


# --------------------------------------------------------------------- 配置
@dataclass
class DataSourceConfig:
    """一个数据源的连接配置。**不存明文密码**，只存凭据引用。"""

    source_id: str
    adapter: str                       # "sqlite" / "mysql"
    label: str = ""
    options: dict[str, Any] = field(default_factory=dict)
    credential_ref: str | None = None  # 指向 OS 凭据存储 / 本地加密库的键
    semantic_path: str | None = None   # dbt metrics.yml 路径


# --------------------------------------------------------------------- 记录
@dataclass
class HistoryRecord:
    """一次问答的完整记录，支撑"可回放"与结果复用。"""

    id: str
    source_id: str
    question: str
    plan_fingerprint: str
    sql: str
    status: str                        # "ok" | "empty" | "error" | "cached"
    row_count: int = 0
    elapsed_ms: int = 0
    created_at: str = ""
    error_code: str | None = None
    error_message: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class QueryLogRecord:
    """一次问答的**观测记录**（append-only，供统计聚合）。

    与 `HistoryRecord` 的分工：那个供"回放/复查某一次问答"，这个供
    "最近用得怎么样、错得多不多"。刻意分成两张表——统计查询会扫全表，
    不该和回放记录抢同一份索引；而且观测数据的字段需求（planner/runtime）
    会随实验变化，不该污染回放契约。
    """

    created_at: str
    source_id: str
    status: str
    question: str = ""
    session_id: str = ""
    planner: str = ""
    runtime: str = ""
    error_code: str | None = None
    cached: bool = False
    elapsed_ms: int = 0
    row_count: int = 0
    steps: int = 0


@dataclass
class ToolCallRecord:
    """一次工具调用的观测记录。来源是运行时里的显式回调，不是从日志反解。"""

    created_at: str
    tool: str
    ok: bool
    elapsed_ms: int = 0
    session_id: str = ""
    source_id: str = ""
    error: str | None = None


@dataclass
class CachedResult:
    """结果缓存条目。键 = (source_id, plan_fingerprint)。"""

    source_id: str
    plan_fingerprint: str
    columns: list[str]
    rows: list[Sequence[Any]]
    row_count: int
    sql: str = ""
    created_at: str = ""
    expires_at: str | None = None
    data_time: str | None = None


# --------------------------------------------------------------------- 契约
class MetaStore(Protocol):
    """元数据与配置的持久化。"""

    # 数据源配置
    def save_source(self, cfg: DataSourceConfig) -> None: ...
    def get_source(self, source_id: str) -> DataSourceConfig | None: ...
    def list_sources(self) -> list[DataSourceConfig]: ...
    def delete_source(self, source_id: str) -> None: ...

    # 元数据快照
    def save_snapshot(self, meta: SnapshotMeta, tables: list[TableMeta]) -> None: ...
    def get_tables(self, source_id: str) -> list[TableMeta]: ...
    def get_snapshot_meta(self, source_id: str) -> SnapshotMeta | None: ...

    # 查询历史
    def add_history(self, rec: HistoryRecord) -> None: ...
    def list_history(self, *, source_id: str | None = None,
                     limit: int = 50) -> list[HistoryRecord]: ...
    def get_history(self, history_id: str) -> HistoryRecord | None: ...


class KnowledgeStoreProtocol(Protocol):
    """知识库持久化（与 knowledge.KnowledgeStore 对齐）。"""

    def upsert(self, entry: KnowledgeEntry, *, note: str = "") -> KnowledgeEntry: ...
    def get(self, key: str) -> KnowledgeEntry | None: ...
    def list(self, *, source_id: str | None = None,
             types: tuple[KnowledgeType, ...] = (),
             include_disabled: bool = False) -> list[KnowledgeEntry]: ...
    def history(self, key: str) -> list[Revision]: ...
    def rollback(self, key: str, version: int) -> KnowledgeEntry: ...
    def set_enabled(self, key: str, enabled: bool) -> None: ...
    def delete(self, key: str) -> None: ...


class ResultCache(Protocol):
    """结果缓存。**source_id 必须参与键**，防止测试库/生产库串缓存。"""

    def get(self, source_id: str, plan_fingerprint: str) -> CachedResult | None: ...
    def put(self, item: CachedResult, *, ttl_seconds: int | None = None) -> None: ...
    def invalidate(self, source_id: str) -> int:
        """手动刷新：清空某数据源的缓存，返回清除条数。"""
        ...
    def stats(self) -> dict[str, Any]: ...


@runtime_checkable
class VectorStore(Protocol):
    """向量检索。实现可选：sqlite-vec / lancedb / chroma / 内存。"""

    name: str

    def upsert(self, ids: list[str], texts: list[str],
               metadatas: list[dict[str, Any]] | None = None) -> None: ...
    def search(self, text: str, *, top_k: int = 8,
               where: dict[str, Any] | None = None) -> list[tuple[str, float]]: ...
    def delete(self, ids: list[str]) -> None: ...


@runtime_checkable
class Embedder(Protocol):
    """向量化。允许本地模型与远端 API 互换。"""

    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class StatsStore(Protocol):
    """观测数据：append-only 记录 + 聚合查询。

    为什么是 append-only：统计要的是"发生过什么"，而不是"当前状态"。
    更新/删除记录会让"上周的失败率"在事后被改写，那统计就失去意义。
    """

    def add_query(self, rec: QueryLogRecord) -> None: ...
    def add_tool_call(self, rec: ToolCallRecord) -> None: ...
    def query_stats(self, *, since: str | None = None) -> dict[str, Any]:
        """聚合：总数、状态分布、成功率、延迟分位、工具调用统计。"""
        ...
    def recent_failures(self, *, limit: int = 10,
                        since: str | None = None) -> list[dict[str, Any]]:
        """最近的非成功条目（界面"失败归因"面板用）。"""
        ...


@dataclass
class StorageBundle:
    """一次装配好的存储集合，由核心注入使用（避免模块级全局）。"""

    meta: MetaStore
    knowledge: KnowledgeStoreProtocol
    cache: ResultCache
    vector: VectorStore | None = None
    embedder: Embedder | None = None
    # agent 会话（回合制澄清需要）。可选：不使用 agent 模式时可缺省。
    sessions: Any = None
    # 观测（M5）。可选：旧实现/测试桩没有它时，统计降级为空而不是报错。
    stats: StatsStore | None = None
