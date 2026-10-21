"""知识库存储（SQLite + FTS5）。

数据表：
    knowledge_entry   当前版本（`enabled` 为停用开关）
    knowledge_rev     全量历史修订（diff / 回滚依据）
    knowledge_fts     BM25 全文索引

关键约定
--------
1. **只有 `enabled=1` 的条目参与检索**。
2. **每次 upsert 先写 `knowledge_rev` 再更新主表**——免审不等于无版本。
3. **rollback 不回退版本号**：从历史恢复内容时生成新版本，
   否则历史会分叉（v3 既是旧的 v2 又是新的 v4，无法解释）。
4. **删除是软删**（`enabled=0` + 保留历史），保留可恢复性。

FTS5 与主表通过 `key` 列关联（不是 rowid），主表更新后需显式同步。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..contracts.knowledge import KnowledgeEntry, KnowledgeType, Revision
from ._dbapi import SqliteKV
from .migrations import open_connection
from .text import index_text, match_expr


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _row_to_entry(row) -> KnowledgeEntry:
    return KnowledgeEntry(
        key=row["key"],
        type=row["type"],
        title=row["title"] or "",
        body=row["body"] or "",
        source_id=row["source_id"],
        tags=json.loads(row["tags"] or "[]"),
        enabled=bool(row["enabled"]),
        origin=row["origin"] or "user",
        source_ref=row["source_ref"] or "",
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
        version=row["version"] or 1,
        extra=json.loads(row["extra"] or "{}"),
    )


class SqliteKnowledgeStore:
    """实现 `KnowledgeStore` 契约。"""

    def __init__(self, db_path: str) -> None:
        self._conn: SqliteKV = open_connection(db_path)

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------ 写
    def upsert(self, entry: KnowledgeEntry, *, note: str = "") -> KnowledgeEntry:
        existing = self.get(entry.key)
        now = _now_iso()

        if existing is None:
            entry.created_at = entry.created_at or now
            entry.updated_at = now
            entry.version = 1
        else:
            entry.created_at = existing.created_at or now
            entry.updated_at = now
            # 内容未变则不产生新版本，避免版本号被无意义地推高
            if (entry.title, entry.body, entry.type, entry.tags,
                    entry.source_id, entry.enabled) == (
                    existing.title, existing.body, existing.type, existing.tags,
                    existing.source_id, existing.enabled):
                return existing
            entry.version = existing.version + 1

        self._conn.execute(
            "INSERT INTO knowledge_entry (key, type, title, body, source_id, tags,"
            " enabled, origin, source_ref, created_at, updated_at, version, extra)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET"
            " type=excluded.type, title=excluded.title, body=excluded.body,"
            " source_id=excluded.source_id, tags=excluded.tags,"
            " enabled=excluded.enabled, origin=excluded.origin,"
            " source_ref=excluded.source_ref, updated_at=excluded.updated_at,"
            " version=excluded.version, extra=excluded.extra",
            (entry.key, entry.type, entry.title, entry.body, entry.source_id,
             json.dumps(entry.tags, ensure_ascii=False), int(entry.enabled),
             entry.origin, entry.source_ref, entry.created_at, entry.updated_at,
             entry.version, json.dumps(entry.extra, ensure_ascii=False, default=str)),
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO knowledge_rev (key, version, title, body,"
            " changed_at, changed_by, note) VALUES (?,?,?,?,?,?,?)",
            (entry.key, entry.version, entry.title, entry.body, now,
             entry.extra.get("changed_by", ""), note),
        )
        self._sync_fts(entry)
        self._conn.commit()
        return entry

    def _sync_fts(self, entry: KnowledgeEntry) -> None:
        """同步 FTS 索引。

        **索引的是预切分文本，不是原文**——unicode61 不切分中文，
        直接索引原文会让中文检索静默失效。详见 storage/text.py。
        """
        self._conn.execute("DELETE FROM knowledge_fts WHERE key = ?", (entry.key,))
        if entry.enabled:
            self._conn.execute(
                "INSERT INTO knowledge_fts (key, title, body, tags) VALUES (?,?,?,?)",
                (entry.key,
                 index_text(entry.title),
                 index_text(entry.body),
                 index_text(" ".join(entry.tags))),
            )

    def set_enabled(self, key: str, enabled: bool) -> None:
        self._conn.execute(
            "UPDATE knowledge_entry SET enabled = ?, updated_at = ? WHERE key = ?",
            (int(enabled), _now_iso(), key),
        )
        entry = self.get(key)
        if entry:
            self._sync_fts(entry)
        self._conn.commit()

    def rollback(self, key: str, version: int) -> KnowledgeEntry:
        """回滚到历史版本：**生成新版本，不复用旧版本号**。"""
        rev = self._conn.execute(
            "SELECT * FROM knowledge_rev WHERE key = ? AND version = ?",
            (key, version),
        ).fetchone()
        if rev is None:
            from ..errors import VersionNotFoundError

            raise VersionNotFoundError(f"知识 {key} 不存在版本 v{version}")

        current = self.get(key)
        if current is None:
            from ..errors import VersionNotFoundError

            raise VersionNotFoundError(f"知识不存在: {key}")

        current.title = rev["title"] or ""
        current.body = rev["body"] or ""
        return self.upsert(current, note=f"回滚到 v{version}")

    def delete(self, key: str) -> None:
        """软删：停用并移出检索，历史保留。"""
        self.set_enabled(key, False)

    # ------------------------------------------------------------ 读
    def get(self, key: str) -> KnowledgeEntry | None:
        row = self._conn.execute(
            "SELECT * FROM knowledge_entry WHERE key = ?", (key,)
        ).fetchone()
        return _row_to_entry(row) if row else None

    def list(self, *, source_id: str | None = None,
             types: tuple[KnowledgeType, ...] = (),
             include_disabled: bool = False) -> list[KnowledgeEntry]:
        sql = "SELECT * FROM knowledge_entry WHERE 1=1"
        params: list[Any] = []
        if source_id is not None:
            # source_id=None 的条目视为全局知识，任何数据源都可见
            sql += " AND (source_id = ? OR source_id IS NULL)"
            params.append(source_id)
        if types:
            sql += f" AND type IN ({','.join('?' * len(types))})"
            params.extend(types)
        if not include_disabled:
            sql += " AND enabled = 1"
        sql += " ORDER BY type, key"
        rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [_row_to_entry(r) for r in rows]

    def history(self, key: str) -> list[Revision]:
        rows = self._conn.execute(
            "SELECT * FROM knowledge_rev WHERE key = ? ORDER BY version DESC",
            (key,),
        ).fetchall()
        return [
            Revision(
                key=r["key"], version=r["version"], body=r["body"] or "",
                title=r["title"] or "", changed_at=r["changed_at"] or "",
                changed_by=r["changed_by"] or "", note=r["note"] or "",
            )
            for r in rows
        ]

    # ------------------------------------------------------------ BM25
    def search_fts(self, text: str, *, source_id: str | None = None,
                   types: tuple[KnowledgeType, ...] = (), top_k: int = 8,
                   ) -> list[tuple[str, float]]:
        """FTS5 BM25 检索，返回 (key, score)。score 越大越相关。

        `bm25()` 在 SQLite 中返回负数（越小越相关），这里取负号统一方向。
        """
        if not text.strip():
            return []
        expr = _to_match_expr(text)
        if not expr:
            return []
        sql = (
            "SELECT f.key AS key, bm25(knowledge_fts) AS score"
            " FROM knowledge_fts f"
            " JOIN knowledge_entry e ON e.key = f.key"
            " WHERE knowledge_fts MATCH ? AND e.enabled = 1"
        )
        params: list[Any] = [expr]
        if source_id is not None:
            sql += " AND (e.source_id = ? OR e.source_id IS NULL)"
            params.append(source_id)
        if types:
            sql += f" AND e.type IN ({','.join('?' * len(types))})"
            params.extend(types)
        sql += " ORDER BY score LIMIT ?"
        params.append(top_k)

        try:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        except Exception:
            # FTS 查询语法问题不应打断主流程，降级为无结果
            return []
        return [(r["key"], -float(r["score"])) for r in rows]


def _to_match_expr(text: str) -> str:
    """把自然语言问题转成 FTS5 MATCH 表达式。

    实现委托给 `storage/text.py`：**入库与查询必须用同一套切分逻辑**，
    否则索引里的 token 和查询里的 token 对不上，检索会静默失效。
    """
    return match_expr(text)
