"""信息与历史路由。

`/api/info` 是**启动自检的界面化**：让用户一眼看到
"当前用的是哪个规划器、有没有模型、安全边界是什么"。
规划器模式直接决定延迟与成本（实测差 100 倍以上），
用户有权知道自己付了什么代价。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from ..deps import get_engine, get_project_entry
from ..schemas import HistoryItem, InfoResponse

router = APIRouter(prefix="/api", tags=["info"])


@router.get("/info", response_model=InfoResponse)
def info(request: Request) -> InfoResponse:
    """启动自检。

    **引擎没起来的时候也要能回答**——这是这个接口存在的意义之一。
    早期版本用 `Depends(get_engine)`，引擎装配失败时会直接 503，
    用户只看到"核心引擎未就绪"，完全不知道是**哪个文件写错了**。
    现在改为直接读项目引擎条目，把启动期/装配期的 warnings 一并透出。
    """
    from nl2sql import __version__

    entry = get_project_entry(request)
    startup = list(entry.warnings)
    if entry.error:
        startup.append(entry.error)
    engine = entry.engine

    if engine is None:
        # 启动就失败：把原因原文交出去，界面才能显示"改哪里"
        return InfoResponse(
            version=__version__,
            planner_mode="未装配",
            llm_available=False,
            engine_ready=False,
            startup_warnings=startup,
        )

    engine.connect()
    cfg = engine.config

    provider = getattr(engine, "llm_provider", None)
    llm_available = bool(provider is not None and provider.available())

    caps: dict[str, Any] = {}
    try:
        sid = cfg.resolve_source().source_id
        caps = engine.capabilities(sid)
    except Exception:  # noqa: BLE001
        caps = {}

    snapshot: dict[str, Any] = {}
    try:
        sid = cfg.resolve_source().source_id
        meta = engine._catalog.snapshot_meta(sid)  # noqa: SLF001
        if meta is not None:
            snapshot = {
                "fingerprint": meta.fingerprint,
                "captured_at": meta.captured_at,
                "table_count": meta.table_count,
            }
    except Exception:  # noqa: BLE001
        snapshot = {}

    knowledge_stats: dict[str, Any] = {}
    try:
        entries = engine.knowledge_list(include_disabled=True)
        by_type: dict[str, int] = {}
        for e in entries:
            by_type[e.type] = by_type.get(e.type, 0) + 1
        knowledge_stats = {
            "enabled": cfg.knowledge.enabled,
            "total": len(entries),
            "enabled_count": sum(1 for e in entries if e.enabled),
            "by_type": by_type,
        }
    except Exception:  # noqa: BLE001
        knowledge_stats = {"enabled": False}

    safety = {
        "read_only": cfg.safety.read_only,
        "max_rows": cfg.safety.max_rows,
        "timeout_s": cfg.safety.timeout_s,
        "forbid_multiple_statements": cfg.safety.forbid_multiple_statements,
        "forbid_ddl_dml": cfg.safety.forbid_ddl_dml,
    }

    cache_info: dict[str, Any] = {"enabled": cfg.cache.enabled,
                                  "ttl_seconds": cfg.cache.ttl_seconds}
    try:
        cache_info.update(engine._cache.stats() if engine._cache else {})  # noqa: SLF001
    except Exception:  # noqa: BLE001
        pass

    # 语义层来自哪里、有多可移植 —— 用户有权知道
    # （用 dbt metrics.yml 时，哪些指标在 dbt 里没有等价物）
    semantic: dict[str, Any] = {
        "source": "dbt metrics.yml" if engine.semantic_report else "内置参考模型",
        "base_table": getattr(engine.semantic_model, "base_table", ""),
        "metrics": len(getattr(engine.semantic_model, "metrics", {}) or {}),
        "dimensions": len(
            getattr(engine.semantic_model, "dimensions", {}) or {}),
    }
    if engine.semantic_report is not None:
        semantic.update(engine.semantic_report.describe())

    # 执行器：名字 + 有没有降级 + 图结构（节点/边）。
    # 降级说明要进 startup_warnings —— 用户以为在跑图执行器、实际没有，
    # 是最难排查的那种沉默。
    runtime: dict[str, Any] = {}
    try:
        runtime = engine.runtime_describe()
    except Exception as e:  # noqa: BLE001 - 自述失败不该让 /api/info 挂掉
        runtime = {"error": str(e)}
    if runtime.get("note"):
        startup.append(str(runtime["note"]))

    return InfoResponse(
        version=__version__,
        planner_mode=engine.planner_mode,
        llm_available=llm_available,
        llm_model=getattr(provider, "model", "") or "",
        sources=engine.sources(),
        default_source=cfg.default_source,
        capabilities=caps,
        safety=safety,
        cache=cache_info,
        knowledge=knowledge_stats,
        snapshot=snapshot,
        semantic=semantic,
        runtime=runtime,
        engine_ready=True,
        startup_warnings=startup,
    )


@router.get("/history", response_model=list[HistoryItem])
def history(source_id: str | None = None, limit: int = 50,
            engine: Any = Depends(get_engine)) -> list[HistoryItem]:
    """查询历史。个人场景下历史本身是体验的一部分：可回看、可复用。"""
    records = engine.history(source_id=source_id, limit=limit)
    return [
        HistoryItem(
            id=r.id, source_id=r.source_id, question=r.question,
            sql=r.sql or "", status=r.status, row_count=r.row_count,
            elapsed_ms=r.elapsed_ms, created_at=r.created_at,
            error_code=r.error_code, error_message=r.error_message,
            plan_fingerprint=r.plan_fingerprint, meta=dict(r.meta or {}),
        )
        for r in records
    ]


@router.get("/health")
def health(request: Request,
           engine: Any = Depends(get_engine)) -> dict[str, Any]:
    """轻量健康检查，供界面轮询与启动自检。"""
    entry = get_project_entry(request)
    startup = list(entry.warnings)
    if entry.error:
        startup.append(entry.error)
    result: dict[str, Any] = {"ok": True, "planner_mode": engine.planner_mode}
    if startup:
        # 启动期的警告（含引擎装配失败的原因）必须能从这里看到
        result["startup_warnings"] = startup
    try:
        result["connection"] = engine.test_connection()
    except Exception as e:  # noqa: BLE001
        result["ok"] = False
        result["connection"] = {"ok": False, "message": str(e)}
    return result
