"""原生同步（一期）：表级全量/增量同步的执行器。

边界
----
* **读**走项目已装配好的只读适配器（sqlite / mysql / postgres），
  与问答、任务执行共用同一个连接层；不额外开可写连接。
* **写**只写项目绑定的 SQLite 写库（受控写目标），用独立事务；
  非 SQLite 目标在 P1 一律拒绝（`_sync_target` 校验）。
* 与受控写共用同一套防线思路：**先预演、后确认**——预演只读源表、
  不改目标库；提交前重新读一遍源表，行数/水位不一致就要求重新预演。

目标表不存在时按源表列名/类型自动建表（只做这一种 DDL）。类型映射刻意
保守：整数 → INTEGER，浮点/数值 → REAL，其余 → TEXT；不复制主键/索引/
约束——同步任务是「搬数据」，不是 schema 迁移工具。
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from nl2sql.contracts.source import QueryResult, TableMeta

from .task_exec import _pipeline

_NUMERIC_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
_IDENT_OK_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


class SyncError(Exception):
    """同步执行失败。`rule` 给前端/审计一个稳定标签（affected_limit 等）。"""

    def __init__(self, message: str, *, rule: str = "sync_failed") -> None:
        super().__init__(message)
        self.rule = rule


# ---------------------------------------------------------------- 连接 / 工具
def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _quote(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return repr(value)
    return str(value)


def _sql_literal(value: str) -> str:
    """把水位线的值安全地拼进 SQL（适配器 execute 只接收 SQL 文本）。

    能按数字解释就按数字，否则按字符串字面量转义——水位线的值来自
    上一次同步读出的真实单元格，但仍不直接信任拼接。
    """
    text = str(value)
    if _NUMERIC_RE.match(text):
        return text
    return "'" + text.replace("'", "''") + "'"


def _adapter(engine: Any, source_id: str) -> Any:
    _, adapters, _, _ = _pipeline(engine)
    adapter = adapters.get(source_id)
    if adapter is None:
        raise SyncError(
            f"源数据源不可用：{source_id}", rule="source_unavailable")
    return adapter


# ---------------------------------------------------------------- 读源表
def source_table_meta(engine: Any, source_id: str, table: str) -> TableMeta:
    adapter = _adapter(engine, source_id)
    try:
        tables = adapter.introspect(include_comments=False, count_rows=False)
    except Exception as exc:  # noqa: BLE001
        raise SyncError(
            f"读取源数据源元数据失败：{exc}", rule="source_introspect_failed"
        ) from exc
    for meta in tables:
        if meta.name == table:
            return meta
    raise SyncError(
        f"源表「{table}」在数据源 {source_id} 中不存在", rule="source_table_missing")


def read_sync_rows(
    engine: Any,
    config: dict[str, Any],
    *,
    after: str = "",
    max_rows: int,
) -> tuple[TableMeta, QueryResult]:
    """读源表待同步的数据（最多 max_rows + 1 行，超上限报 affected_limit）。"""
    meta = source_table_meta(engine, config["source_id"], config["table"])
    table = config["table"]
    mode = config["mode"]
    watermark = config.get("watermark") or ""

    sql = f"SELECT * FROM {_quote(table)}"
    if mode == "incremental" and after:
        if not watermark:
            raise SyncError(
                "增量同步缺少递增列（watermark）", rule="watermark_missing")
        if not any(c.name == watermark for c in meta.columns):
            raise SyncError(
                f"增量列「{watermark}」不在源表 {table} 中",
                rule="watermark_missing",
            )
        sql += f" WHERE {_quote(watermark)} > {_sql_literal(after)}" \
               f" ORDER BY {_quote(watermark)}"

    adapter = _adapter(engine, config["source_id"])
    timeout_s = 60.0
    try:
        result = adapter.execute(sql, limit=max_rows + 1, timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001
        raise SyncError(f"读取源表失败：{exc}", rule="source_read_failed") from exc

    # execute(limit=N) 返回最多 N 行；传 max_rows+1 时若真拿到 max_rows+1 行，
    # 说明待同步行数已超过上限。
    if result.row_count > max_rows:
        raise SyncError(
            f"待同步行数超过上限 {max_rows}；请缩小范围或调大项目写入上限",
            rule="affected_limit",
        )
    return meta, result


# ---------------------------------------------------------------- 目标表
def target_table_exists(db_path: str, table: str) -> bool:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    return row is not None


def _count_rows(conn: sqlite3.Connection, table: str) -> int:
    try:
        return int(conn.execute(
            f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0])
    except sqlite3.Error:
        return 0


def _sqlite_type(data_type: str) -> str:
    text = (data_type or "").upper()
    if "INT" in text:
        return "INTEGER"
    if any(token in text for token in ("REAL", "FLOA", "DOUB", "NUM", "DEC")):
        return "REAL"
    return "TEXT"


def _ensure_target_table(
    conn: sqlite3.Connection, table: str, meta: TableMeta
) -> bool:
    """目标表不存在时按源列建表；返回是否新建。"""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if row is not None:
        return False
    if not meta.columns:
        raise SyncError(
            f"源表「{meta.name}」没有可同步的列", rule="source_columns_missing")
    for column in meta.columns:
        if not _IDENT_OK_RE.fullmatch(column.name):
            raise SyncError(
                f"列名「{column.name}」含不支持字符（P1 只支持字母/数字/下划线）",
                rule="column_unsupported",
            )
    cols = ", ".join(
        f"{_quote(c.name)} {_sqlite_type(c.data_type)}" for c in meta.columns)
    conn.execute(f"CREATE TABLE IF NOT EXISTS {_quote(table)} ({cols})")
    return True


# ---------------------------------------------------------------- 预演 / 提交
def preview(
    *,
    db_path: str,
    engine: Any,
    config: dict[str, Any],
    state: dict[str, Any] | None,
    max_affected: int,
    sample_limit: int = 20,
) -> dict[str, Any]:
    """预演：只读源表 + 读目标表现状，不修改任何数据。"""
    mode = config["mode"]
    watermark = config.get("watermark") or ""
    after = ""
    if mode == "incremental" and state:
        after = str(state.get("watermark") or "")
    meta, result = read_sync_rows(
        engine, config, after=after, max_rows=int(max_affected))

    columns = list(result.columns)
    rows = [list(row) for row in result.rows]
    sample = [
        {str(col): _jsonable(value) for col, value in zip(columns, row)}
        for row in rows[:sample_limit]
    ]

    target_exists = target_table_exists(db_path, config["target_table"])
    target_rows = 0
    if target_exists:
        with _connect(db_path) as conn:
            target_rows = _count_rows(conn, config["target_table"])

    watermark_after = after
    if mode == "incremental" and watermark and rows:
        try:
            index = columns.index(watermark)
        except ValueError as exc:
            raise SyncError(
                f"增量列「{watermark}」不在源表结果中", rule="watermark_missing"
            ) from exc
        watermark_after = str(_jsonable(rows[-1][index]))

    return {
        "rows": len(rows),
        "columns": columns,
        "sample": sample,
        "target_table": config["target_table"],
        "target_exists": target_exists,
        "target_rows": target_rows,
        "watermark_before": after,
        "watermark_after": watermark_after,
        "mode": mode,
    }


def commit(
    *,
    db_path: str,
    engine: Any,
    config: dict[str, Any],
    state: dict[str, Any] | None,
    max_affected: int,
    expected_rows: int,
    expected_watermark_after: str,
) -> dict[str, Any]:
    """提交：重新读源表核对后，在目标 SQLite 单事务里写入。"""
    mode = config["mode"]
    after = ""
    if mode == "incremental" and state:
        after = str(state.get("watermark") or "")
    meta, result = read_sync_rows(
        engine, config, after=after, max_rows=int(max_affected))

    columns = list(result.columns)
    rows = [list(row) for row in result.rows]
    if len(rows) != int(expected_rows):
        raise SyncError(
            "源表数据在预演后发生变化（行数不一致）；请重新预演",
            rule="source_changed",
        )
    watermark_after = expected_watermark_after
    watermark = config.get("watermark") or ""
    if mode == "incremental" and watermark and rows:
        try:
            index = columns.index(watermark)
        except ValueError as exc:
            raise SyncError(
                f"增量列「{watermark}」不在源表结果中", rule="watermark_missing"
            ) from exc
        current = str(_jsonable(rows[-1][index]))
        if current != str(expected_watermark_after):
            raise SyncError(
                "源表水位在预演后发生变化；请重新预演", rule="source_changed")

    target_table = config["target_table"]
    conn = _connect(db_path)
    created_table = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        created_table = _ensure_target_table(conn, target_table, meta)
        if mode == "full":
            conn.execute(f"DELETE FROM {_quote(target_table)}")
        if rows:
            placeholders = ", ".join("?" for _ in columns)
            col_sql = ", ".join(_quote(c) for c in columns)
            conn.executemany(
                f"INSERT INTO {_quote(target_table)} ({col_sql})"
                f" VALUES ({placeholders})",
                [tuple(row) for row in rows],
            )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise SyncError(f"写入目标库失败：{exc}", rule="sqlite_error") from exc
    except SyncError:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    with _connect(db_path) as conn2:
        target_rows = _count_rows(conn2, target_table)
    return {
        "rows": len(rows),
        "target_rows": target_rows,
        "watermark_after": watermark_after,
        "target_table": target_table,
        "created_table": created_table,
        "mode": mode,
    }
