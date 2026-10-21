"""各模块骨架内容定义。配合 scripts/scaffold.py 使用。

每个条目: {相对路径: (模块 docstring, 正文代码)}
正文代码为最小可运行骨架 + 明确 TODO，不含实现逻辑。
"""
from __future__ import annotations

STUBS: dict[str, tuple[str, str]] = {}

# ======================================================== storage
STUBS["nl2sql/storage/__init__.py"] = (
    """存储实现层：元数据、知识、历史、缓存、向量。

对外暴露 `build_storage(config)` 装配一个 `StorageBundle`。
默认全 SQLite —— 个人场景零配置优先。

约束：不 import 任何 Web 框架；不持有模块级连接。
""",
    '''from __future__ import annotations

from ..config import NL2SQLConfig
from ..contracts.storage import StorageBundle

__all__ = ["build_storage"]


def build_storage(config: NL2SQLConfig) -> StorageBundle:
    """按配置装配存储集合。

    TODO(S2):
      1. 建 SQLite 元数据库 schema（见 docs/02 数据表清单）
      2. 实现 SqliteMetaStore / SqliteKnowledgeStore / SqliteResultCache
      3. 按 config.knowledge.vector_store 装配向量库（默认 memory）
      4. 按 config.knowledge.embedder 装配向量化器（默认 hash，免依赖）
    """
    raise NotImplementedError("S2 阶段实现")
''',
)

STUBS["nl2sql/storage/sqlite_meta.py"] = (
    """元数据存储（SQLite 实现）。

数据表：
    data_source   数据源配置（不含明文密码，只有 credential_ref）
    snapshot      元数据快照标识与时间戳
    table_meta    表元数据
    column_meta   列元数据
    history       查询历史（支撑回放与结果复用）
""",
    '''from __future__ import annotations

import sqlite3
from typing import Any

from ..contracts.source import SnapshotMeta, TableMeta
from ..contracts.storage import DataSourceConfig, HistoryRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS data_source (
    source_id   TEXT PRIMARY KEY,
    adapter     TEXT NOT NULL,
    label       TEXT DEFAULT '',
    options     TEXT DEFAULT '{}',
    credential_ref TEXT,
    semantic_path TEXT
);
CREATE TABLE IF NOT EXISTS snapshot (
    source_id   TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    table_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS table_meta (
    source_id TEXT NOT NULL, table_name TEXT NOT NULL,
    schema_name TEXT, comment TEXT DEFAULT '', row_estimate INTEGER,
    PRIMARY KEY (source_id, table_name)
);
CREATE TABLE IF NOT EXISTS column_meta (
    source_id TEXT NOT NULL, table_name TEXT NOT NULL, column_name TEXT NOT NULL,
    data_type TEXT, nullable INTEGER DEFAULT 1, comment TEXT DEFAULT '',
    is_pk INTEGER DEFAULT 0, ordinal INTEGER DEFAULT 0,
    PRIMARY KEY (source_id, table_name, column_name)
);
CREATE TABLE IF NOT EXISTS history (
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, question TEXT NOT NULL,
    plan_fingerprint TEXT NOT NULL, sql TEXT DEFAULT '', status TEXT NOT NULL,
    row_count INTEGER DEFAULT 0, elapsed_ms INTEGER DEFAULT 0,
    created_at TEXT NOT NULL, error_code TEXT, error_message TEXT, meta TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_history_source_time ON history (source_id, created_at DESC);
"""


class SqliteMetaStore:
    """TODO(S2): 实现 MetaStore 协议的全部方法。

    注意：连接由实例持有并在 close() 释放，不做模块级单例。
    """

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
''',
)

STUBS["nl2sql/storage/sqlite_knowledge.py"] = (
    """知识库存储（SQLite + FTS5）。

数据表：
    knowledge_entry   当前版本（enabled 为停用开关）
    knowledge_rev     全量历史修订（diff / 回滚依据）

关键点：**只有 enabled=1 的条目参与检索**。
FTS5 提供 BM25；向量检索由 VectorStore 提供，二者在 knowledge/ 里融合。
""",
    '''from __future__ import annotations

import sqlite3

from ..contracts.knowledge import KnowledgeEntry, KnowledgeType, Revision

SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_entry (
    key TEXT PRIMARY KEY, type TEXT NOT NULL, title TEXT DEFAULT '',
    body TEXT DEFAULT '', source_id TEXT, tags TEXT DEFAULT '[]',
    enabled INTEGER DEFAULT 1, origin TEXT DEFAULT 'user',
    source_ref TEXT DEFAULT '', created_at TEXT, updated_at TEXT,
    version INTEGER DEFAULT 1, extra TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS knowledge_rev (
    key TEXT NOT NULL, version INTEGER NOT NULL, title TEXT DEFAULT '',
    body TEXT DEFAULT '', changed_at TEXT, changed_by TEXT DEFAULT '',
    note TEXT DEFAULT '', PRIMARY KEY (key, version)
);
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    key UNINDEXED, title, body, tags
);
"""


class SqliteKnowledgeStore:
    """TODO(S6): 实现 KnowledgeStore 协议。

    要点：
      1. upsert 时先写 knowledge_rev（留痕），再更新 knowledge_entry
      2. FTS5 与主表同步维护
      3. rollback(key, version) 从 knowledge_rev 恢复并生成新版本号（不回退版本号）
    """

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
''',
)

