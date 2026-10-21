"""观测存储（SQLite 实现）：问答与工具调用的 append-only 记录 + 聚合。

为什么要单独一张表，而不是从 `history` 反解
-----------------------------------------
`history` 的职责是"某一次问答发生了什么"，字段围绕回放：`sql`、
`plan_fingerprint`、`error_message`。观测要的是另一组列：`planner`、
`runtime`、`steps`、`cached`——它们是**运行方式的痕迹**，不是问答内容。
把两者混在一张表里，任何一边加字段都会让另一边变脏；拆开之后，
统计查询可以走自己的索引，也不受回放字段的存储开销影响。

口径（写进契约测试，改口径必须同时改这里与 `docs/15`）
----------------------------------------------------
* `success` = `ok` / `empty` / `truncated`（与 `QueryOutcome.ok` 一致：
  "查到了空表"不是系统失败，是数据事实）
* `need_clarification` 单列：智能体追问是**正常交互**，不算失败
* `failure` = 其余状态（unresolved / invalid_sql / blocked / timeout / error）
* 延迟分位用最近秩法（nearest-rank），`p50` 在有偶数个样本时取偏小一侧——
  个人场景的样本量小，插值会造出"不存在的查询耗时"，反而误导。
"""
from __future__ import annotations

from typing import Any

from ..contracts.storage import QueryLogRecord, ToolCallRecord
from .migrations import open_connection

# 与 `nl2sql.contracts.pipeline.QueryStatus` 同步。测试会断言两者一致，
# 防止以后加了状态却忘了统计口径（那会让失败率悄悄变低）。
KNOWN_STATUSES = (
    "ok", "empty", "truncated", "unresolved", "invalid_sql",
    "blocked", "timeout", "error", "need_clarification",
)
_SUCCESS_STATUSES = frozenset({"ok", "empty", "truncated"})
_OK_OR_CLARIFY = _SUCCESS_STATUSES | {"need_clarification"}


