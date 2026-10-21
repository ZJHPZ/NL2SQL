"""PostgreSQL 适配器。

设计要点（与 MySQL / SQLite 那两份对照着看）
-------------------------------------------
1. **只读有三层，这里负责第二层。**
   `guard/` 解析 SQL 文本拒写（第一层）、适配器把每条查询放进只读事务
   （本例，第二层）、库上是只读账号（第三层）。
   为什么要第二层：guard 的判据是文本，绕过文本层面的检查就失效了；
   只读事务是**服务端**的语义，写语句会拿到 SQLSTATE 25006 而不是被执行。
   这一层不需要用户配合（不像建只读账号要 DBA 动手），所以放在适配器里最合适。

2. **超时用 `statement_timeout`，不用 socket read_timeout。**
   `statement_timeout` 是服务端自己掐断，单位毫秒，作用于语句 —— 正好是
   我们要管的范围。注意 `SET LOCAL statement_timeout = %s` **不能参数化**
   （PostgreSQL 的 SET 不支持绑定参数，会报 `syntax error at or near "$1"`），
   只能把整数拼进 SQL 文本；ms 是整数，没有注入面。
   `SET LOCAL` 只对当前事务生效，事务结束自动恢复，不会污染连接池里的下一条查询。

3. **值归一化与 MySQL 口径一致，不交给比对器。**
   PostgreSQL 会把 DATE 返回成 `datetime.date`、NUMERIC 返回成 `Decimal`，
   而 SQLite 返回 `str` / `float`。若把差异泄漏给上层，"同一份数据、同一个
   计划"就会因为引擎类型系统不同而比不出"等价"。所以在这里统一：
   date/datetime → ISO 字符串、Decimal → float、bytes → utf-8。
   代价是 NUMERIC 的精确性在展示层被降级成双精度 —— 与 SQLite 一致，
   这正是等价性验收需要的口径。

4. **生成列看 `attgenerated`。**
   PostgreSQL 用 `pg_attribute.attgenerated = 's'` 识别 STORED GENERATED；
   SQLite 那边是 `PRAGMA table_xinfo` 的 `hidden in (2,3)`；MySQL 是
   `information_schema.COLUMNS.EXTRA` 里的 `GENERATED`。三边都识别到，
   才谈得上"元数据抽取是等价的"。

5. **标识符引用是双引号，默认 schema 是 `public`。**
   `identifier_quote='"'` 与 SQLite 一致；PG 会把未加引号的标识符折叠成
   小写，所以错误提示里专门点了这一点。元数据查询限制在 `schema` 参数内，
   不把 `pg_catalog` 和其他 schema 的表混进来。
"""
from __future__ import annotations

import re
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

# PostgreSQL 错误码（SQLSTATE）→ 可行动的提示。
# 只覆盖"规划器最可能遇到"的几个：语法错、表/列不存在、权限、函数不存在。
# 错误是给人看的，不是给日志看的 —— 每条都要能指向下一步动作。
_ERROR_HINTS: dict[str, str] = {
    "42601": "SQL 语法错误。PostgreSQL 的标识符不加双引号会被折叠成小写，"
             "函数名也与 SQLite/MySQL 不同（如 substr→SUBSTRING）",
    "42P01": "表不存在。先确认 schema（默认 public）与表名，"
             "可用 /api/admin/tools 看已发现的表",
    "42703": "字段不存在。PostgreSQL 会把未加双引号的标识符折叠成小写，"
             "注意字段名与双引号包裹",
    "42501": "当前账号没有这个操作权限（只读账号只给 SELECT）",
    "42883": "函数不存在。PostgreSQL 的函数名与 SQLite/MySQL 不同组，"
             "如 TO_CHAR / EXTRACT",
    "25006": "语句在只读事务里被拒绝——这是适配器的第二层防线，不是 bug",
}

# 查询被服务端取消（statement_timeout 触发时报这个码）
_TIMEOUT_SQLSTATES = frozenset({"57014"})


def _normalize(value: Any) -> Any:
    """把 PostgreSQL 的 Python 类型压成与 SQLite 一致的口径。见模块 docstring 第 3 条。"""
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