STUBS["nl2sql/storage/sqlite_cache.py"] = (
    """结果缓存（SQLite 实现）。

键 = (source_id, plan_fingerprint)，**source_id 必须参与键**，
防止个人用户同时连测试库与生产库时串缓存。

失效策略：TTL + 手动刷新（invalidate）。不做 CDC。
""",
    '''from __future__ import annotations

import sqlite3

from ..contracts.storage import CachedResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS result_cache (
    source_id TEXT NOT NULL,
    plan_fingerprint TEXT NOT NULL,
    columns TEXT NOT NULL,
    rows TEXT NOT NULL,
    row_count INTEGER DEFAULT 0,
    sql TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT,
    data_time TEXT,
    PRIMARY KEY (source_id, plan_fingerprint)
);
"""


class SqliteResultCache:
    """TODO(S8): 实现 ResultCache 协议。

    存 rows 用 JSON，序列化时统一把值转成字符串（与评测器的归一化一致），
    避免 float/Decimal 跨驱动不一致导致比对失败。
    """

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
''',
)

STUBS["nl2sql/storage/memory_vector.py"] = (
    """内存向量库 + 免依赖向量化器（默认实现）。

用途：P0 零依赖起步。用户不配任何模型也能用知识库的 BM25 部分。
进阶实现（sqlite-vec / lancedb / 本地 embedding 模型）走同一 VectorStore 契约。
""",
    '''from __future__ import annotations

import hashlib
import math
from typing import Any

DIM = 256


class HashEmbedder:
    """字符 n-gram 哈希向量化。**免模型依赖的占位实现**。

    不是语义向量——真正语义检索请换成本地模型或远端 API。
    它的价值是让 P0 不阻塞、且接口先跑通。
    """

    name = "hash"
    dim = DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    @staticmethod
    def _one(text: str) -> list[float]:
        vec = [0.0] * DIM
        for n in (1, 2):
            for i in range(len(text) - n + 1):
                gram = text[i:i + n]
                h = int(hashlib.md5(gram.encode("utf-8")).hexdigest()[:8], 16)
                vec[h % DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class MemoryVectorStore:
    """TODO(S6): 实现 VectorStore 协议（余弦相似度 + where 过滤）。"""

    name = "memory"

    def __init__(self, embedder: Any) -> None:
        self._embedder = embedder
        self._items: dict[str, tuple[list[float], dict[str, Any]]] = {}
''',
)

# ======================================================== catalog
STUBS["nl2sql/catalog/__init__.py"] = (
    """元数据目录：抽取 + 缓存 + 快照。

职责：把数据源的库表结构变成可检索、可对比的快照，
并向知识库提供"库表注释 → 知识条目"的原料。
""",
    '''from __future__ import annotations

from ..contracts.source import SnapshotMeta, TableMeta

__all__ = ["CatalogService", "diff_snapshots"]


class CatalogService:
    """TODO(S2): 元数据抽取与缓存。

    要点：
      1. 首次连接自动抽取，落 snapshot + table_meta + column_meta
      2. fingerprint 用于判断是否需要重新抽取
      3. 提供 render_ddl() 供自由生成时作为上下文
    """

    def __init__(self, store, adapters) -> None:
        self._store = store
        self._adapters = adapters

    def refresh(self, source_id: str) -> SnapshotMeta:
        raise NotImplementedError("S2")

    def tables(self, source_id: str) -> list[TableMeta]:
        raise NotImplementedError("S2")

    def render_ddl(self, source_id: str, *, max_tables: int = 50) -> str:
        """渲染精简 DDL 文本。**切忌把整库 DDL 全塞进 prompt**。"""
        raise NotImplementedError("S2")


def diff_snapshots(old: list[TableMeta], new: list[TableMeta]) -> dict[str, list[str]]:
    """快照差异，用于提示"表结构变了，知识库可能需要更新"。"""
    raise NotImplementedError("S6")
''',
)

