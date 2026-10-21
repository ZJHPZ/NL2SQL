"""查询路由：问答、手写 SQL 执行、元数据。

**关键设计：手写 SQL 执行必须走 guard。**
若 `/api/execute` 直接连数据库，界面就成了绕过只读与 LIMIT 的旁路——
用户在编辑框里写 `DROP TABLE` 就执行了。
因此这里显式调用 `guard.check()`，只是跳过规划器（不必为此花模型的钱）。
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from nl2sql.contracts.pipeline import QueryOutcome

from ..deps import get_engine
from ..schemas import (
    ExecuteRequest,
    QueryRequest,
    QueryResponse,
    ResultPayload,
    SnapshotRequest,
    TableInfo,
)
from ..serializers import outcome_to_response, result_payload

router = APIRouter(prefix="/api", tags=["query"])


@router.post("/query", response_model=QueryResponse)
def run_query(req: QueryRequest,
              engine: Any = Depends(get_engine)) -> QueryResponse:
    """自然语言 → SQL + 结果表。

    失败**不抛异常**：`QueryOutcome` 里的 status/message/suggestions
    就是给界面用的，直接透出比包成 HTTP 错误更有用。
    """
    outcome = engine.ask(
        req.question, source_id=req.source_id, now=req.now,
        use_cache=req.use_cache, session_id=req.session_id,
    )
    return outcome_to_response(outcome, planner_mode=engine.planner_mode)


@router.post("/execute", response_model=QueryResponse)
def execute_sql(req: ExecuteRequest,
                engine: Any = Depends(get_engine)) -> QueryResponse:
    """执行用户手写的 SQL。**只读守门照旧生效。**

    这条路径刻意与 `/query` 分开：
      * 不走规划器（用户已经给了 SQL，没必要花模型的钱）
      * 但必须过 `guard`（只读 / 禁多语句 / 强制 LIMIT）
      * 结果同样可写缓存（按 SQL 文本指纹），因此重跑很快
    """
    engine.connect()
    if engine._agent is None:  # noqa: SLF001 - 壳层需要判断管线是否就绪
        raise HTTPException(status_code=503, detail="查询管线未就绪")

    guard = engine._agent._guard            # noqa: SLF001
    adapter = engine._agent._adapter        # noqa: SLF001
    cache = engine._agent._cache            # noqa: SLF001

    t0 = time.perf_counter()
    # SQL 级 LIMIT 取展示上限 + 1：适配器要能读到"多出的那一行"才判断得出截断。
    # 详见 nl2sql/sources/sqlite.py::execute 的约定说明。
    decision = guard.check(req.sql, limit=engine.config.safety.max_rows + 1)
    if not decision.allowed:
        return outcome_to_response(QueryOutcome(
            status="blocked",
            question="（手写 SQL）",
            source_id=engine.config.resolve_source(req.source_id).source_id,
            sql=req.sql,
            error_code="BLOCKED",
            message=decision.message or "SQL 被安全策略拒绝",
            suggestions=[decision.suggestion] if decision.suggestion else [],
            notes=[f"命中规则：{decision.rule}"],
        ), planner_mode="manual")

    source_id = engine.config.resolve_source(req.source_id).source_id
    try:
        result = adapter.execute(
            decision.sql,
            # 传**展示上限**：适配器内部自己去取 N+1 行判断截断。
            # 这里多传 1 会让它以为展示上限是 1001，于是返回 1001 行。
            limit=engine.config.safety.max_rows,
            timeout_s=engine.config.safety.timeout_s,
        )
    except Exception as e:  # noqa: BLE001 - 转成可展示的反馈
        elapsed = int((time.perf_counter() - t0) * 1000)
        return outcome_to_response(QueryOutcome(
            status="error",
            question="（手写 SQL）",
            source_id=source_id,
            sql=decision.sql,
            error_code=type(e).__name__,
            message=f"执行失败：{e}",
            suggestions=["检查字段名与语法，或先在数据库客户端验证该查询"],
            elapsed_ms=elapsed,
        ), planner_mode="manual")

    result.source_id = source_id
    result.sql = decision.sql
    outcome = QueryOutcome(
        status="truncated" if result.truncated else (
            "empty" if result.row_count == 0 else "ok"),
        question="（手写 SQL）",
        source_id=source_id,
        sql=decision.sql,
        result=result,
        notes=["手写 SQL 执行（未经语义层校验，口径未验证）"],
        suggestions=(["结果被截断，可缩小范围或加 LIMIT"] if result.truncated else []),
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
    )
    # 手写 SQL 的结果也缓存：重跑同一段 SQL 不必再打数据库。
    # 缓存失败不影响返回——缓存只是优化，不参与正确性判断。
    if cache is not None and not result.truncated:
        try:
            cache.store_sql(source_id, decision.sql, result)
        except Exception:  # noqa: BLE001
            pass
    return outcome_to_response(outcome, planner_mode="manual")


@router.get("/tables", response_model=list[TableInfo])
def list_tables(source_id: str | None = None,
                engine: Any = Depends(get_engine)) -> list[TableInfo]:
    """列出某数据源的表结构（来自本地快照，不实时连库）。"""
    tables = engine.tables(source_id)
    out: list[TableInfo] = []
    for t in tables:
        out.append(TableInfo(
            name=t.name,
            comment=t.comment or "",
            row_estimate=t.row_estimate,
            column_count=len(t.columns),
            columns=[
                {
                    "name": c.name,
                    "data_type": c.data_type,
                    "nullable": c.nullable,
                    "comment": c.comment,
                    "is_primary_key": c.is_primary_key,
                    "generated": c.generated,
                }
                for c in t.columns
            ],
            foreign_keys=[list(fk) for fk in t.foreign_keys],
        ))
    return out


@router.get("/ddl")
def get_ddl(source_id: str | None = None, max_tables: int = 20,
            engine: Any = Depends(get_engine)) -> dict[str, str]:
    """渲染受限的精简 DDL，便于用户核对语义层与实际库表是否一致。"""
    return {"ddl": engine.ddl(source_id, max_tables=max_tables)}


@router.post("/snapshot")
def refresh_snapshot(req: SnapshotRequest,
                     engine: Any = Depends(get_engine)) -> dict[str, Any]:
    meta = engine.refresh_snapshot(req.source_id, force=req.force)
    return {
        "source_id": meta.source_id,
        "fingerprint": meta.fingerprint,
        "captured_at": meta.captured_at,
        "table_count": meta.table_count,
    }


@router.get("/cache")
def cache_stats(engine: Any = Depends(get_engine)) -> dict[str, Any]:
    engine.connect()
    cache = engine._cache  # noqa: SLF001
    return cache.stats() if cache is not None else {}


@router.post("/cache/refresh")
def cache_refresh(engine: Any = Depends(get_engine)) -> dict[str, Any]:
    """手动刷新结果缓存（对应 TTL 之外的"我现在就要最新数据"）。"""
    engine.connect()
    cache = engine._cache  # noqa: SLF001
    removed = cache.invalidate(engine.config.resolve_source().source_id) \
        if cache is not None else 0
    return {"removed": removed}
