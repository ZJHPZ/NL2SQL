"""原生同步（一期）：表级全量/增量同步的执行器。

边界
----
* **读**走项目已装配好的只读适配器（sqlite / mysql / postgres），
  与问答、任务执行共用同一个连接层；不额外开可写连接。
* **写**写项目绑定的同步目标：SQLite 测试库（本地文件）或 MySQL 实验库。
  目标为 MySQL 时使用项目 secrets 里的专用账号直连，只对绑定库生效；
  PostgreSQL / JDBC 目标在 P1 一律拒绝（`_sync_target` 校验）。
* 与受控写共用同一套防线思路：**先预演、后确认**——预演只读源表、
  不改目标库；提交前重新读一遍源表，行数/水位不一致就要求重新预演。

目标表不存在时按源表列名/类型自动建表（只做这一种 DDL）。类型映射刻意
保守：整数 → INTEGER/INT，浮点/数值 → REAL/DOUBLE，其余 → TEXT；不复制
主键/索引/约束——同步任务是「搬数据」，不是 schema 迁移工具。
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from nl2sql.contracts.source import QueryResult, TableMeta

from .task_exec import _pipeline

_NUMERIC_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
_IDENT_OK_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_TARGET_ADAPTERS = ("sqlite", "mysql")
_MYSQL_SYSTEM_DBS = ("mysql", "information_schema", "performance_schema", "sys")


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


def _target_adapter(target: dict[str, Any]) -> str:
    adapter = str(target.get("adapter") or "sqlite").strip().lower()
    if adapter not in _TARGET_ADAPTERS:
        raise SyncError(
            f"同步目标暂不支持 {adapter or '未知'}；P1 支持 SQLite 测试库与 "
            "MySQL 实验库",
            rule="target_unsupported",
        )
    return adapter


def _mysql_connect(target: dict[str, Any]) -> Any:
    """连 MySQL 实验库；pymysql 是可选的 web 依赖，缺了如实报错。"""
    try:
        import pymysql  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - 环境未装 pymysql
        raise SyncError(
            "MySQL 同步目标需要 pymysql；请先安装：pip install pymysql",
            rule="target_driver_missing",
        ) from exc
    database = str(target.get("database") or "").strip()
    if database.lower() in _MYSQL_SYSTEM_DBS:
        raise SyncError(
            f"不能把 MySQL 系统库 {database} 当作同步目标", rule="target_unsafe")
    return pymysql.connect(
        host=str(target.get("host") or "127.0.0.1"),
        port=int(target.get("port") or 3306),
        user=str(target.get("user") or ""),
        password=str(target.get("password") or ""),
        database=database,
        charset="utf8mb4",
        connect_timeout=5.0,
        autocommit=False,
    )


def _quote(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


def _backtick(identifier: str) -> str:
    return "`" + str(identifier).replace("`", "``") + "`"


def _quote_for(adapter: str, identifier: str) -> str:
    return _backtick(identifier) if adapter == "mysql" else _quote(identifier)


def _source_family(engine: Any, source_id: str) -> str:
    """按源适配器选标识符引用方式（MySQL 用反引号）。"""
    try:
        config = engine.config.resolve_source(source_id)
        adapter = str(getattr(config, "adapter", "") or "").lower()
        if adapter:
            return adapter
    except Exception:  # noqa: BLE001 - 拿不到配置时退回类名推断
        pass
    try:
        name = type(_adapter(engine, source_id)).__name__.lower()
    except Exception:  # noqa: BLE001
        return ""
    if "mysql" in name:
        return "mysql"
    if "postgres" in name:
        return "postgres"
    return "sqlite"


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
    quote = lambda ident: _quote_for(  # noqa: E731
        _source_family(engine, config["source_id"]), ident)

    sql = f"SELECT * FROM {quote(table)}"
    if mode == "incremental" and after:
        if not watermark:
            raise SyncError(
                "增量同步缺少递增列（watermark）", rule="watermark_missing")
        if not any(c.name == watermark for c in meta.columns):
            raise SyncError(
                f"增量列「{watermark}」不在源表 {table} 中",
                rule="watermark_missing",
            )
        sql += f" WHERE {quote(watermark)} > {_sql_literal(after)}" \
               f" ORDER BY {quote(watermark)}"

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
def target_table_exists(target: dict[str, Any], table: str) -> bool:
    adapter = _target_adapter(target)
    if adapter == "sqlite":
        with _connect(str(target.get("path") or "")) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
        return row is not None
    conn = _mysql_connect(target)
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES LIKE %s", (table,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def _count_rows(target: dict[str, Any], table: str) -> int:
    adapter = _target_adapter(target)
    quote = lambda ident: _quote_for(adapter, ident)  # noqa: E731
    try:
        if adapter == "sqlite":
            with _connect(str(target.get("path") or "")) as conn:
                return int(conn.execute(
                    f"SELECT COUNT(*) FROM {quote(table)}").fetchone()[0])
        conn = _mysql_connect(target)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {quote(table)}")
                row = cur.fetchone()
                return int(row[0]) if row else 0
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - 目标表不存在/读不到时按 0 行展示
        return 0


def _sqlite_type(data_type: str) -> str:
    text = (data_type or "").upper()
    if "INT" in text:
        return "INTEGER"
    if any(token in text for token in ("REAL", "FLOA", "DOUB", "NUM", "DEC")):
        return "REAL"
    return "TEXT"


def _mysql_type(data_type: str) -> str:
    text = (data_type or "").upper()
    if "INT" in text:
        return "INT"
    if any(token in text for token in ("REAL", "FLOA", "DOUB", "NUM", "DEC")):
        return "DOUBLE"
    return "TEXT"


def _ensure_target_table(
    conn: Any, target: dict[str, Any], table: str, meta: TableMeta
) -> bool:
    """目标表不存在时按源列建表；返回是否新建。"""
    adapter = _target_adapter(target)
    quote = lambda ident: _quote_for(adapter, ident)  # noqa: E731
    if adapter == "sqlite":
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if row is not None:
            return False
    else:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES LIKE %s", (table,))
            if cur.fetchone() is not None:
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
    type_of = _mysql_type if adapter == "mysql" else _sqlite_type
    cols = ", ".join(
        f"{quote(c.name)} {type_of(c.data_type)}" for c in meta.columns)
    if adapter == "sqlite":
        conn.execute(f"CREATE TABLE IF NOT EXISTS {quote(table)} ({cols})")
    else:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {quote(table)} ({cols})"
                " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
    return True


# ---------------------------------------------------------------- 预演 / 提交
def preview(
    *,
    target: dict[str, Any],
    engine: Any,
    config: dict[str, Any],
    state: dict[str, Any] | None,
    max_affected: int,
    sample_limit: int = 20,
) -> dict[str, Any]:
    """预演：只读源表 + 读目标表现状，不修改任何数据。"""
    _target_adapter(target)  # 提前校验目标适配器，别等提交才发现不支持
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

    target_table = config["target_table"]
    target_exists = target_table_exists(target, target_table)
    target_rows = _count_rows(target, target_table) if target_exists else 0

    # 增量任务还没有水位（first run）时，如果目标表里已经有数据，
    # 直接追加会造成重复行；如实拒绝，要求先全量初始化或清空目标表。
    if mode == "incremental" and not after and target_exists and target_rows > 0:
        raise SyncError(
            f"增量任务还没有水位，且目标表「{target_table}」已有 {target_rows} 行；"
            "请先用全量模式初始化一次，或先清空目标表再运行增量",
            rule="incremental_needs_init",
        )

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
        "target_table": target_table,
        "target_exists": target_exists,
        "target_rows": target_rows,
        "watermark_before": after,
        "watermark_after": watermark_after,
        "mode": mode,
        "target_adapter": _target_adapter(target),
        "target_label": str(target.get("label") or target.get("source_id") or ""),
    }


def commit(
    *,
    target: dict[str, Any],
    engine: Any,
    config: dict[str, Any],
    state: dict[str, Any] | None,
    max_affected: int,
    expected_rows: int,
    expected_watermark_after: str,
) -> dict[str, Any]:
    """提交：重新读源表核对后，在目标库单事务里写入。"""
    adapter = _target_adapter(target)
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
    # 增量首跑（水位为空）且目标表已有数据：拒绝，避免追加出重复行。
    if mode == "incremental" and not after and target_table_exists(target, target_table):
        existing = _count_rows(target, target_table)
        if existing > 0:
            raise SyncError(
                f"增量任务还没有水位，且目标表「{target_table}」已有 {existing} 行；"
                "请先用全量模式初始化一次，或先清空目标表再运行增量",
                rule="incremental_needs_init",
            )
    quote = lambda ident: _quote_for(adapter, ident)  # noqa: E731
    created_table = False

    if adapter == "sqlite":
        conn = _connect(str(target.get("path") or ""))
        try:
            conn.execute("BEGIN IMMEDIATE")
            created_table = _ensure_target_table(conn, target, target_table, meta)
            if mode == "full":
                conn.execute(f"DELETE FROM {quote(target_table)}")
            if rows:
                placeholders = ", ".join("?" for _ in columns)
                col_sql = ", ".join(quote(c) for c in columns)
                conn.executemany(
                    f"INSERT INTO {quote(target_table)} ({col_sql})"
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
    else:
        conn = _mysql_connect(target)
        try:
            conn.begin()
            created_table = _ensure_target_table(conn, target, target_table, meta)
            with conn.cursor() as cur:
                if mode == "full":
                    cur.execute(f"DELETE FROM {quote(target_table)}")
                if rows:
                    placeholders = ", ".join("%s" for _ in columns)
                    col_sql = ", ".join(quote(c) for c in columns)
                    cur.executemany(
                        f"INSERT INTO {quote(target_table)} ({col_sql})"
                        f" VALUES ({placeholders})",
                        [tuple(row) for row in rows],
                    )
            conn.commit()
        except SyncError:
            conn.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - pymysql 错误统一成业务结果
            conn.rollback()
            raise SyncError(f"写入 MySQL 目标失败：{exc}", rule="mysql_error") from exc
        finally:
            conn.close()

    target_rows = _count_rows(target, target_table)
    return {
        "rows": len(rows),
        "target_rows": target_rows,
        "watermark_after": watermark_after,
        "target_table": target_table,
        "created_table": created_table,
        "mode": mode,
        "target_adapter": adapter,
    }