# ======================================================== semantics
STUBS["nl2sql/semantics/__init__.py"] = (
    """语义层：业务概念 → 逻辑计划。

格式：**兼容 dbt metrics.yml，不自创 DSL**。
职责边界：只产出 LogicalPlan（要什么），不产出 SQL（怎么写）。
""",
    '''from __future__ import annotations

from ..contracts.semantic import LogicalPlan, SemanticModel

__all__ = ["DbtMetricsProvider", "compile_plan_to_sql", "SemanticRegistry"]


class DbtMetricsProvider:
    """TODO(S4): 加载 dbt metrics.yml 并 resolve 成 LogicalPlan。

    要点：
      1. load(): 解析 yaml → SemanticModel（指标/维度/实体/同义词）
      2. resolve(): 问题 → LogicalPlan。确定性：同入参同指纹
      3. 未覆盖抛 SemanticResolutionError，由上层决定自由生成 + 标注未验证口径
      4. 相对时间（本月/上月）必须以显式 now 锚定，不许读系统时钟
    """

    def __init__(self, metrics_path: str, source_id: str) -> None:
        self._path = metrics_path
        self._source_id = source_id

    def load(self) -> SemanticModel:
        raise NotImplementedError("S4")

    def resolve(self, question: str, *, now: str | None = None) -> LogicalPlan:
        raise NotImplementedError("S4")


def compile_plan_to_sql(plan: LogicalPlan, model: SemanticModel, dialect: str) -> str:
    """逻辑计划 → 方言 SQL。

    TODO(S3): 先用字符串模板 + sqlglot 做方言转译，
    不要一上来就写完整的 SQL AST 生成器。
    """
    raise NotImplementedError("S3")


class SemanticRegistry:
    """多数据源的语义模型集合（每个 source_id 一个模型 + 版本）。"""

    def __init__(self) -> None:
        self._models: dict[str, SemanticModel] = {}
''',
)

# ======================================================== knowledge
STUBS["nl2sql/knowledge/__init__.py"] = (
    """知识库：条目管理 + 多路召回 + 自动生成。

个人场景：**免审，但保留版本、停用开关、测试检索**。
""",
    '''from __future__ import annotations

from ..contracts.knowledge import KnowledgeEntry, RetrievalHit, RetrievalQuery
from ..contracts.source import TableMeta

__all__ = [
    "KnowledgeService",
    "HybridRetriever",
    "Bm25Retriever",
    "VectorRetriever",
    "entries_from_table_meta",
]


class KnowledgeService:
    """TODO(S6): 知识库门面。

    要点：
      1. upsert 一律留修订；删除是软删（保留历史）
      2. retrieve 只召回 enabled=1 的条目
      3. test_retrieve 返回 why（命中原因），供 UI 展示
      4. auto_generate_from_comments 幂等：已存在的 key 不覆盖用户改动
    """

    def __init__(self, store, retrievers) -> None:
        self._store = store
        self._retrievers = retrievers

    def retrieve(self, query: RetrievalQuery) -> list[RetrievalHit]:
        raise NotImplementedError("S6")

    def test_retrieve(self, text: str, *, source_id: str | None = None,
                      top_k: int = 8) -> list[RetrievalHit]:
        raise NotImplementedError("S6")


class Bm25Retriever:
    """TODO(S6): 基于 SQLite FTS5 的 BM25 召回。"""

    name = "bm25"


class VectorRetriever:
    """TODO(S6): 基于 VectorStore 的向量召回。"""

    name = "vector"


class HybridRetriever:
    """TODO(S6): 多路召回融合 + 重排（RRF 起步即可）。"""

    name = "hybrid"

    def __init__(self, retrievers) -> None:
        self._retrievers = retrievers


def entries_from_table_meta(meta: TableMeta, *, source_id: str) -> list[KnowledgeEntry]:
    """库表注释 → 初版知识条目。

    TODO(S6): 表注释 → 一条 schema 知识；列注释 → 汇总进该条正文。
    这是降低上手门槛的关键：用户不必面对空白知识库。
    """
    raise NotImplementedError("S6")
''',
)

