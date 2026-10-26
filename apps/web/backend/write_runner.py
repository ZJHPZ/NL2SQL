"""受控写执行层（P1）：在独立的可写 SQLite 连接里「先演后写」。

为什么单独一个模块，而不复用只读适配器
--------------------------------------
* 只读适配器（`nl2sql.sources.sqlite.SqliteAdapter`）以 `mode=ro` 打开库，
  这是只读防线的最后一道物理保险，不能被写路径借用；
* 写入必须走 `BEGIN IMMEDIATE` 事务：预演 = 真正执行一次再 `ROLLBACK`，
  提交 = 再执行一次后 `COMMIT`。影响行数上限在事务内核对，超限就回滚，
  所以「预演通过 → 提交」之间不会出现行数悄悄变大的情况（提交时还会复核）。
* 业务数据源仍然是用户的库；任务库（tasks.db）永远不在这里被写——
  路由器会显式拒绝把任务库当写目标。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from nl2sql.guard.write import WriteCheck

__all__ = ["WriteRunError", "dry_run", "commit", "table_quote"]


class WriteRunError(Exception):
    """受控写执行失败（带可展示的规则名与语句，便于前端与审计复用）。"""

    def __init__(self, message: str, *, rule: str = "write_failed",
                 sql: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.rule = rule
        self.sql = sql


def table_quote(name: str) -> str:
    """把表名安全地包成 SQLite 标识符（这里只用于内部取样查询）。"""
    return '"' + str(name).replace('"', '""') + '"'


def _connect(db_path: str, *, timeout_s: float = 10.0) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=max(1.0, timeout_s),
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _sample(conn: sqlite3.Connection, check: WriteCheck, *, limit: int = 20
            ) -> list[dict[str, Any]]:
    """取「受影响行」的样本（最多 20 行）。取样失败不影响主流程。"""
    if check.kind == "insert":
        return []
    where = (check.where_sql or "").strip()
    if not where:
        return []
    sql = f"SELECT * FROM {table_quote(check.table)} {where} LIMIT ?"
    try:
        rows = conn.execute(sql, (int(limit),)).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def _inserted_row(conn: sqlite3.Connection, check: WriteCheck,
                  rowid: int | None) -> list[dict[str, Any]]:
    if rowid is None:
        return []
    try:
        row = conn.execute(
            f"SELECT * FROM {table_quote(check.table)} WHERE rowid = ?",
            (int(rowid),),
        ).fetchone()
    except sqlite3.Error:
        return []
    return [dict(row)] if row is not None else []


def _rowcount(cursor: sqlite3.Cursor) -> int:
    count = cursor.rowcount
    return int(count) if count is not None and count >= 0 else 0


def _affected_limit_error(rows: int, max_affected: int) -> WriteRunError:
    return WriteRunError(
        f"影响行数 {rows} 超过项目上限 {max_affected}；"
        "已回滚，未写任何数据。请收紧 WHERE 或调大项目写入上限（需谨慎）",
        rule="affected_limit",
    )


def dry_run(
    db_path: str,
    check: WriteCheck,
    *,
    max_affected: int = 100,
    sample_limit: int = 20,
) -> dict[str, Any]:
    """预演：在事务里真的执行一次，记录影响行数与前后样本，然后回滚。"""
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        before: list[dict[str, Any]] = []
        after: list[dict[str, Any]] = []
        try:
            before = _sample(conn, check, limit=sample_limit)
            cursor = conn.execute(check.sql)
            rows = _rowcount(cursor)
            if rows > max_affected:
                conn.execute("ROLLBACK")
                raise _affected_limit_error(rows, max_affected)
            if check.kind == "insert":
                after = _inserted_row(conn, check, cursor.lastrowid)
            else:
                after = _sample(conn, check, limit=sample_limit)
            conn.execute("ROLLBACK")
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise WriteRunError(
                f"预演执行失败：{exc}（已回滚，未写任何数据）",
                rule="sqlite_error", sql=check.sql,
            ) from exc
        return {
            "rows": rows,
            "before": before,
            "after": after,
            "kind": check.kind,
            "table": check.table,
        }
    finally:
        conn.close()


def commit(
    db_path: str,
    check: WriteCheck,
    *,
    max_affected: int = 100,
) -> dict[str, Any]:
    """提交：事务内再执行一次；行数超限或失败则整体回滚。"""
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        before: list[dict[str, Any]] = []
        after: list[dict[str, Any]] = []
        try:
            before = _sample(conn, check)
            cursor = conn.execute(check.sql)
            rows = _rowcount(cursor)
            if rows > max_affected:
                conn.execute("ROLLBACK")
                raise _affected_limit_error(rows, max_affected)
            if check.kind == "insert":
                after = _inserted_row(conn, check, cursor.lastrowid)
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise WriteRunError(
                f"提交失败：{exc}（已回滚，未写任何数据）",
                rule="sqlite_error", sql=check.sql,
            ) from exc
        return {"rows": rows, "before": before, "after": after}
    finally:
        conn.close()
