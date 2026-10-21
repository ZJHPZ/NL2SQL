"""元数据存储（SQLite 实现）。

承载：数据源配置、元数据快照、查询历史。
不含业务判断——"哪些表该进 prompt"是 catalog 的事。

约定：
    * 连接由实例持有，`close()` 释放；**不做模块级单例**
    * 所有时间为 ISO8601 字符串（由调用方决定时区，本层不解释）
"""
from __future__ import annotations

import json

from ..config import NL2SQLConfig, SourceConfig, SourceOptions
from ..contracts.source import ColumnMeta, SnapshotMeta, TableMeta
from ..contracts.storage import DataSourceConfig, HistoryRecord
from ._dbapi import SqliteKV
from .migrations import open_connection


class SqliteMetaStore:
    """实现 `MetaStore` 契约。"""

    def __init__(self, db_path: str) -> None:
        self._conn: SqliteKV = open_connection(db_path)

    # ------------------------------------------------------------ 生命周期
    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------ 数据源
    def save_source(self, cfg: DataSourceConfig) -> None:
        self._conn.execute(
            "INSERT INTO data_source (source_id, adapter, label, options,"
            " credential_ref, semantic_path) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(source_id) DO UPDATE SET"
            " adapter=excluded.adapter, label=excluded.label,"
            " options=excluded.options, credential_ref=excluded.credential_ref,"
            " semantic_path=excluded.semantic_path",
            (cfg.source_id, cfg.adapter, cfg.label,
             json.dumps(cfg.options, ensure_ascii=False, default=str),
             cfg.credential_ref, cfg.semantic_path),
        )
        self._conn.commit()

    def get_source(self, source_id: str) -> DataSourceConfig | None:
        row = self._conn.execute(
            "SELECT * FROM data_source WHERE source_id = ?", (source_id,)
        ).fetchone()
        if row is None:
            return None
        return DataSourceConfig(
            source_id=row["source_id"], adapter=row["adapter"],
            label=row["label"] or "", options=json.loads(row["options"] or "{}"),
            credential_ref=row["credential_ref"], semantic_path=row["semantic_path"],
        )

    def list_sources(self) -> list[DataSourceConfig]:
        rows = self._conn.execute(
            "SELECT source_id FROM data_source ORDER BY source_id"
        ).fetchall()
        return [c for c in (self.get_source(r["source_id"]) for r in rows) if c]

    def delete_source(self, source_id: str) -> None:
        for table in ("data_source", "snapshot", "table_meta",
                      "column_meta", "foreign_key"):
            self._conn.execute(f"DELETE FROM {table} WHERE source_id = ?", (source_id,))
        self._conn.commit()

    # ------------------------------------------------------------ 快照与元数据
    def save_snapshot(self, meta: SnapshotMeta, tables: list[TableMeta]) -> None:
        sid = meta.source_id
        self._conn.execute(
            "INSERT INTO snapshot (source_id, fingerprint, captured_at, table_count)"
            " VALUES (?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET"
            " fingerprint=excluded.fingerprint, captured_at=excluded.captured_at,"
            " table_count=excluded.table_count",
            (sid, meta.fingerprint, meta.captured_at, meta.table_count),
        )
        # 全量替换该数据源的表结构，避免残留已删除的表
        self._conn.execute("DELETE FROM table_meta WHERE source_id = ?", (sid,))
        self._conn.execute("DELETE FROM column_meta WHERE source_id = ?", (sid,))
        self._conn.execute("DELETE FROM foreign_key WHERE source_id = ?", (sid,))

        for t in tables:
            self._conn.execute(
                "INSERT INTO table_meta (source_id, table_name, schema_name,"
                " comment, row_estimate) VALUES (?,?,?,?,?)",
                (sid, t.name, t.schema, t.comment, t.row_estimate),
            )
            for c in t.columns:
                self._conn.execute(
                    "INSERT INTO column_meta (source_id, table_name, column_name,"
                    " data_type, nullable, comment, is_pk, ordinal, is_generated)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (sid, t.name, c.name, c.data_type, int(c.nullable),
                     c.comment, int(c.is_primary_key), c.ordinal,
                     int(c.generated)),
                )
            for col, ref_table, ref_col in t.foreign_keys:
                self._conn.execute(
                    "INSERT OR IGNORE INTO foreign_key (source_id, table_name,"
                    " column_name, ref_table, ref_column) VALUES (?,?,?,?,?)",
                    (sid, t.name, col, ref_table, ref_col),
                )
        self._conn.commit()

    def get_tables(self, source_id: str) -> list[TableMeta]:
        trows = self._conn.execute(
            "SELECT * FROM table_meta WHERE source_id = ? ORDER BY table_name",
            (source_id,),
        ).fetchall()
        if not trows:
            return []

        crows = self._conn.execute(
            "SELECT * FROM column_meta WHERE source_id = ?"
            " ORDER BY table_name, ordinal",
            (source_id,),
        ).fetchall()
        frows = self._conn.execute(
            "SELECT * FROM foreign_key WHERE source_id = ?", (source_id,)
        ).fetchall()

        cols_by_table: dict[str, list[ColumnMeta]] = {}
        for c in crows:
            keys = c.keys()
            cols_by_table.setdefault(c["table_name"], []).append(ColumnMeta(
                name=c["column_name"], data_type=c["data_type"] or "",
                nullable=bool(c["nullable"]), comment=c["comment"] or "",
                is_primary_key=bool(c["is_pk"]), ordinal=c["ordinal"] or 0,
                generated=bool(c["is_generated"]) if "is_generated" in keys else False,
            ))
        fk_by_table: dict[str, list[tuple[str, str, str]]] = {}
        for f in frows:
            fk_by_table.setdefault(f["table_name"], []).append(
                (f["column_name"], f["ref_table"], f["ref_column"])
            )

        return [
            TableMeta(
                name=t["table_name"], schema=t["schema_name"],
                comment=t["comment"] or "", row_estimate=t["row_estimate"],
                columns=cols_by_table.get(t["table_name"], []),
                foreign_keys=fk_by_table.get(t["table_name"], []),
            )
            for t in trows
        ]

    def get_snapshot_meta(self, source_id: str) -> SnapshotMeta | None:
        row = self._conn.execute(
            "SELECT * FROM snapshot WHERE source_id = ?", (source_id,)
        ).fetchone()
        if row is None:
            return None
        return SnapshotMeta(
            source_id=row["source_id"], fingerprint=row["fingerprint"],
            captured_at=row["captured_at"], table_count=row["table_count"] or 0,
        )

    # ------------------------------------------------------------ 历史
    def add_history(self, rec: HistoryRecord) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO history (id, source_id, question,"
            " plan_fingerprint, sql, status, row_count, elapsed_ms, created_at,"
            " error_code, error_message, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec.id, rec.source_id, rec.question, rec.plan_fingerprint, rec.sql,
             rec.status, rec.row_count, rec.elapsed_ms, rec.created_at,
             rec.error_code, rec.error_message,
             json.dumps(rec.meta, ensure_ascii=False, default=str)),
        )
        self._conn.commit()

    def list_history(self, *, source_id: str | None = None,
                     limit: int = 50) -> list[HistoryRecord]:
        if source_id:
            rows = self._conn.execute(
                "SELECT * FROM history WHERE source_id = ?"
                " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (source_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM history ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._to_history(r) for r in rows]

    def get_history(self, history_id: str) -> HistoryRecord | None:
        row = self._conn.execute(
            "SELECT * FROM history WHERE id = ?", (history_id,)
        ).fetchone()
        return self._to_history(row) if row else None

    @staticmethod
    def _to_history(row) -> HistoryRecord:
        return HistoryRecord(
            id=row["id"], source_id=row["source_id"], question=row["question"],
            plan_fingerprint=row["plan_fingerprint"], sql=row["sql"] or "",
            status=row["status"], row_count=row["row_count"] or 0,
            elapsed_ms=row["elapsed_ms"] or 0, created_at=row["created_at"],
            error_code=row["error_code"], error_message=row["error_message"],
            meta=json.loads(row["meta"] or "{}"),
        )


# ---------------------------------------------------------------- 配置转换
# 这些函数放在存储层而不是 config.py：
# 配置层不应依赖存储层，转换是持久化细节。


def source_config_to_storage(cfg: SourceConfig) -> DataSourceConfig:
    """核心配置 → 存储记录。密码不在其中（只有 credential_ref）。"""
    opts = cfg.options
    flat = {k: v for k, v in {
        "path": opts.path, "host": opts.host, "port": opts.port,
        "user": opts.user, "database": opts.database, "charset": opts.charset,
    }.items() if v is not None}
    flat.update(opts.params)
    return DataSourceConfig(
        source_id=cfg.source_id, adapter=cfg.adapter, label=cfg.label,
        options=flat, credential_ref=cfg.credential_ref,
        semantic_path=cfg.semantic_path,
    )


def storage_to_source_config(rec: DataSourceConfig) -> SourceConfig:
    """存储记录 → 核心配置。用于从库里恢复配置（S8 多数据源）。"""
    o = rec.options or {}
    known = {"path", "host", "port", "user", "database", "charset"}
    params = {k: v for k, v in o.items() if k not in known}
    return SourceConfig(
        source_id=rec.source_id, adapter=rec.adapter, label=rec.label,
        options=SourceOptions(
            path=o.get("path"), host=o.get("host"), port=o.get("port"),
            user=o.get("user"), database=o.get("database"),
            charset=o.get("charset"), params=params,
        ),
        credential_ref=rec.credential_ref, semantic_path=rec.semantic_path,
    )


def config_to_storage(config: NL2SQLConfig) -> list[DataSourceConfig]:
    return [source_config_to_storage(c) for c in config.sources.values()]
