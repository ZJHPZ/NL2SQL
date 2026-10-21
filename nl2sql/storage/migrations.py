"""建表与迁移。

原则：**幂等**。每次打开存储都跑一遍，已存在的表不动。
`user_version` 记录 schema 版本，为后续结构变更留出升级路径。

注意：`executescript()` 会隐式提交，因此不能放在显式事务里回滚。
所以迁移脚本必须自己保证幂等（`CREATE TABLE IF NOT EXISTS`）。
"""
from __future__ import annotations

import sqlite3

from .text import index_text

SCHEMA_VERSION = 4

# ---------------------------------------------------------------- 元数据与历史
DDL_META = """
CREATE TABLE IF NOT EXISTS data_source (
    source_id      TEXT PRIMARY KEY,
    adapter        TEXT NOT NULL,
    label          TEXT DEFAULT '',
    options        TEXT DEFAULT '{}',
    credential_ref TEXT,
    semantic_path  TEXT
);

CREATE TABLE IF NOT EXISTS snapshot (
    source_id   TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    table_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS table_meta (
    source_id    TEXT NOT NULL,
    table_name   TEXT NOT NULL,
    schema_name  TEXT,
    comment      TEXT DEFAULT '',
    row_estimate INTEGER,
    PRIMARY KEY (source_id, table_name)
);

CREATE TABLE IF NOT EXISTS column_meta (
    source_id   TEXT NOT NULL,
    table_name  TEXT NOT NULL,
    column_name TEXT NOT NULL,
    data_type   TEXT DEFAULT '',
    nullable    INTEGER DEFAULT 1,
    comment     TEXT DEFAULT '',
    is_pk       INTEGER DEFAULT 0,
    ordinal     INTEGER DEFAULT 0,
    is_generated INTEGER DEFAULT 0,
    PRIMARY KEY (source_id, table_name, column_name)
);

CREATE TABLE IF NOT EXISTS foreign_key (
    source_id   TEXT NOT NULL,
    table_name  TEXT NOT NULL,
    column_name TEXT NOT NULL,
    ref_table   TEXT NOT NULL,
    ref_column  TEXT NOT NULL,
    PRIMARY KEY (source_id, table_name, column_name, ref_table, ref_column)
);

CREATE TABLE IF NOT EXISTS history (
    id               TEXT PRIMARY KEY,
    source_id        TEXT NOT NULL,
    question         TEXT NOT NULL,
    plan_fingerprint TEXT NOT NULL,
    sql              TEXT DEFAULT '',
    status           TEXT NOT NULL,
    row_count        INTEGER DEFAULT 0,
    elapsed_ms       INTEGER DEFAULT 0,
    created_at       TEXT NOT NULL,
    error_code       TEXT,
    error_message    TEXT,
    meta             TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_history_source_time
    ON history (source_id, created_at DESC);
"""

# ---------------------------------------------------------------- 知识库
DDL_KNOWLEDGE = """
CREATE TABLE IF NOT EXISTS knowledge_entry (
    key        TEXT PRIMARY KEY,
    type       TEXT NOT NULL,
    title      TEXT DEFAULT '',
    body       TEXT DEFAULT '',
    source_id  TEXT,
    tags       TEXT DEFAULT '[]',
    enabled    INTEGER DEFAULT 1,
    origin     TEXT DEFAULT 'user',
    source_ref TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT,
    version    INTEGER DEFAULT 1,
    extra      TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_knowledge_source_type
    ON knowledge_entry (source_id, type, enabled);

CREATE TABLE IF NOT EXISTS knowledge_rev (
    key        TEXT NOT NULL,
    version    INTEGER NOT NULL,
    title      TEXT DEFAULT '',
    body       TEXT DEFAULT '',
    changed_at TEXT,
    changed_by TEXT DEFAULT '',
    note       TEXT DEFAULT '',
    PRIMARY KEY (key, version)
);

-- BM25 检索。rowid 与 knowledge_entry.rowid 无强绑定，靠 key 列关联，
-- 因此主表更新后需要显式同步（见 SqliteKnowledgeStore._sync_fts）。
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    key UNINDEXED,
    title,
    body,
    tags,
    tokenize = 'unicode61'
);
"""

