"""MySQL 适配器。

设计要点（与 SQLite 那份对照着看）
-----------------------------------
1. **只读有三层，这里负责第二层。**
   `guard/` 解析 SQL 文本拒写（第一层）、适配器把每条查询放进
   `START TRANSACTION READ ONLY`（本例，第二层）、库上是只读账号（第三层）。
   为什么要第二层：guard 的判据是文本，绕过文本层面的检查就失效了；
   只读事务是**服务端**的语义，写语句会拿到 1792 而不是被执行。
   这一层不需要用户配合（不像建只读账号要 DBA 动手），所以放在适配器里最合适。

2. **超时用 `max_execution_time`，不用 socket read_timeout。**
   `read_timeout` 只在"服务端一个字节都不发"时才生效，一条慢慢吐的
   大查询会一直续命。`max_execution_time` 是服务端自己掐断，
   单位毫秒，且只作用于 SELECT —— 正好是我们要管的范围。
   MySQL 5.7.8+ / 8.0 支持；更老的版本 `SET SESSION` 会报错，此时退回
   `read_timeout`，并在 `warnings` 里说明（降级要留痕，不能静默）。

3. **值归一化是适配器的事，不是比对器的事。**
   MySQL 会把 DATE 返回成 `datetime.date`、DECIMAL 返回成 `Decimal`，
   而 SQLite 返回 `str` / `float`。若把差异泄漏给上层，"同一份数据、同一个
   计划"就会因为引擎类型系统不同而比不出"等价"。所以在这里统一：
   date/datetime → ISO 字符串、Decimal → float、bytes → utf-8。
   代价是 DECIMAL 的精确性在展示层被降级成双精度 —— 与 SQLite 一致，
   这正是等价性验收需要的口径。

4. **生成列看 `EXTRA`。**
   SQLite 那边是 `PRAGMA table_xinfo` 的 `hidden in (2,3)`；
   MySQL 对应 `information_schema.COLUMNS.EXTRA` 里出现 `GENERATED`
   （`STORED GENERATED` / `VIRTUAL GENERATED`）。两边都识别到，才谈得上
   "元数据抽取是等价的"。
"""
from __future__ import annotations

import time
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal
from typing import Any

from ..contracts.source import (
    ColumnMeta,
    DryRun,
    QueryResult,
    SourceCapabilities,
    TableMeta,
)
from ..errors import QueryTimeoutError, SourceConnectionError

# MySQL 错误码 → 可行动的提示。
# 只覆盖"规划器最可能遇到"的几个：语法错、表/列不存在、权限。
# 错误是给人看的，不是给日志看的 —— 每条都要能指向下一步动作。
_ERROR_HINTS: dict[int, str] = {
    1064: "SQL 语法错误。通常是标识符没加反引号，或用了 SQLite 的写法"
          "（如 substr() 截日期在这里能跑，但 strftime() 不行）",
    1146: "表不存在。先确认库名与表名，可用 /api/admin/tools 看已发现的表",
    1054: "字段不存在。注意 MySQL 的字段名大小写与反引号包裹",
    1142: "当前账号没有这个操作权限（只读账号只给 SELECT）",
    1305: "函数不存在。MySQL 的函数名与 SQLite 不同组，如 DATE_FORMAT 对应 strftime",
    1792: "语句在只读事务里被拒绝——这是适配器的第二层防线，不是 bug",
}

# 生成列在 EXTRA 里出现这些字样
_GENERATED_MARK = "GENERATED"


def _normalize(value: Any) -> Any:
    """把 MySQL 的 Python 类型压成与 SQLite 一致的口径。见模块 docstring 第 3 条。"""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, dtime):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return value


