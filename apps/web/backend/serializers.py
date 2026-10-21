"""`QueryOutcome` → JSON 安全的响应。

为什么要单独一层：`QueryResult.rows` 里可能含 `Decimal` / `date` / `bytes`
等非 JSON 原生类型。**不能依赖 FastAPI 的自动转换**——它遇到未知类型会
直接报 500，而这种错误只在真实数据上才出现（本地测试库未必覆盖）。

因此这里显式归一化为 JSON 原生标量，规则与 `storage/sqlite_cache.normalize_value`
保持一致：数字转字符串、日期转 ISO、其余转 str。
一致很重要——否则"缓存命中"与"实时执行"两条路径会呈现出不同的类型，
前端排序会把 "9" 和 "10" 按字符串排错。
"""
from __future__ import annotations

import math
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from nl2sql.contracts.pipeline import QueryOutcome

from .schemas import KnowledgeHit, KnowledgeItem, QueryResponse, ResultPayload

__all__ = ["to_json_value", "result_payload", "outcome_to_response",
           "knowledge_item", "knowledge_hit"]


def to_json_value(v: Any) -> Any:
    """把单个值转成 JSON 原生类型，并保留数值可排序性。

    与缓存层归一化的差异：这里**保留 number 类型**而不转成字符串，
    以让前端排序正确。缓存层转字符串是为了跨引擎比对稳定——
    两者目的不同，因此刻意不共用实现，但**都要处理同样的输入类型集合**。
    """
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return str(v)
        return v
    if isinstance(v, Decimal):
        f = float(v)
        return int(f) if f == int(f) and abs(f) < 1e15 else f
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return str(v)


def result_payload(result: Any) -> ResultPayload | None:
    if result is None:
        return None
    return ResultPayload(
        columns=[str(c) for c in result.columns],
        rows=[[to_json_value(v) for v in row] for row in result.rows],
        row_count=result.row_count,
        truncated=bool(result.truncated),
        elapsed_ms=int(result.elapsed_ms or 0),
        data_time=result.data_time,
        cached=bool(result.cached),
        warnings=list(result.warnings or []),
    )


def outcome_to_response(outcome: QueryOutcome, *,
                        planner_mode: str = "") -> QueryResponse:
    # 会话标识从 notes 里取回来（运行时以 `会话: xxx` 形式透出）。
    # 若将来改成结构化字段，这里同步即可——壳层不猜。
    session_id = None
    answer = None
    for n in (outcome.notes or []):
        if n.startswith("会话: "):
            session_id = n[len("会话: "):].strip()
    if planner_mode == "agent":
        # agent 模式下 message 就是给用户的散文答案
        answer = outcome.message

    return QueryResponse(
        status=outcome.status,
        question=outcome.question,
        source_id=outcome.source_id,
        sql=outcome.sql,
        result=result_payload(outcome.result),
        error_code=outcome.error_code,
        message=outcome.message,
        suggestions=list(outcome.suggestions or []),
        candidates=list(outcome.candidates or []),
        applied_filters=list(outcome.applied_filters or []),
        notes=list(outcome.notes or []),
        plan_fingerprint=outcome.plan_fingerprint,
        cached=bool(outcome.cached),
        elapsed_ms=int(outcome.elapsed_ms or 0),
        data_time=outcome.data_time,
        trace=dict(outcome.trace or {}),
        ok=bool(outcome.ok),
        planner_mode=planner_mode,
        session_id=session_id,
        answer=answer,
    )


def knowledge_item(entry: Any) -> KnowledgeItem:
    return KnowledgeItem(
        key=entry.key, type=entry.type, title=entry.title, body=entry.body,
        source_id=entry.source_id, tags=list(entry.tags or []),
        enabled=bool(entry.enabled), origin=entry.origin,
        source_ref=entry.source_ref, version=entry.version,
        created_at=entry.created_at, updated_at=entry.updated_at,
    )


def knowledge_hit(hit: Any) -> KnowledgeHit:
    return KnowledgeHit(
        key=hit.entry.key, type=hit.entry.type, title=hit.entry.title,
        body=hit.entry.body, score=float(hit.score), why=hit.why,
    )
