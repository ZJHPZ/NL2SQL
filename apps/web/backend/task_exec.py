"""任务执行的最小公共路径（只读 SQL）。

为什么单独一个模块
------------------
「手动运行任务」和「工作流步骤执行（含后台调度线程）」必须走**同一条**
只读链路：guard 再检查一次、限制 LIMIT/超时、结果截断标记一致。
如果调度线程为了跑一步 SQL 反向 import `routers.tasks`，模块之间会形成
环；把这段下沉到 task_exec，路由与调度器都只依赖它。

边界
----
这里只执行**只读 SQL**：写入任务走受控写链路（write_runner + 预演凭据），
永远不经过本模块。
"""
from __future__ import annotations

import time
from typing import Any

from nl2sql.contracts.pipeline import QueryOutcome
from nl2sql.errors import NL2SQLError, QueryTimeoutError


def execute_readonly(engine: Any, adapter: Any, cache: Any, guard: Any,
                     sql: str, *, source_id: str,
                     question: str = "") -> QueryOutcome:
    """与 `/api/execute` 同一条只读执行路径（任务库不能绕过防线）。"""
    max_rows = int(engine.config.safety.max_rows)
    timeout_s = float(engine.config.safety.timeout_s)

    # SQL 级 LIMIT 取展示上限 + 1：适配器要能读到"多出的那一行"才判断得出截断。
    decision = guard.check(sql, limit=max_rows + 1)
    if not decision.allowed:
        return QueryOutcome(
            status="blocked", question=question, source_id=source_id,
            sql=sql, error_code="BLOCKED",
            message=decision.message or "SQL 被安全策略拒绝",
            suggestions=[decision.suggestion] if decision.suggestion else [],
            notes=[f"命中规则：{decision.rule}"],
        )

    t0 = time.perf_counter()
    try:
        result = adapter.execute(decision.sql, limit=max_rows,
                                 timeout_s=timeout_s)
    except QueryTimeoutError as exc:
        return QueryOutcome(
            status="timeout", question=question, source_id=source_id,
            sql=decision.sql, error_code="TIMEOUT",
            message=f"查询超时：{exc}",
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )
    except Exception as exc:  # noqa: BLE001 - 执行失败按结果状态返回
        return QueryOutcome(
            status="error", question=question, source_id=source_id,
            sql=decision.sql, error_code=type(exc).__name__,
            message=f"执行失败：{exc}",
            suggestions=["检查字段名与语法，或先在数据库客户端验证该查询"],
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )

    result.source_id = source_id
    result.sql = decision.sql
    outcome = QueryOutcome(
        status="truncated" if result.truncated else (
            "empty" if result.row_count == 0 else "ok"),
        question=question, source_id=source_id, sql=decision.sql,
        result=result,
        notes=["任务执行（只读 SQL，未经语义层校验，口径未验证）"],
        suggestions=(["结果被截断，可缩小范围或加 LIMIT"]
                     if result.truncated else []),
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
    )
    if cache is not None and not result.truncated:
        try:
            cache.store_sql(source_id, decision.sql, result)
        except Exception:  # noqa: BLE001 - 缓存失败不影响结果
            pass
    return outcome


def _pipeline(engine: Any) -> tuple[Any, dict[str, Any], Any, Any]:
    """取引擎上已经装配好的 guard / adapters / cache（缺 guard 视为未就绪）。"""
    agent = getattr(engine, "_agent", None)
    guard = getattr(agent, "_guard", None) or getattr(engine, "_guard", None)
    adapters = getattr(engine, "_adapters", {}) or {}
    cache = getattr(agent, "_cache", None)
    if guard is None:
        raise NL2SQLError("查询管线未就绪：缺少 SQL 守门组件")
    engine.connect()
    return agent, adapters, cache, guard


def run_sql_task(engine: Any, sql: str, *, source_id: str = "",
                 question: str = "") -> dict[str, Any]:
    """执行一条只读 SQL 任务 SQL，返回工作流步骤要的轻量结果。

    返回 `{"status": "ok"|"error", "rows": int, "error": str, "elapsed_ms": int}`。
    blocked / timeout 都归为 error——工作流一步被守门拦下就该停下，
    否则后续步骤会基于错误的前提继续跑。
    """
    try:
        agent, adapters, cache, guard = _pipeline(engine)
        engine_id = source_id or engine.config.default_source
        resolved = engine.config.resolve_source(engine_id)
        sid = resolved.source_id
        adapter = adapters.get(sid) or getattr(agent, "_adapter", None)
        if adapter is None:
            raise NL2SQLError(f"数据源不可用：{sid}")
        outcome = execute_readonly(engine, adapter, cache, guard, sql,
                                   source_id=sid, question=question)
    except NL2SQLError as exc:
        return {"status": "error", "rows": 0,
                "error": str(exc), "elapsed_ms": 0}
    except Exception as exc:  # noqa: BLE001 - 步骤失败不应炸掉调度线程
        return {"status": "error", "rows": 0,
                "error": f"执行失败：{exc}", "elapsed_ms": 0}

    if outcome.status in ("ok", "empty", "truncated"):
        rows = outcome.result.row_count if outcome.result is not None else 0
        return {"status": "ok", "rows": int(rows), "error": "",
                "elapsed_ms": int(outcome.elapsed_ms or 0)}
    return {"status": "error", "rows": 0,
            "error": outcome.message or "执行失败",
            "elapsed_ms": int(outcome.elapsed_ms or 0)}
