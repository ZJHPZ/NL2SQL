"""SQLite 适配器。

要点：
    * **只读**：以 `file:...?mode=ro` URI 打开，从连接层兜底，
      不只依赖上层 guard 校验。
    * **超时**：用 `set_progress_handler` 实现，SQLite 无内建 query timeout。
    * **元数据**：`sqlite_master` + `PRAGMA table_info` / `foreign_key_list`。
      SQLite 无表/列注释概念，注释留空，由知识库层人工补充。
    * **截断**：读 `limit + 1` 行判断是否被截断，不修改 SQL 文本。
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Any
from urllib.parse import quote

from ..contracts.source import (
    ColumnMeta,
    DryRun,
    QueryResult,
    SourceCapabilities,
    TableMeta,
)
from ..errors import QueryTimeoutError, SourceConnectionError
from .columns import suggest_columns  # noqa: F401  (供上层复用的重导出)

# 不属于用户表的内部对象前缀
_INTERNAL_PREFIXES = ("sqlite_",)

_PROGRESS_CHECK_EVERY = 2000


def _to_ro_uri(path: str) -> str:
    """构造只读 URI。

    路径需 URL 编码，否则空格/中文/`?` 会破坏 URI。

    **必须显式带 `mode=ro`**：SQLite 在 URI 未指定 `mode` 时默认是 `rwc`
    （read-write-create），只给 `file:` 前缀**不会**得到只读连接。
    这是一个很容易写错且后果严重（可写生产库）的细节。
    """
    if path.startswith("file:"):
        return path if "mode=" in path else (
            path + ("&" if "?" in path else "?") + "mode=ro"
        )
    abs_path = os.path.abspath(path)
    encoded = quote(abs_path.replace(os.sep, "/"), safe="/:")
    return f"file:{encoded}?mode=ro"


class SqliteAdapter:
    """实现 `SourceAdapter` 契约（只读）。"""

    capabilities = SourceCapabilities(
        dialect="sqlite",
        sql=True,
        pagination="limit",
        supports_cte=True,
        supports_window=True,
        supports_json=True,
        supports_explain=True,
        identifier_quote='"',
    )

    def __init__(self, path: str, *, read_only: bool = True,
                 count_rows: bool = True) -> None:
        self._path = path
        self._read_only = read_only
        self._count_rows = count_rows
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------ 连接
    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            if self._read_only:
                target = _to_ro_uri(self._path)
                if not os.path.exists(self._path):
                    raise SourceConnectionError(
                        f"数据库文件不存在: {self._path}"
                    )
            else:
                target = self._path
            try:
                # uri=True 仅在只读模式需要（写模式直接用路径，避免歧义）
                self._conn = (
                    sqlite3.connect(target, uri=True, check_same_thread=False)
                    if self._read_only
                    else sqlite3.connect(target, check_same_thread=False)
                )
                self._conn.row_factory = sqlite3.Row
            except sqlite3.Error as e:
                raise SourceConnectionError(f"无法打开 SQLite: {self._path} ({e})") from e
        return self._conn

    def test_connection(self) -> None:
        try:
            self.conn.execute("SELECT 1").fetchone()
        except sqlite3.Error as e:
            raise SourceConnectionError(f"连接自检失败: {e}") from e

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------ 元数据
    def introspect(self, *, include_comments: bool = True,
                   count_rows: bool | None = None) -> list[TableMeta]:
        do_count = self._count_rows if count_rows is None else count_rows
        conn = self.conn
        rows = conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            " ORDER BY name"
        ).fetchall()

        tables: list[TableMeta] = []
        for r in rows:
            name = r["name"]
            if any(name.startswith(p) for p in _INTERNAL_PREFIXES):
                continue
            tables.append(self._table_meta(name, do_count=do_count))
        return tables

    def _table_meta(self, table: str, *, do_count: bool) -> TableMeta:
        conn = self.conn
        quoted = self._quote(table)
        cols: list[ColumnMeta] = []
        # 用 table_xinfo 而不是 table_info：
        # table_info **不返回生成列**（STOLEN/VIRTUAL），
        # 而本项目 schema 里的 fact_order.amount 正是生成列，
        # 漏掉它会让"列名不存在"误判并给出错误的候选列表。
        try:
            info = conn.execute(f"PRAGMA table_xinfo({quoted})").fetchall()
        except sqlite3.Error:
            info = conn.execute(f"PRAGMA table_info({quoted})").fetchall()
        for c in info:
            keys = c.keys()
            hidden = int(c["hidden"]) if "hidden" in keys else 0
            # hidden: 0=普通列, 1=隐藏列(虚拟表), 2=生成列VIRTUAL, 3=生成列STORED
            cols.append(ColumnMeta(
                name=c["name"],
                data_type=(c["type"] or "").upper(),
                nullable=not bool(c["notnull"]),
                comment="",                       # SQLite 无列注释
                is_primary_key=bool(c["pk"]),
                ordinal=int(c["cid"]),
                generated=hidden in (2, 3),
            ))

        fks: list[tuple[str, str, str]] = []
        try:
            for fk in conn.execute(f"PRAGMA foreign_key_list({quoted})").fetchall():
                fks.append((fk["from"], fk["table"], fk["to"] or ""))
        except sqlite3.Error:
            pass

        row_estimate: int | None = None
        if do_count:
            try:
                row_estimate = int(
                    conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
                )
            except sqlite3.Error:
                row_estimate = None

        return TableMeta(
            name=table, schema=None, comment="",
            columns=cols, row_estimate=row_estimate, foreign_keys=fks,
        )

    @staticmethod
    def _quote(identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    # ------------------------------------------------------------ 执行
    def execute(self, sql: str, *, limit: int, timeout_s: float) -> QueryResult:
        """执行只读 SQL。

        Args:
            limit: **想展示的行数上限 N**（不是探测上限）。
                适配器内部会去取 N+1 行：拿到 N+1 行说明还有更多，
                于是截断为 N 行并标记 `truncated`。

        重要：调用方给 SQL 加 LIMIT 时也必须用 N+1，否则数据库会先把
        行截到 N，适配器永远看不到"还有更多"，truncated 会静默失效。
        这个耦合在 guard 与 agent 之间，已在两处注释标注。
        """
        conn = self.conn
        started = time.perf_counter()

        deadline = started + max(0.001, timeout_s)

        def _progress() -> int:
            # 返回非 0 会中断当前语句
            return 1 if time.perf_counter() > deadline else 0

        conn.set_progress_handler(_progress, _PROGRESS_CHECK_EVERY)
        truncated = False
        try:
            cur = conn.execute(sql)
            raw = cur.fetchmany(limit + 1) if limit and limit > 0 else cur.fetchall()
        except sqlite3.OperationalError as e:
            if "interrupted" in str(e).lower():
                raise QueryTimeoutError(
                    f"查询超过 {timeout_s:.0f}s 被中断"
                ) from e
            raise
        except sqlite3.ProgrammingError as e:
            if "interrupted" in str(e).lower():
                raise QueryTimeoutError(
                    f"查询超过 {timeout_s:.0f}s 被中断"
                ) from e
            raise
        finally:
            conn.set_progress_handler(None, 0)

        if limit and limit > 0:
            # 约定：`limit` 是**想展示的上限 N**。适配器内部多读 1 行来探测
            # "是否还有更多"，因此：
            #     拿到 N+1 行 → 截断为 N 行并标记 truncated
            #     拿到 <= N 行 → 没有更多，不截断
            # 这样调用方（agent / web）只需传展示上限，不必关心探测细节。
            if len(raw) > limit:
                raw = raw[:limit]
                truncated = True

        columns = [d[0] for d in (cur.description or [])]
        rows = [tuple(r) for r in raw]
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

    def explain(self, sql: str) -> DryRun | None:
        """SQLite 的 EXPLAIN QUERY PLAN 只给访问计划，不给行数估算。

        因此 `estimated_rows` 返回 None，只做"是否有全表扫描"的提示。
        """
        try:
            rows = self.conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
        except sqlite3.Error as e:
            return DryRun(ok=False, warnings=[f"EXPLAIN 失败: {e}"])

        details = [str(r["detail"]) for r in rows] if rows else []
        full_scan = any("SCAN" in d and "SEARCH" not in d for d in details)
        warnings: list[str] = []
        if full_scan:
            warnings.append("检测到全表扫描（SCAN），大表上可能较慢")
        return DryRun(
            ok=True,
            estimated_rows=None,
            estimated_cost=None,
            full_scan=full_scan,
            warnings=warnings,
            raw="\n".join(details),
        )

    # ------------------------------------------------------------ 诊断
    def describe(self) -> dict[str, Any]:
        return {
            "dialect": self.capabilities.dialect,
            "path": self._path,
            "read_only": self._read_only,
            "sqlite_version": sqlite3.sqlite_version,
        }
