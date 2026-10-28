"""受控写执行层（P1）：在独立的可写连接里「先演后写」。

为什么单独一个模块，而不复用只读适配器
-------------------------------------
* 只读适配器（`nl2sql.sources.sqlite.SqliteAdapter` / 各 SQL 适配器）以
  `read_only` 打开连接，这是只读防线的最后一道物理保险，不能被写路径借用；
* 写入必须走显式事务：预演 = 真正执行一次再 `ROLLBACK`，提交 = 再执行一次后
  `COMMIT`。影响行数上限在事务内核对，超限就回滚，所以「预演通过 → 提交」之间
  不会出现行数悄悄变大的情况（提交时还会复核）；
* 业务数据源仍然是用户的库；任务库（tasks.db）永远不在这里被写——
  路由器会显式拒绝把任务库当写目标；
* 目标适配器目前支持 SQLite（测试库）与 MySQL（实验库）：SQLite 用
  `BEGIN IMMEDIATE` + `?` 占位符，MySQL 用 pymysql 事务 + `%s` 占位符；
  标识符引用、类型映射、取样查询按目标方言分派。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from nl2sql.guard.write import WriteCheck

__all__ = ["WriteRunError", "dry_run", "commit", "table_quote"]

_TARGET_ADAPTERS = ("sqlite", "mysql")
_MYSQL_SYSTEM_DBS = {"mysql", "information_schema", "performance_schema", "sys"}


class WriteRunError(Exception):
    """受控写执行失败（带可展示的规则名与语句，便于前端与审计复用）。"""

    def __init__(self, message: str, *, rule: str = "write_failed",
                 sql: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.rule = rule
        self.sql = sql


def table_quote(name: str, adapter: str = "sqlite") -> str:
    """把表名安全地包成目标方言的标识符（只用于内部取样查询）。"""
    text = str(name)
    if adapter == "mysql":
        return "`" + text.replace("`", "``") + "`"
    return '"' + text.replace('"', '""') + '"'


def _target_adapter(target: dict[str, Any]) -> str:
    adapter = str((target or {}).get("adapter") or "").strip().lower()
    if adapter not in _TARGET_ADAPTERS:
        raise WriteRunError(
            "受控写目标只支持 SQLite 测试库或 MySQL 实验库；"
            "PostgreSQL / JDBC 的写事务与回滚语义尚未接入",
            rule="target_unsupported",
        )
    return adapter


def _connect_sqlite(target: dict[str, Any], *,
                    timeout_s: float = 10.0) -> sqlite3.Connection:
    conn = sqlite3.connect(str(target.get("path") or ""),
                           timeout=max(1.0, timeout_s),
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _connect_mysql(target: dict[str, Any]) -> Any:
    try:
        import pymysql  # 仅 MySQL 目标按需加载
        from pymysql.constants import CLIENT as _MYSQL_CLIENT
    except ImportError as exc:  # pragma: no cover - 环境缺驱动时给出可读错误
        raise WriteRunError(
            "写目标是 MySQL，但当前环境缺少 pymysql；请先安装可选依赖 "
            "（pip install pymysql）",
            rule="target_driver_missing",
        ) from exc
    database = str(target.get("database") or "").strip()
    if database.lower() in _MYSQL_SYSTEM_DBS:
        raise WriteRunError(
            f"不能把 MySQL 系统库 {database} 当作写入目标",
            rule="target_unsafe",
        )
    try:
        return pymysql.connect(
            host=str(target.get("host") or ""),
            port=int(target.get("port") or 3306),
            user=str(target.get("user") or ""),
            password=str(target.get("password") or ""),
            database=database,
            connect_timeout=5.0,
            charset="utf8mb4",
            autocommit=False,
            cursorclass=pymysql.cursors.DictCursor,
            # 用「匹配行数」而不是「值真正变化的行数」，与 SQLite 的 changes()
            # 语义对齐：即使写入的值和原来相同，也计入影响行数，上限更保守。
            client_flag=_MYSQL_CLIENT.FOUND_ROWS,
        )
    except pymysql.MySQLError as exc:
        raise WriteRunError(
            f"连接 MySQL 写入目标失败：{exc}（未写任何数据）",
            rule="target_connect_failed",
        ) from exc


def _quote_for(adapter: str, name: str) -> str:
    return table_quote(name, adapter)


def _sample_sqlite(conn: sqlite3.Connection, check: WriteCheck, *,
                   limit: int = 20) -> list[dict[str, Any]]:
    """取「受影响行」的样本（最多 N 行）。取样失败不影响主流程。"""
    if check.kind == "insert":
        return []
    where = (check.where_sql or "").strip()
    if not where:
        return []
    sql = f"SELECT * FROM {_quote_for('sqlite', check.table)} {where} LIMIT ?"
    try:
        rows = conn.execute(sql, (int(limit),)).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def _sample_mysql(conn: Any, check: WriteCheck, *,
                  limit: int = 20) -> list[dict[str, Any]]:
    if check.kind == "insert":
        return []
    where = (check.where_sql or "").strip()
    if not where:
        return []
    sql = f"SELECT * FROM {_quote_for('mysql', check.table)} {where} LIMIT %s"
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (int(limit),))
            rows = cur.fetchall()
    except Exception:  # noqa: BLE001 - 取样失败不影响主流程
        return []
    return [_dict_row(row) for row in rows]


def _inserted_row_sqlite(conn: sqlite3.Connection, check: WriteCheck,
                         rowid: int | None) -> list[dict[str, Any]]:
    if rowid is None:
        return []
    try:
        row = conn.execute(
            f"SELECT * FROM {_quote_for('sqlite', check.table)} WHERE rowid = ?",
            (int(rowid),),
        ).fetchone()
    except sqlite3.Error:
        return []
    return [dict(row)] if row is not None else []


def _mysql_auto_pk(conn: Any, table: str) -> str:
    """找出表的自增主键列名；没有就返回空串。"""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
                "AND COLUMN_KEY = 'PRI' AND EXTRA LIKE '%%auto_increment%%' "
                "LIMIT 1",
                (table,),
            )
            row = cur.fetchone()
    except Exception:  # noqa: BLE001
        return ""
    if not row:
        return ""
    if isinstance(row, dict):
        return str(list(row.values())[0] if row else "")
    return str(row[0])


def _inserted_row_mysql(conn: Any, check: WriteCheck,
                        rowid: int | None) -> list[dict[str, Any]]:
    if rowid is None:
        return []
    pk = _mysql_auto_pk(conn, check.table)
    if not pk:
        return []
    sql = (f"SELECT * FROM {_quote_for('mysql', check.table)} "
           f"WHERE {_quote_for('mysql', pk)} = %s")
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (int(rowid),))
            row = cur.fetchone()
    except Exception:  # noqa: BLE001
        return []
    return [_dict_row(row)] if row is not None else []


def _dict_row(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    if isinstance(row, (tuple, list)):
        return {str(i): value for i, value in enumerate(row)}
    return {}


def _affected_limit_error(rows: int, max_affected: int) -> WriteRunError:
    return WriteRunError(
        f"影响行数 {rows} 超过项目上限 {max_affected}；"
        "已回滚，未写任何数据。请收紧 WHERE 或调大项目写入上限（需谨慎）",
        rule="affected_limit",
    )


def _execute_sqlite(conn: sqlite3.Connection, check: WriteCheck,
                    *, sample_limit: int) -> tuple[int, list[dict[str, Any]],
                                                    list[dict[str, Any]]]:
    before = _sample_sqlite(conn, check, limit=sample_limit)
    cursor = conn.execute(check.sql)
    count = cursor.rowcount
    rows = int(count) if count is not None and count >= 0 else 0
    after: list[dict[str, Any]] = []
    if check.kind == "insert":
        after = _inserted_row_sqlite(conn, check, cursor.lastrowid)
    else:
        after = _sample_sqlite(conn, check, limit=sample_limit)
    return rows, before, after


def _execute_mysql(conn: Any, check: WriteCheck,
                   *, sample_limit: int) -> tuple[int, list[dict[str, Any]],
                                                   list[dict[str, Any]]]:
    before = _sample_mysql(conn, check, limit=sample_limit)
    with conn.cursor() as cur:
        cur.execute(check.sql)
        count = cur.rowcount
        rowid = cur.lastrowid
    rows = int(count) if count is not None and count >= 0 else 0
    after: list[dict[str, Any]] = []
    if check.kind == "insert":
        after = _inserted_row_mysql(conn, check, rowid)
    else:
        after = _sample_mysql(conn, check, limit=sample_limit)
    return rows, before, after


def dry_run(
    target: dict[str, Any],
    check: WriteCheck,
    *,
    max_affected: int = 100,
    sample_limit: int = 20,
) -> dict[str, Any]:
    """预演：在事务里真的执行一次，记录影响行数与前后样本，然后回滚。"""
    adapter = _target_adapter(target)
    conn = _connect_sqlite(target) if adapter == "sqlite" else _connect_mysql(target)
    try:
        if adapter == "sqlite":
            conn.execute("BEGIN IMMEDIATE")  # type: ignore[union-attr]
        else:
            conn.begin()
        try:
            rows, before, after = (
                _execute_sqlite(conn, check, sample_limit=sample_limit)
                if adapter == "sqlite"
                else _execute_mysql(conn, check, sample_limit=sample_limit)
            )
            if rows > max_affected:
                if adapter == "sqlite":
                    conn.execute("ROLLBACK")  # type: ignore[union-attr]
                else:
                    conn.rollback()
                raise _affected_limit_error(rows, max_affected)
            if adapter == "sqlite":
                conn.execute("ROLLBACK")  # type: ignore[union-attr]
            else:
                conn.rollback()
        except WriteRunError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一转成可展示错误
            try:
                if adapter == "sqlite":
                    conn.execute("ROLLBACK")  # type: ignore[union-attr]
                else:
                    conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise WriteRunError(
                f"预演执行失败：{exc}（已回滚，未写任何数据）",
                rule=f"{adapter}_error", sql=check.sql,
            ) from exc
        return {
            "rows": rows,
            "before": before,
            "after": after,
            "kind": check.kind,
            "table": check.table,
            "target_adapter": adapter,
        }
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def commit(
    target: dict[str, Any],
    check: WriteCheck,
    *,
    max_affected: int = 100,
) -> dict[str, Any]:
    """提交：事务内再执行一次；行数超限或失败则整体回滚。"""
    adapter = _target_adapter(target)
    conn = _connect_sqlite(target) if adapter == "sqlite" else _connect_mysql(target)
    try:
        if adapter == "sqlite":
            conn.execute("BEGIN IMMEDIATE")  # type: ignore[union-attr]
        else:
            conn.begin()
        try:
            rows, before, after = (
                _execute_sqlite(conn, check, sample_limit=20)
                if adapter == "sqlite"
                else _execute_mysql(conn, check, sample_limit=20)
            )
            if rows > max_affected:
                if adapter == "sqlite":
                    conn.execute("ROLLBACK")  # type: ignore[union-attr]
                else:
                    conn.rollback()
                raise _affected_limit_error(rows, max_affected)
            if adapter == "sqlite":
                conn.execute("COMMIT")  # type: ignore[union-attr]
            else:
                conn.commit()
        except WriteRunError:
            raise
        except Exception as exc:  # noqa: BLE001
            try:
                if adapter == "sqlite":
                    conn.execute("ROLLBACK")  # type: ignore[union-attr]
                else:
                    conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise WriteRunError(
                f"提交失败：{exc}（已回滚，未写任何数据）",
                rule=f"{adapter}_error", sql=check.sql,
            ) from exc
        return {"rows": rows, "before": before, "after": after,
                "target_adapter": adapter}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