# ======================================================== sources
STUBS["nl2sql/sources/__init__.py"] = (
    """数据源适配层：能力声明 + 元数据抽取 + 执行。

P0 只做 SQLite 与 MySQL。新增数据源 = 加一个文件 + 注册，不改核心。
""",
    '''from __future__ import annotations

from ..contracts.source import SourceAdapter

__all__ = ["build_adapter", "SqliteAdapter", "MysqlAdapter"]


def build_adapter(source_cfg, credentials=None) -> SourceAdapter:
    """按配置装配适配器。

    TODO(S2/S8): 从注册表取适配器类并实例化。
    密码从 credentials（OS 凭据存储解析结果）注入，而非配置。
    """
    raise NotImplementedError("S2")


class SqliteAdapter:
    """TODO(S2): 实现 SourceAdapter（sqlite3，只读 URI 模式）。"""


class MysqlAdapter:
    """TODO(S8): 实现 SourceAdapter（SQLAlchemy + pymysql）。"""
''',
)

STUBS["nl2sql/sources/sqlite.py"] = (
    """SQLite 适配器。

要点：
    * 打开只读：`file:...?mode=ro`，从连接层面兜底只读
    * introspect: sqlite_master + PRAGMA table_info（SQLite 无注释概念，
      可在知识库中人工补充）
    * 标识符引号：双引号
    * 分页：LIMIT
""",
    '''from __future__ import annotations

from ..contracts.source import (
    ColumnMeta,
    DryRun,
    QueryResult,
    SourceCapabilities,
    TableMeta,
)


class SqliteAdapter:
    capabilities = SourceCapabilities(
        dialect="sqlite",
        pagination="limit",
        supports_cte=True,
        supports_window=True,
        supports_json=True,
        supports_explain=True,
        identifier_quote='"',
    )

    def __init__(self, path: str, *, read_only: bool = True) -> None:
        self._path = path
        self._read_only = read_only
        self._conn = None

    # TODO(S2): connect / test_connection / introspect / execute / explain / close
''',
)

STUBS["nl2sql/sources/columns.py"] = (
    """列名候选与"相似字段建议"。

用途：当生成的 SQL 引用了不存在的列时，**不要抛数据库原始错误**，
而是给出可用字段候选。这是"只给结果表"方案里错误体验的关键一环。
""",
    '''from __future__ import annotations

__all__ = ["suggest_columns"]


def suggest_columns(bad: str, available: list[str], *, top_k: int = 5) -> list[str]:
    """TODO(S4): 用 difflib 或编辑距离给出候选列名。"""
    import difflib

    return difflib.get_close_matches(bad, available, n=top_k, cutoff=0.5)
''',
)

# ======================================================== dialect
STUBS["nl2sql/dialect/__init__.py"] = (
    """方言层：逻辑计划 → 方言 SQL，以及 SQL 校验。

**不让 LLM 直接写方言 SQL。** 确定性编译 + sqlglot 转译。
""",
    '''from __future__ import annotations

from ..contracts.semantic import LogicalPlan, SemanticModel
from ..contracts.source import SourceCapabilities

__all__ = ["SqlCompiler", "SqlValidator"]


class SqlCompiler:
    """TODO(S3): LogicalPlan → SQL（先 SQLite/MySQL 同构，再走 sqlglot 转译）。

    分工原则：
        LangGraph 管编排（高层决策）
        sqlglot/SQLAlchemy 管方言（确定性编译）
    """

    def __init__(self, capabilities: SourceCapabilities) -> None:
        self._cap = capabilities

    def compile(self, plan: LogicalPlan, model: SemanticModel) -> str:
        raise NotImplementedError("S3")


class SqlValidator:
    """TODO(S3): 静态校验。

    检查项：
        1. 只读：拒绝 DDL/DML/多语句
        2. 引用存在性：表名/列名必须在快照中
        3. 强制 LIMIT：按 config.safety.max_rows 补齐或收紧
        4. 方言合法性：可用 sqlglot.parse_one 校验
    """

    def __init__(self, capabilities: SourceCapabilities, known_tables) -> None:
        self._cap = capabilities
        self._known_tables = known_tables

    def validate(self, sql: str, *, max_rows: int) -> tuple[bool, list[str]]:
        raise NotImplementedError("S3")
''',
)

# ======================================================== guard
STUBS["nl2sql/guard/__init__.py"] = (
    """执行守门：SQL 落地前的最后一道确定性校验。

原则：**授权与安全由网关确定性校验，绝不交给模型或 Prompt 判断。**
个人场景精简为：只读、禁多语句、强制 LIMIT、超时。
""",
    '''from __future__ import annotations

from ..contracts.source import SourceCapabilities

__all__ = ["Guard", "GuardDecision"]


class GuardDecision:
    __slots__ = ("allowed", "sql", "rule", "message", "suggestion")

    def __init__(self, allowed: bool, sql: str = "", rule: str = "",
                 message: str = "", suggestion: str = "") -> None:
        self.allowed = allowed
        self.sql = sql
        self.rule = rule
        self.message = message
        self.suggestion = suggestion


class Guard:
    """TODO(S3): 实现 check(sql) -> GuardDecision。

    实现方式建议：用 sqlglot 解析成 AST 判断语句类型，**不要用正则**。
    正则判只读语句会在注释、字符串字面量、CTE 上翻车。
    """

    def __init__(self, capabilities: SourceCapabilities, *, max_rows: int,
                 timeout_s: float, forbid_multiple: bool = True) -> None:
        self._cap = capabilities
        self._max_rows = max_rows
        self._timeout_s = timeout_s
        self._forbid_multiple = forbid_multiple

    def check(self, sql: str) -> GuardDecision:
        raise NotImplementedError("S3")
''',
)

