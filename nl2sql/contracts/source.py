"""数据源契约：能力声明、元数据、执行。

设计要点
--------
1. **Capability 驱动降级**：核心层不做 `if dialect == "oracle"` 判断，
   而是读适配器声明的能力，按能力选择策略。
2. **执行为只读语义**：适配器不负责安全，安全由 `guard/` 在调用前完成；
   但适配器必须实现自己的超时兜底。
3. **元数据与执行分离**：`introspect()` 可以走缓存，`execute()` 必须实时。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable


# --------------------------------------------------------------------- 能力
@dataclass(frozen=True)
class SourceCapabilities:
    """适配器能力声明。核心层据此选择生成与执行策略。"""

    dialect: str                      # "sqlite" / "mysql" / "hive" ...
    sql: bool = True                  # 是否支持 SQL（NoSQL 为 False）
    pagination: str = "limit"         # "limit" | "rownum" | "offset_fetch" | "none"
    supports_cte: bool = True
    supports_window: bool = True
    supports_json: bool = False
    supports_explain: bool = True
    identifier_quote: str = '"'       # '"' | '`' | ''
    default_schema: str | None = None
    # 生成列、注释等方言差异由适配器自行处理，不外泄给核心层
    extras: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------- 元数据
@dataclass
class ColumnMeta:
    name: str
    data_type: str
    nullable: bool = True
    comment: str = ""
    is_primary_key: bool = False
    ordinal: int = 0
    # 生成列（如 SQLite/MySQL 的 GENERATED ALWAYS AS）。
    # 必须识别：生成列不能出现在 INSERT 里，且部分数据库不允许直接写。
    generated: bool = False


@dataclass
class TableMeta:
    name: str
    schema: str | None = None
    comment: str = ""
    columns: list[ColumnMeta] = field(default_factory=list)
    row_estimate: int | None = None
    # 物理外键（语义外键由语义层维护，不在这里）
    foreign_keys: list[tuple[str, str, str]] = field(default_factory=list)
    # ("column", "ref_schema.ref_table", "ref_column")


@dataclass
class SnapshotMeta:
    """一次元数据快照的标识与时间戳，用于知识库与缓存的失效判断。"""

    source_id: str
    fingerprint: str
    captured_at: str          # ISO8601
    table_count: int = 0


# --------------------------------------------------------------------- 执行
@dataclass
class QueryResult:
    """执行结果。列名与行数据分离，便于跨引擎比对。"""

    columns: list[str]
    rows: list[Sequence[Any]]
    row_count: int
    truncated: bool = False       # 是否因 LIMIT 被截断
    elapsed_ms: int = 0
    source_id: str = ""
    sql: str = ""
    cached: bool = False
    data_time: str | None = None  # 数据新鲜度（若适配器可知）
    warnings: list[str] = field(default_factory=list)


@dataclass
class DryRun:
    """执行前审查结果（EXPLAIN / 静态分析）。"""

    ok: bool
    estimated_rows: int | None = None
    estimated_cost: float | None = None
    full_scan: bool = False
    warnings: list[str] = field(default_factory=list)
    raw: str | None = None        # 原始 EXPLAIN 输出，便于排查


# --------------------------------------------------------------------- 契约
@runtime_checkable
class SourceAdapter(Protocol):
    """所有数据源适配器的契约。

    实现要求：
    * 无模块级全局状态；连接由实例持有，生命周期由调用方管理
    * `execute()` 必须只读；写操作由 guard 层拦截，适配器不重复实现
    * 任何方言差异在适配器内部消化，不得外泄为调用方的 if 分支
    """

    capabilities: SourceCapabilities

    def test_connection(self) -> None:
        """连接自检，失败抛 SourceConnectionError。"""
        ...

    def introspect(self, *, include_comments: bool = True) -> list[TableMeta]:
        """抽取库表元数据。"""
        ...

    def execute(self, sql: str, *, limit: int, timeout_s: float) -> QueryResult:
        """执行只读 SQL。超时抛 QueryTimeoutError。"""
        ...

    def explain(self, sql: str) -> DryRun | None:
        """可选：执行前审查。不支持则返回 None。"""
        ...

    def close(self) -> None:
        ...