# ---------------------------------------------------------------- 结果缓存
DDL_CACHE = """
CREATE TABLE IF NOT EXISTS result_cache (
    source_id        TEXT NOT NULL,
    plan_fingerprint TEXT NOT NULL,
    columns          TEXT NOT NULL,
    rows             TEXT NOT NULL,
    row_count        INTEGER DEFAULT 0,
    sql              TEXT DEFAULT '',
    created_at       TEXT NOT NULL,
    expires_at       TEXT,
    data_time        TEXT,
    PRIMARY KEY (source_id, plan_fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON result_cache (expires_at);
"""

# ---------------------------------------------------------------- agent 会话
# 回合制澄清要求跨请求保留上下文，因此必须落盘。
# turns 以 JSON 存储：轮次整体读写、参数是嵌套结构，拆表只会让读写都变复杂。
DDL_SESSION = """
CREATE TABLE IF NOT EXISTS agent_session (
    session_id       TEXT PRIMARY KEY,
    source_id        TEXT DEFAULT '',
    turns            TEXT DEFAULT '[]',
    pending_question TEXT DEFAULT '',
    pending_options  TEXT DEFAULT '[]',
    now_anchor       TEXT,
    last_status      TEXT DEFAULT '',
    created_at       TEXT,
    updated_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_updated
    ON agent_session (updated_at DESC);
"""


def migrate(conn: sqlite3.Connection) -> int:
    """建表 / 升级。返回当前 schema 版本。"""
    cur = conn.cursor()
    version = cur.execute("PRAGMA user_version").fetchone()[0]

    # v0 → v1：初始结构
    if version < 1:
        cur.executescript(DDL_META)
        cur.executescript(DDL_KNOWLEDGE)
        cur.executescript(DDL_CACHE)
        cur.execute("PRAGMA user_version = 1")
        conn.commit()
        version = 1

    # v1 → v2：FTS 索引改为预切分文本。
    # 原因：unicode61 不切分中文，导致中文 BM25 静默失效（详见 storage/text.py）。
    # FTS 表是纯派生数据，重建即可，不影响 knowledge_entry / knowledge_rev。
    if version < 2:
        cur.execute("DELETE FROM knowledge_fts")
        rows = cur.execute(
            "SELECT key, title, body, tags FROM knowledge_entry WHERE enabled = 1"
        ).fetchall()
        for r in rows:
            cur.execute(
                "INSERT INTO knowledge_fts (key, title, body, tags) VALUES (?,?,?,?)",
                (r["key"],
                 index_text(r["title"] or ""),
                 index_text(r["body"] or ""),
                 index_text(*_parse_tags(r["tags"]))),
            )
        cur.execute("PRAGMA user_version = 2")
        conn.commit()
        version = 2

    # v2 → v3：column_meta 增加 is_generated。
    #
    # 这是一个**静默数据丢失**的修复：适配器能正确识别生成列（走 table_xinfo），
    # 但存储层没这一列，写入时被丢弃，读回来 generated 永远是 False。
    # 后果是"生成列"这个信息只活在 introspect() 的返回值里，
    # 一旦经过快照落盘就消失——而知识库自动生成正是从快照读的。
    if version < 3:
        cols = {r[1] for r in cur.execute("PRAGMA table_info(column_meta)")}
        if "is_generated" not in cols:
            cur.execute(
                "ALTER TABLE column_meta ADD COLUMN is_generated INTEGER DEFAULT 0"
            )
        cur.execute("PRAGMA user_version = 3")
        conn.commit()
        version = 3

    # v3 → v4：agent 会话表（回合制澄清需要跨请求保留上下文）
    if version < 4:
        cur.executescript(DDL_SESSION)
        cur.execute("PRAGMA user_version = 4")
        conn.commit()
        version = 4

    return version


def _parse_tags(raw: str | None) -> list[str]:
    import json

    try:
        tags = json.loads(raw or "[]")
        return [str(t) for t in tags] if isinstance(tags, list) else []
    except (ValueError, TypeError):
        return []


def open_connection(db_path: str) -> sqlite3.Connection:
    """打开连接并完成迁移。调用方持有连接生命周期。"""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn
