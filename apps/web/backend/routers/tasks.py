"""任务库 HTTP 接口（M7 数据开发工作台，契约 §2）。

任务的边界先写清楚（它决定了所有实现）：

* 任务**只保存一条只读 SQL**，不写任何业务数据；是否执行由人决定。
* 执行时仍走 guard（只读 + 单条 + LIMIT + 超时）——任务库不是绕过防线的后门。
* 版本只追加不覆盖：用户改、AI 改、回滚都会留下 revision 记录。
* 「AI 修改」由 `nl2sql.tasks.stamp_ai_comment` 在 SQL 末尾追加
  `-- ai修改，YYYY-MM-DD HH:MM`，审计者能在 SQL 里直接看到。
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from nl2sql.contracts.pipeline import QueryOutcome
from nl2sql.errors import NL2SQLError, QueryTimeoutError
from nl2sql.tasks import TaskStore

from ..projects import DEFAULT_PROJECT_ID, ProjectHub
from ..schemas import TaskCreateRequest, TaskRunRequest, TaskUpdateRequest
from ..serializers import outcome_to_response

router = APIRouter(prefix="/api", tags=["tasks"])


def _hub(request: Request) -> ProjectHub:
    hub = getattr(request.app.state, "project_hub", None)
    if hub is None:
        raise HTTPException(status_code=503, detail="项目中心未就绪")
    return hub


def _project_engine(hub: ProjectHub, project_id: str):
    entry = hub.entry(project_id)
    if entry.engine is None:
        raise HTTPException(status_code=503, detail=entry.error or "项目引擎未就绪")
    return entry.engine


def _store_for(hub: ProjectHub, project_id: str) -> tuple[TaskStore, Any]:
    engine = _project_engine(hub, project_id)
    store = getattr(engine, "task_store", None)
    if store is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "当前项目未启用任务库。默认项目需要设置环境变量 "
                "NL2SQL_TASKS_ENABLED=1（任务写在内部任务库，不写业务数据）；"
                "也可以新建一个项目，项目自带任务库。"
            ),
        )
    return store, engine


def _task_or_404(store: TaskStore, task_id: str):
    task = store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    return task


def _summary(task: Any) -> dict[str, Any]:
    data = task.to_dict()
    sql = str(data.get("sql") or "")
    preview = " ".join(sql.split())
    if len(preview) > 120:
        preview = preview[:120] + "…"
    data["sql_preview"] = preview
    data["status"] = "enabled" if data.get("enabled") else "disabled"
    return data


def _clamp_limit(raw: int | None, default: int = 50, maximum: int = 200) -> int:
    try:
        value = int(raw or default)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, maximum))


# ================================================================== 列表/详情
@router.get("/projects/{project_id}/tasks")
def list_tasks(project_id: str, request: Request,
               limit: int = 50) -> dict[str, Any]:
    hub = _hub(request)
    store, _engine = _store_for(hub, project_id)
    tasks = store.list(limit=_clamp_limit(limit))
    return {"project_id": project_id, "tasks": [_summary(t) for t in tasks],
            "total": store.count()}


@router.post("/projects/{project_id}/tasks")
def create_task(project_id: str, req: TaskCreateRequest,
                request: Request) -> dict[str, Any]:
    hub = _hub(request)
    store, _engine = _store_for(hub, project_id)
    try:
        task = store.create(
            name=req.name, sql=req.sql, description=req.description,
            tags=req.tags, source_id=req.source_id or "", author="user",
        )
    except NL2SQLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"task": _summary(task), "revision": task.revision}


@router.get("/projects/{project_id}/tasks/{task_id}")
def get_task(project_id: str, task_id: str, request: Request) -> dict[str, Any]:
    hub = _hub(request)
    store, _engine = _store_for(hub, project_id)
    task = _task_or_404(store, task_id)
    revisions = store.revisions(task_id)
    return {"task": _summary(task),
            "revisions": [r.to_dict() for r in revisions]}


@router.put("/projects/{project_id}/tasks/{task_id}")
def update_task(project_id: str, task_id: str, req: TaskUpdateRequest,
                request: Request) -> dict[str, Any]:
    hub = _hub(request)
    store, _engine = _store_for(hub, project_id)
    _task_or_404(store, task_id)
    try:
        task = store.update(
            task_id, name=req.name, sql=req.sql,
            description=req.description, tags=req.tags,
            enabled=req.enabled, note=req.note or "用户编辑",
            author="user", source_id=req.source_id,
        )
    except NL2SQLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"task": _summary(task), "revision": task.revision}


@router.delete("/projects/{project_id}/tasks/{task_id}")
def delete_task(project_id: str, task_id: str,
                request: Request) -> dict[str, Any]:
    hub = _hub(request)
    store, _engine = _store_for(hub, project_id)
    _task_or_404(store, task_id)
    store.delete(task_id)
    return {"ok": True}


# ================================================================== 版本
@router.get("/projects/{project_id}/tasks/{task_id}/revisions")
def list_revisions(project_id: str, task_id: str,
                   request: Request) -> dict[str, Any]:
    hub = _hub(request)
    store, _engine = _store_for(hub, project_id)
    task = _task_or_404(store, task_id)
    revisions = store.revisions(task_id)
    return {"task_id": task_id, "current_revision": task.revision,
            "revisions": [r.to_dict() for r in revisions]}


@router.post("/projects/{project_id}/tasks/{task_id}/revisions/{revision}/restore")
def restore_revision(project_id: str, task_id: str, revision: int,
                     request: Request) -> dict[str, Any]:
    """回滚 = 用旧版本内容生成一个新版本（历史仍可追溯）。"""
    hub = _hub(request)
    store, _engine = _store_for(hub, project_id)
    _task_or_404(store, task_id)
    old = store.get_revision(task_id, revision)
    if old is None:
        raise HTTPException(status_code=404,
                            detail=f"版本不存在：{task_id} v{revision}")
    try:
        task = store.update(
            task_id, sql=old.sql, name=old.name, description=old.description,
            note=f"回滚到版本 v{revision}", author="user",
        )
    except NL2SQLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"task": _summary(task), "revision": task.revision,
            "restored_from": revision}


# ================================================================== 执行
@router.post("/projects/{project_id}/tasks/{task_id}/run")
def run_task(project_id: str, task_id: str, req: TaskRunRequest,
             request: Request) -> dict[str, Any]:
    """执行任务里的只读 SQL。

    和 `/api/execute` 的差别只在"SQL 来自任务库"，防线完全一致：
    guard 校验（只读/单条/LIMIT/超时）→ 适配器执行 → 结果塑形。
    失败与拦截都返回 HTTP 200（status 表达结果），界面直接展示。
    """
    hub = _hub(request)
    store, engine = _store_for(hub, project_id)
    task = _task_or_404(store, task_id)

    agent = getattr(engine, "_agent", None)
    guard = getattr(agent, "_guard", None) or getattr(engine, "_guard", None)
    adapters = getattr(engine, "_adapters", {}) or {}
    cache = getattr(agent, "_cache", None)
    if guard is None:
        raise HTTPException(status_code=503, detail="查询管线未就绪")

    try:
        engine.connect()
        engine_id = req.source_id or task.source_id or engine.config.default_source
        source_id = engine.config.resolve_source(engine_id).source_id
        adapter = adapters.get(source_id) or getattr(agent, "_adapter", None)
        if adapter is None:
            raise NL2SQLError(f"数据源不可用：{source_id}")

        outcome = _execute_readonly(
            engine, adapter, cache, guard, task.sql, source_id=source_id,
            question=f"任务：{task.name}",
        )
    except NL2SQLError as exc:
        outcome = QueryOutcome(status="error", question=f"任务：{task.name}",
                               sql=task.sql, source_id=task.source_id,
                               error_code="TASK_ERROR", message=str(exc))
    try:
        store.mark_run(task_id, outcome.status)
    except Exception:  # noqa: BLE001 - 记录失败不该覆盖执行结果
        pass
    payload = outcome_to_response(outcome, planner_mode="task").model_dump()
    payload["task"] = _summary(store.get(task_id) or task)
    return payload


def _execute_readonly(engine: Any, adapter: Any, cache: Any, guard: Any,
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