class PostgresAdapter:
    """实现 `SourceAdapter` 契约（只读）。"""

    capabilities = SourceCapabilities(
        dialect="postgres",
        sql=True,
        pagination="limit",
        supports_cte=True,
        supports_window=True,
        supports_json=True,         # jsonb / JSON 函数
        supports_explain=True,
        identifier_quote='"',
        default_schema="public",
    )

    def __init__(
        self,
        *,
        host: str,
        user: str,
        database: str,
        password: str = "",
        port: int = 5432,
        schema: str = "public",
        read_only: bool = True,
        count_rows: bool = True,
        connect_timeout_s: float = 5.0,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._database = database
        self._password = password
        self._schema = schema or "public"
        self._read_only = read_only
        self._count_rows = count_rows
        self._connect_timeout_s = connect_timeout_s
        self._conn: Any = None

    # ------------------------------------------------------------ 连接
    @property
    def conn(self) -> Any:
        if self._conn is None:
            try:
                import psycopg
            except ImportError as exc:  # pragma: no cover - 环境问题
                raise SourceConnectionError(
                    "PostgreSQL 适配器需要 psycopg：pip install psycopg[binary]"
                ) from exc

            kwargs: dict[str, Any] = {
                "host": self._host,
                "port": self._port,
                "user": self._user,
                "dbname": self._database,
                "connect_timeout": int(max(1, self._connect_timeout_s)),
                # 不要 autocommit：下面显式开只读事务，
                # 由事务边界决定可见性，比 autocommit 更好推理
                "autocommit": False,
            }
            if self._password:
                kwargs["password"] = self._password
            try:
                self._conn = psycopg.connect(**kwargs)
            except Exception as exc:  # psycopg.OperationalError 等
                raise SourceConnectionError(
                    f"无法连接 PostgreSQL {self._user}@{self._host}:{self._port}"
                    f"/{self._database} ({exc})"
                ) from exc
            if self._read_only:
                # psycopg3：只读连接开启的事务自动带 READ ONLY
                self._conn.read_only = True
        return self._conn

    def test_connection(self) -> None:
        conn = self.conn
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            conn.rollback()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001 - 连接已坏时 rollback 也会抛
                pass
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
            # relkind 'r' = 普通表，'p' = 分区父表；视图/序列不要
            cur.execute(
                "SELECT c.relname,"
                "       COALESCE(obj_description(c.oid, 'pg_class'), '')"
                "  FROM pg_catalog.pg_class c"
                "  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = %s AND c.relkind IN ('r', 'p')"
                " ORDER BY c.relname",
                (self._schema,),
            )
            names = [(r[0], r[1] or "") for r in cur.fetchall()]
        conn.rollback()   # 结束只读事务，避免长事务占着快照

        tables: list[TableMeta] = []
        for name, comment in names:
            tables.append(self._table_meta(
                name, comment, do_count=do_count,
                include_comments=include_comments,
            ))
        return tables

    def _table_meta(self, table: str, table_comment: str, *, do_count: bool,
                    include_comments: bool) -> TableMeta:
        conn = self.conn
        with conn.cursor() as cur:
            # 列：attgenerated='s' 是 STORED 生成列（PG12+），'' 表示普通列
            cur.execute(
                "SELECT a.attname,"
                "       format_type(a.atttypid, a.atttypmod),"
                "       NOT a.attnotnull,"
                "       COALESCE(col_description(a.attrelid, a.attnum), ''),"
                "       a.attnum,"
                "       (a.attgenerated = 's')"
                "  FROM pg_catalog.pg_attribute a"
                "  JOIN pg_catalog.pg_class c ON c.oid = a.attrelid"
                "  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = %s AND c.relname = %s"
                "   AND a.attnum > 0 AND NOT a.attisdropped"
                " ORDER BY a.attnum",
                (self._schema, table),
            )
            col_rows = cur.fetchall()

            # 主键列名（contype='p'）；golden 表都是单列主键，多列也能覆盖
            cur.execute(
                "SELECT a.attname"
                "  FROM pg_catalog.pg_constraint con"
                "  JOIN pg_catalog.pg_class c ON c.oid = con.conrelid"
                "  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
                "  JOIN pg_catalog.pg_attribute a"
                "    ON a.attrelid = c.oid AND a.attnum = ANY(con.conkey)"
                " WHERE n.nspname = %s AND c.relname = %s AND con.contype = 'p'",
                (self._schema, table),
            )
            pk_names = {r[0] for r in cur.fetchall()}

            # 物理外键：contype='f'，两端都用 ANY(conkey/confkey) 取列名
            cur.execute(
                "SELECT a.attname, fn.nspname, fc.relname, fa.attname"
                "  FROM pg_catalog.pg_constraint con"
                "  JOIN pg_catalog.pg_class c ON c.oid = con.conrelid"
                "  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
                "  JOIN pg_catalog.pg_class fc ON fc.oid = con.confrelid"
                "  JOIN pg_catalog.pg_namespace fn ON fn.oid = fc.relnamespace"
                "  JOIN pg_catalog.pg_attribute a"
                "    ON a.attrelid = c.oid AND a.attnum = ANY(con.conkey)"
                "  JOIN pg_catalog.pg_attribute fa"
                "    ON fa.attrelid = fc.oid AND fa.attnum = ANY(con.confkey)"
                " WHERE con.contype = 'f' AND n.nspname = %s AND c.relname = %s"
                " ORDER BY a.attnum",
                (self._schema, table),
            )
            fk_rows = cur.fetchall()

        conn.rollback()

        cols: list[ColumnMeta] = []
        for name, data_type, nullable, col_comment, attnum, generated in col_rows:
            cols.append(ColumnMeta(
                name=name,
                data_type=(data_type or "").upper(),
                nullable=bool(nullable),
                comment=(col_comment or "") if include_comments else "",
                is_primary_key=name in pk_names,
                # SQLite 的 cid 是 0 基；PG 的 attnum 从 1 开始，压成 0 基
                ordinal=int(attnum) - 1,
                generated=bool(generated),
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
            schema=self._schema,
            comment=table_comment if include_comments else "",
            columns=cols,
            row_estimate=row_estimate,
            foreign_keys=fks,
        )

    def _count_rows_exact(self, table: str) -> int | None:
        conn = self.conn
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {self._qualified(table)}")
                value = cur.fetchone()[0]
            conn.rollback()
            return int(value)
        except Exception:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001 - 连接已坏时 rollback 也会抛
                pass
            return None

    def _quote(self, identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    def _qualified(self, table: str) -> str:
        return f"{self._quote(self._schema)}.{self._quote(table)}"

    # ------------------------------------------------------------ 执行
    def execute(self, sql: str, *, limit: int, timeout_s: float) -> QueryResult:
        """在只读事务里执行查询。

        `limit` 的语义与 SQLite/MySQL 适配器完全一致：它是**想展示的行数上限
        N**，适配器内部多读 1 行判断是否还有更多。调用方给 SQL 加 LIMIT 时
        也必须用 N+1，否则数据库先截断、truncated 静默失效。

        这里**不改 SQL 文本**：psycopg 的 `fetchmany(N+1)` 能拿到 N+1 行，
        与 SQLite 的 fetchmany 行为一样，所以三边的截断判定天然一致。
        """
        conn = self.conn
        started = time.perf_counter()
        timeout_ms = max(1, int(round(timeout_s * 1000)))

        truncated = False
        columns: list[str] = []
        raw: list[tuple] = []
        try:
            # force_rollback=True：只读场景不需要提交，退出时统一回滚
            with conn.transaction(force_rollback=True):
                with conn.cursor() as cur:
                    if self._read_only:
                        # 第二层防线：服务端语义的只读事务
                        # （连接上已设 read_only，这里再显式声明一次，
                        #  防止调用方改动过连接属性）
                        cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
                    cur.execute(sql)
                    raw = (cur.fetchmany(limit + 1)
                           if limit and limit > 0 else cur.fetchall())
                    columns = [d[0] for d in (cur.description or [])]
        except Exception as exc:
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

    def _raise_translated(self, exc: Exception, timeout_s: float) -> None:
        """把引擎错误翻成两种情况：超时，或带提示的原错。"""
        sqlstate = getattr(exc, "sqlstate", None) or ""
        text = str(exc).strip()

        if sqlstate in _TIMEOUT_SQLSTATES:
            raise QueryTimeoutError(
                f"查询超过 {timeout_s:.1f}s 被服务端中断"
            ) from exc

        hint = _ERROR_HINTS.get(sqlstate)
        if hint:
            raise RuntimeError(
                f"PostgreSQL {sqlstate}: {text}（{hint}）"
            ) from exc

        raise RuntimeError(f"PostgreSQL 执行失败: {text}") from exc

    def explain(self, sql: str) -> DryRun | None:
        """`EXPLAIN` 拿行数估算与访问方式。

        PostgreSQL 的 `EXPLAIN` 不做实际执行，但可能取统计信息；
        同样放进只读事务里跑，超时也下推。
        """
        conn = self.conn
        try:
            with conn.transaction(force_rollback=True):
                with conn.cursor() as cur:
                    if self._read_only:
                        cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute("SET LOCAL statement_timeout = 15000")
                    cur.execute(f"EXPLAIN {sql}")
                    lines = [str(r[0]) for r in cur.fetchall()]
        except Exception as exc:
            return DryRun(ok=False, warnings=[f"EXPLAIN 失败: {exc}"])

        raw_text = "\n".join(lines)

        # EXPLAIN 文本形如：
        #   Seq Scan on fact_order  (cost=0.00..21.10 rows=810 width=8)
        full_scan = "Seq Scan" in raw_text
        estimate: int | None = None
        m = re.search(r"rows=(\d+)", raw_text)
        if m:
            try:
                estimate = int(m.group(1))
            except ValueError:  # pragma: no cover - 正则已保证是数字
                estimate = None

        cost: float | None = None
        cm = re.search(r"cost=[\d.]+\.\.([\d.]+)", raw_text)
        if cm:
            try:
                cost = float(cm.group(1))
            except ValueError:  # pragma: no cover
                cost = None

        warnings: list[str] = []
        if full_scan:
            warnings.append("检测到全表扫描（Seq Scan），大表上可能较慢")

        return DryRun(
            ok=True,
            estimated_rows=estimate,
            estimated_cost=cost,
            full_scan=full_scan,
            warnings=warnings,
            raw=raw_text,
        )

    # ------------------------------------------------------------ 诊断
    def describe(self) -> dict[str, Any]:
        return {
            "dialect": self.capabilities.dialect,
            "host": self._host,
            "port": self._port,
            "user": self._user,
            "database": self._database,
            "schema": self._schema,
            "read_only": self._read_only,
            "timeout_fallback": False,   # 与 MySQL 的 describe() 形状对齐
        }