class MysqlAdapter:
    """实现 `SourceAdapter` 契约（只读）。"""

    capabilities = SourceCapabilities(
        dialect="mysql",
        sql=True,
        pagination="limit",
        supports_cte=True,          # 8.0+；5.7 没有，但 5.7 已 EOL，不降级
        supports_window=True,       # 同上
        supports_json=True,
        supports_explain=True,
        identifier_quote="`",
    )

    def __init__(
        self,
        *,
        host: str,
        user: str,
        database: str,
        password: str,
        port: int = 3306,
        read_only: bool = True,
        count_rows: bool = True,
        connect_timeout_s: float = 5.0,
        charset: str = "utf8mb4",
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._database = database
        self._password = password
        self._read_only = read_only
        self._count_rows = count_rows
        self._connect_timeout_s = connect_timeout_s
        self._charset = charset
        self._conn: Any = None
        # 降到 read_timeout 兜底时置 True，只在 describe() 里体现
        self._timeout_fallback = False

    # ------------------------------------------------------------ 连接
    @property
    def conn(self) -> Any:
        if self._conn is None:
            try:
                import pymysql
            except ImportError as exc:  # pragma: no cover - 环境问题
                raise SourceConnectionError(
                    "MySQL 适配器需要 pymysql：pip install pymysql"
                ) from exc

            try:
                self._conn = pymysql.connect(
                    host=self._host,
                    port=self._port,
                    user=self._user,
                    password=self._password,
                    database=self._database,
                    charset=self._charset,
                    connect_timeout=int(max(1, self._connect_timeout_s)),
                    # 不要 autocommit：下面显式开只读事务，
                    # 由事务边界决定可见性，比 autocommit 更好推理
                    autocommit=False,
                )
            except Exception as exc:  # pymysql.err.OperationalError 等
                raise SourceConnectionError(
                    f"无法连接 MySQL {self._user}@{self._host}:{self._port}"
                    f"/{self._database} ({exc})"
                ) from exc
        return self._conn

    def test_connection(self) -> None:
        conn = self.conn
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        except Exception as exc:
            raise SourceConnectionError(f"连接自检失败: {exc}") from exc

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    # ------------------------------------------------------------ 元数据
    def introspect(self, *, include_comments: bool = True,
                   count_rows: bool | None = None) -> list[TableMeta]:
        do_count = self._count_rows if count_rows is None else count_rows
        conn = self.conn
        with conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM information_schema.TABLES"
                " WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'"
                " ORDER BY TABLE_NAME",
                (self._database,),
            )
            names = [r[0] for r in cur.fetchall()]
        conn.rollback()   # 结束只读事务，避免长事务占着快照

        tables: list[TableMeta] = []
        for name in names:
            tables.append(self._table_meta(name, do_count=do_count,
                                           include_comments=include_comments))
        return tables

    def _table_meta(self, table: str, *, do_count: bool,
                    include_comments: bool) -> TableMeta:
        conn = self.conn
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_COMMENT,"
                "       COLUMN_KEY, ORDINAL_POSITION, EXTRA"
                "  FROM information_schema.COLUMNS"
                " WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s"
                " ORDER BY ORDINAL_POSITION",
                (self._database, table),
            )
            col_rows = cur.fetchall()

            cur.execute(
                "SELECT TABLE_COMMENT FROM information_schema.TABLES"
                " WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                (self._database, table),
            )
            comment_row = cur.fetchone()
            table_comment = (comment_row[0] or "") if comment_row else ""

            cur.execute(
                "SELECT COLUMN_NAME, REFERENCED_TABLE_SCHEMA,"
                "       REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME"
                "  FROM information_schema.KEY_COLUMN_USAGE"
                " WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s"
                "   AND REFERENCED_TABLE_NAME IS NOT NULL"
                " ORDER BY ORDINAL_POSITION",
                (self._database, table),
            )
            fk_rows = cur.fetchall()

        conn.rollback()

        cols: list[ColumnMeta] = []
        for name, data_type, nullable, col_comment, key, ordinal, extra in col_rows:
            cols.append(ColumnMeta(
                name=name,
                data_type=(data_type or "").upper(),
                nullable=(nullable or "").upper() == "YES",
                comment=(col_comment or "") if include_comments else "",
                is_primary_key=(key == "PRI"),
                # SQLite 的 cid 是 0 基；这里也压成 0 基，方便两边逐字段比对
                ordinal=int(ordinal) - 1,
                generated=_GENERATED_MARK in (extra or "").upper(),
            ))

        fks: list[tuple[str, str, str]] = []
        for col, ref_schema, ref_table, ref_col in fk_rows:
            target = f"{ref_schema}.{ref_table}" if ref_schema else ref_table
            fks.append((col, target, ref_col or ""))

        row_estimate: int | None = None
        if do_count:
            row_estimate = self._count_rows_exact(table)

        return TableMeta(
            name=table,
            schema=self._database,
            comment=table_comment,
            columns=cols,
            row_estimate=row_estimate,
            foreign_keys=fks,
        )

    def _count_rows_exact(self, table: str) -> int | None:
        conn = self.conn
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {self._quote(table)}")
                value = cur.fetchone()[0]
            conn.rollback()
            return int(value)
        except Exception:
            conn.rollback()
            return None

    def _quote(self, identifier: str) -> str:
        return "`" + identifier.replace("`", "``") + "`"

    # ------------------------------------------------------------ 执行
    def execute(self, sql: str, *, limit: int, timeout_s: float) -> QueryResult:
        """在只读事务里执行查询。

        `limit` 的语义与 SQLite 适配器完全一致：它是**想展示的行数上限 N**，
        适配器内部多读 1 行判断是否还有更多。调用方给 SQL 加 LIMIT 时也必须
        用 N+1，否则数据库先截断、truncated 静默失效。

        这里**不改 SQL 文本**：pymysql 的 `fetchmany(N+1)` 能拿到 N+1 行，
        与 SQLite 的 fetchmany 行为一样，所以两边的截断判定天然一致。
        """
        conn = self.conn
        started = time.perf_counter()
        self._apply_timeout(conn, timeout_s)

        truncated = False
        columns: list[str] = []
        rows: list[tuple] = []
        try:
            with conn.cursor() as cur:
                if self._read_only:
                    # 第二层防线：服务端语义的只读事务
                    cur.execute("START TRANSACTION READ ONLY")
                try:
                    cur.execute(sql)
                    raw = cur.fetchmany(limit + 1) if limit and limit > 0 else cur.fetchall()
                    columns = [d[0] for d in (cur.description or [])]
                finally:
                    if self._read_only:
                        conn.rollback()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001 - 连接已坏时 rollback 也会抛
                pass
            self._raise_translated(exc, timeout_s)

        if limit and limit > 0 and len(raw) > limit:
            raw = raw[:limit]
            truncated = True

        rows = [tuple(_normalize(v) for v in row) for row in raw]
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        warnings: list[str] = []
        if truncated:
            warnings.append(f"结果被截断，仅返回前 {limit} 行")

        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=elapsed_ms,
            source_id="",
            sql=sql,
            warnings=warnings,
        )

    def _apply_timeout(self, conn: Any, timeout_s: float) -> None:
        """把超时下推到服务端。见模块 docstring 第 2 条。"""
        ms = max(1, int(timeout_s * 1000))
        try:
            with conn.cursor() as cur:
                cur.execute("SET SESSION max_execution_time = %s", (ms,))
            conn.rollback()
        except Exception:
            # 老版本没有这个变量：降级到 socket read_timeout，并且留痕
            self._timeout_fallback = True
            try:
                conn.read_timeout = max(1, int(timeout_s))
            except Exception:  # noqa: BLE001
                pass
            conn.rollback()

    def _raise_translated(self, exc: Exception, timeout_s: float) -> None:
        """把引擎错误翻成两种情况：超时，或带提示的原错。"""
        args = getattr(exc, "args", ())
        code = args[0] if args and isinstance(args[0], int) else None
        text = str(args[1]) if len(args) > 1 else str(exc)

        # 3024 = max_execution_time 被触发；2013 = 连接在查询中丢失（超时常见表现）
        if code in (3024, 2013) or "max_execution_time" in text:
            raise QueryTimeoutError(
                f"查询超过 {timeout_s:.0f}s 被服务端中断"
            ) from exc

        if code in _ERROR_HINTS:
            raise RuntimeError(f"MySQL {code}: {text}（{_ERROR_HINTS[code]}）") from exc

        raise RuntimeError(f"MySQL 执行失败: {text}") from exc

    def explain(self, sql: str) -> DryRun | None:
        """`EXPLAIN` 拿行数估算与访问方式。

        MySQL 的 `EXPLAIN` 是**真的可能执行**的（优化器要统计信息），
        所以同样放进只读事务里跑。
        """
        conn = self.conn
        try:
            with conn.cursor() as cur:
                if self._read_only:
                    cur.execute("START TRANSACTION READ ONLY")
                cur.execute(f"EXPLAIN {sql}")
                desc = [d[0] for d in (cur.description or [])]
                rows = cur.fetchall()
                conn.rollback()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            return DryRun(ok=False, warnings=[f"EXPLAIN 失败: {exc}"])

        raw_lines = [", ".join(f"{c}={v}" for c, v in zip(desc, r)) for r in rows]

        # type=ALL 表示全表扫描；rows 是各步的估算行数，相乘取个量级
        full_scan = False
        estimate: int | None = None
        if "type" in desc and "rows" in desc:
            ti, ri = desc.index("type"), desc.index("rows")
            for row in rows:
                if row[ti] == "ALL":
                    full_scan = True
                n = row[ri]
                if isinstance(n, int) and n > 0:
                    estimate = n if estimate is None else estimate * n

        warnings: list[str] = []
        if full_scan:
            warnings.append("检测到全表扫描（type=ALL），大表上可能较慢")

        return DryRun(
            ok=True,
            estimated_rows=estimate,
            estimated_cost=None,
            full_scan=full_scan,
            warnings=warnings,
            raw="\n".join(raw_lines),
        )

    # ------------------------------------------------------------ 诊断
    def describe(self) -> dict[str, Any]:
        return {
            "dialect": self.capabilities.dialect,
            "host": self._host,
            "port": self._port,
            "user": self._user,
            "database": self._database,
            "read_only": self._read_only,
            "timeout_fallback": self._timeout_fallback,
        }