class SqliteStatsStore:
    """实现 `StatsStore` 契约。连接由实例持有，`close()` 释放。"""

    def __init__(self, db_path: str) -> None:
        self._conn = open_connection(db_path)

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------ 写入
    def add_query(self, rec: QueryLogRecord) -> None:
        self._conn.execute(
            "INSERT INTO query_log (created_at, source_id, session_id, question,"
            " planner, runtime, status, error_code, cached, elapsed_ms,"
            " row_count, steps) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec.created_at, rec.source_id, rec.session_id, rec.question,
             rec.planner, rec.runtime, rec.status, rec.error_code,
             1 if rec.cached else 0, int(rec.elapsed_ms), int(rec.row_count),
             int(rec.steps)),
        )
        self._conn.commit()

    def add_tool_call(self, rec: ToolCallRecord) -> None:
        self._conn.execute(
            "INSERT INTO tool_log (created_at, session_id, source_id, tool, ok,"
            " elapsed_ms, error) VALUES (?,?,?,?,?,?,?)",
            (rec.created_at, rec.session_id, rec.source_id, rec.tool,
             1 if rec.ok else 0, int(rec.elapsed_ms), rec.error),
        )
        self._conn.commit()

    # ------------------------------------------------------------ 聚合
    def query_stats(self, *, since: str | None = None) -> dict[str, Any]:
        where, args = _since_clause(since)
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM query_log" + where +
            " GROUP BY status", args
        ).fetchall()
        by_status = {s: 0 for s in KNOWN_STATUSES}
        for r in rows:
            by_status[str(r["status"])] = int(r["n"])

        total = sum(by_status.values())
        success = sum(by_status[s] for s in _SUCCESS_STATUSES)
        clarify = by_status["need_clarification"]
        failure = total - success - clarify

        elapsed = [
            int(r["elapsed_ms"]) for r in self._conn.execute(
                "SELECT elapsed_ms FROM query_log" + where +
                " ORDER BY elapsed_ms", args
            ).fetchall()
        ]
        cached_row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM query_log" + where +
            (" AND cached = 1" if where else " WHERE cached = 1"), args
        ).fetchone()

        tool_rows = self._conn.execute(
            "SELECT tool, COUNT(*) AS calls, SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END)"
            " AS failures, AVG(elapsed_ms) AS avg_ms"
            " FROM tool_log" + where + " GROUP BY tool ORDER BY calls DESC", args
        ).fetchall()
        tools: dict[str, dict[str, Any]] = {}
        for r in tool_rows:
            name = str(r["tool"])
            calls = int(r["calls"])
            fails = int(r["failures"] or 0)
            tool_elapsed = [
                int(x["elapsed_ms"]) for x in self._conn.execute(
                    "SELECT elapsed_ms FROM tool_log WHERE tool = ?" +
                    (" AND created_at >= ?" if where else "") +
                    " ORDER BY elapsed_ms",
                    (name, *args) if where else (name,),
                ).fetchall()
            ]
            tools[name] = {
                "calls": calls,
                "failures": fails,
                "failure_rate": _rate(fails, calls),
                "avg_ms": int(r["avg_ms"] or 0),
                "p95_ms": _percentile(tool_elapsed, 95),
            }

        tool_total = sum(t["calls"] for t in tools.values())
        tool_failures = sum(t["failures"] for t in tools.values())
        return {
            "total": total,
            "by_status": by_status,
            "success": success,
            "clarify": clarify,
            "failure": failure,
            "success_rate": _rate(success, total),
            "failure_rate": _rate(failure, total),
            "cached": int(cached_row["n"] if cached_row else 0),
            "cached_rate": _rate(int(cached_row["n"] if cached_row else 0), total),
            "latency_ms": {
                "p50": _percentile(elapsed, 50),
                "p95": _percentile(elapsed, 95),
                "avg": int(sum(elapsed) / len(elapsed)) if elapsed else 0,
            },
            "tools": {
                "total": tool_total,
                "failures": tool_failures,
                "failure_rate": _rate(tool_failures, tool_total),
                "by_tool": tools,
            },
            "since": since,
        }

    def recent_failures(self, *, limit: int = 10,
                        since: str | None = None) -> list[dict[str, Any]]:
        """最近的非成功条目。`need_clarification` 不算失败，不列进来。"""
        where, args = _since_clause(since, extra=(
            "status NOT IN ('ok','empty','truncated','need_clarification')"
        ))
        rows = self._conn.execute(
            "SELECT created_at, source_id, status, question, error_code,"
            " elapsed_ms, planner, runtime FROM query_log" + where +
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (*args, int(limit)),
        ).fetchall()
        return [
            {
                "created_at": str(r["created_at"]),
                "source_id": str(r["source_id"]),
                "status": str(r["status"]),
                "question": str(r["question"] or ""),
                "error_code": r["error_code"],
                "elapsed_ms": int(r["elapsed_ms"] or 0),
                "planner": str(r["planner"] or ""),
                "runtime": str(r["runtime"] or ""),
            }
            for r in rows
        ]

    # 兼容 ResultCache 风格的 stats 接口：管理页/测试可能直接问存储
    def stats(self) -> dict[str, Any]:
        return self.query_stats()


# --------------------------------------------------------------------- 工具
def _since_clause(since: str | None, extra: str | None = None
                  ) -> tuple[str, tuple[Any, ...]]:
    parts: list[str] = []
    args: list[Any] = []
    if since:
        parts.append("created_at >= ?")
        args.append(since)
    if extra:
        parts.append(extra)
    if not parts:
        return "", ()
    return " WHERE " + " AND ".join(parts), tuple(args)


def _rate(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def _percentile(sorted_values: list[int], p: int) -> int:
    """最近秩法。`p` ∈ (0, 100]，样本空时返回 0。"""
    if not sorted_values:
        return 0
    n = len(sorted_values)
    rank = max(1, -(-p * n // 100))       # ceil(p*n/100)
    return int(sorted_values[min(n, rank) - 1])