# ======================================================== cache
STUBS["nl2sql/cache/__init__.py"] = (
    """结果缓存：逻辑计划指纹 → 结果集。

**已明确去掉跨会话语义相似缓存**（判断错了没人发现）。
只保留确定性指纹缓存，且按数据源隔离。
""",
    '''from __future__ import annotations

from ..contracts.semantic import LogicalPlan
from ..contracts.storage import CachedResult, ResultCache

__all__ = ["CacheService"]


class CacheService:
    """TODO(S8): 缓存读写。

    要点：
      1. 键 = (source_id, plan.fingerprint())，两者缺一不可
      2. 读命中时 outcome.cached = True，并透出 data_time
      3. 写失败不得影响正常返回（降级为不缓存）
    """

    def __init__(self, store: ResultCache, *, enabled: bool = True,
                 ttl_seconds: int = 900) -> None:
        self._store = store
        self._enabled = enabled
        self._ttl = ttl_seconds

    def lookup(self, source_id: str, plan: LogicalPlan) -> CachedResult | None:
        if not self._enabled:
            return None
        raise NotImplementedError("S8")
''',
)

# ======================================================== agent
STUBS["nl2sql/agent/__init__.py"] = (
    """智能体编排层（LangGraph）。

职责：把"问题"变成"结果"。所有副作用（执行、缓存、召回）在此汇聚。
节点之间只通过 state 通信；依赖全部构造注入。
""",
    '''from __future__ import annotations

from ..contracts.pipeline import GraphSpec, QueryOutcome

__all__ = ["AgentGraph", "build_default_graph", "ErrorOutcomeBuilder"]


def build_default_graph(spec: GraphSpec | None = None) -> GraphSpec:
    """P0 默认管线。

    TODO(S4):
        intent → retrieve → plan → compile → validate → cache_lookup
              → guard → execute → cache_store → outcome

    注意：cache_lookup 在 plan 之后 —— 因为缓存键是逻辑计划指纹，
    问题换个说法只要计划相同就应命中。
    """
    raise NotImplementedError("S4")


class AgentGraph:
    """TODO(S4): 声明式管线执行器。

    要点：
      1. 节点由注册表查找（支持用户自定义节点）
      2. 依赖构造时注入，运行时只传 state
      3. trace 记录每个节点耗时，供回放排障
    """

    def __init__(self, spec: GraphSpec, registry, deps: dict) -> None:
        self._spec = spec
        self._registry = registry
        self._deps = deps

    def run(self, question: str, *, source_id: str, now: str | None = None) -> QueryOutcome:
        raise NotImplementedError("S4")


class ErrorOutcomeBuilder:
    """TODO(S4): 把各类失败翻译成**可操作**的 QueryOutcome。

    这是产品体验的核心，不是收尾工作：
        列名不存在 → 给候选字段（candidates）
        结果为空   → 回显生效的过滤条件（applied_filters）
        超时       → 提示加时间范围（suggestions）
        被截断     → 明示"仅显示前 N 行"，并提供导出
        未覆盖口径 → notes 加"未验证口径"，不静默
    """
''',
)

STUBS["nl2sql/agent/graph.py"] = (
    """LangGraph 管线定义。

约束：
    * `langgraph` 只在这里出现——核心其他模块不依赖它
    * state 为普通 dict，便于持久化与回放
    * 节点实现从注册表查找，便于用户替换
""",
    '''from __future__ import annotations

from typing import Any

__all__ = ["build_langgraph"]


def build_langgraph(spec, nodes: dict[str, Any]):
    """TODO(S4): 把 GraphSpec 编译成 LangGraph 图。

    实现提示：
        from langgraph.graph import StateGraph, END
        g = StateGraph(dict)
        for node_spec in spec.nodes:
            g.add_node(node_spec.name, nodes[node_spec.name].run)
        ...
    一期不做澄清分支；clarify=False。
    """
    raise NotImplementedError("S4")
''',
)
