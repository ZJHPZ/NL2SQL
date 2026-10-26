"""任务库 HTTP 接口（M7 数据开发工作台，契约 §2）。

任务的边界先写清楚（它决定了所有实现）：

* 任务**只保存一条只读 SQL**，不写任何业务数据；是否执行由人决定。
* 执行时仍走 guard（只读 + 单条 + LIMIT + 超时）——任务库不是绕过防线的后门。
* 版本只追加不覆盖：用户改、AI 改、回滚都会留下 revision 记录。
* 「AI 修改」由 `nl2sql.tasks.stamp_ai_comment` 在 SQL 末尾追加
  `-- ai修改，YYYY-MM-DD HH:MM`，审计者能在 SQL 里直接看到。
"""
from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from nl2sql.contracts.pipeline import QueryOutcome
from nl2sql.errors import NL2SQLError, QueryTimeoutError
from nl2sql.guard.write import WriteGuard
from nl2sql.tasks import (
    DEFAULT_TASK_FOLDERS,
    TASK_KIND_SQL,
    TASK_KIND_WRITE,
    TaskStore,
)

from .. import write_runner
from ..projects import DEFAULT_PROJECT_ID, ProjectHub
from ..schemas import (
    TaskCreateRequest,
    TaskFolderCreateRequest,
    TaskFolderRenameRequest,
    TaskMoveRequest,
    TaskRunRequest,
    TaskUpdateRequest,
    WriteCommitRequest,
    WriteDryRunRequest,
)
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
            folder=req.folder, kind=req.kind,
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
            folder=req.folder, kind=req.kind,
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


@router.post("/projects/{project_id}/tasks/{task_id}/move")
def move_task(project_id: str, task_id: str, req: TaskMoveRequest,
              request: Request) -> dict[str, Any]:
    """把任务移动到另一个文件夹（不生成版本，只改归属）。"""
    hub = _hub(request)
    store, _engine = _store_for(hub, project_id)
    _task_or_404(store, task_id)
    try:
        task = store.move(task_id, req.folder)
    except NL2SQLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"task": _summary(task)}


# ================================================================== 文件夹
@router.get("/projects/{project_id}/task-folders")
def list_task_folders(project_id: str, request: Request) -> dict[str, Any]:
    hub = _hub(request)
    store, _engine = _store_for(hub, project_id)
    return {
        "project_id": project_id,
        "folders": store.folders(),
        "defaults": list(DEFAULT_TASK_FOLDERS),
    }


@router.post("/projects/{project_id}/task-folders")
def create_task_folder(project_id: str, req: TaskFolderCreateRequest,
                       request: Request) -> dict[str, Any]:
    hub = _hub(request)
    store, _engine = _store_for(hub, project_id)
    try:
        folder = store.create_folder(req.path, author="user")
    except NL2SQLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"folder": folder}


@router.put("/projects/{project_id}/task-folders")
def rename_task_folder(project_id: str, req: TaskFolderRenameRequest,
                       request: Request) -> dict[str, Any]:
    hub = _hub(request)
    store, _engine = _store_for(hub, project_id)
    try:
        result = store.rename_folder(req.path, req.new_path, author="user")
    except NL2SQLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "folder": result, "folders": store.folders()}


@router.delete("/projects/{project_id}/task-folders")
def delete_task_folder(project_id: str, path: str, request: Request) -> dict[str, Any]:
    hub = _hub(request)
    store, _engine = _store_for(hub, project_id)
    try:
        removed = store.delete_folder(path)
    except NL2SQLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "removed": removed, "folders": store.folders()}


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
    if task.kind == TASK_KIND_WRITE:
        raise HTTPException(
            status_code=400,
            detail="写入任务不能直接运行：请先「预演写入」查看影响行数与前后对比，"
                   "再显式确认提交（只读执行链路不会执行写语句）",
        )
    if task.kind != TASK_KIND_SQL:
        # 如实拒绝：PySpark 只有存储与版本，没有执行器。不能假装能跑。
        raise HTTPException(
            status_code=400,
            detail="PySpark 任务执行器尚未接入：当前只能保存代码与版本，"
                   "可运行的只有 SQL 任务",
        )

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


def _project_meta(hub: ProjectHub, project_id: str) -> dict[str, Any]:
    try:
        meta = hub.store.get(project_id)
    except NL2SQLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if meta is None:
        raise HTTPException(status_code=404, detail=f"项目不存在：{project_id}")
    return meta


def _write_target(hub: ProjectHub, project_id: str, source_id: str,
                  store: TaskStore) -> tuple[str, dict[str, Any]]:
    """解析受控写的目标库路径；任何一项不满足都如实拒绝。"""
    meta = _project_meta(hub, project_id)
    settings = meta.get("data_write") or {}
    if not settings.get("enabled"):
        raise HTTPException(
            status_code=403,
            detail="项目未开启受控写。请到项目设置里显式开启，并绑定一个 "
                   "SQLite 测试库；问答与只读任务永远不写业务数据",
        )
    target_id = str(settings.get("source_id") or "")
    if not target_id:
        raise HTTPException(status_code=400,
                            detail="项目尚未绑定写入数据源，请先在项目设置里选择")
    if source_id and source_id != target_id:
        raise HTTPException(
            status_code=400,
            detail=f"写入目标只能是项目绑定的数据源 {target_id}",
        )
    source = next(
        (s for s in meta.get("sources") or []
         if str(s.get("source_id") or "") == target_id),
        None,
    )
    if source is None:
        raise HTTPException(status_code=400,
                            detail=f"写入数据源不存在：{target_id}")
    if str(source.get("adapter") or "") != "sqlite":
        raise HTTPException(
            status_code=400,
            detail="P1 受控写只支持 SQLite 测试库；MySQL / PostgreSQL / JDBC "
                   "的写事务与回滚语义尚未接入",
        )
    path = str(source.get("path") or "")
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=400,
                            detail=f"写入数据源文件不存在：{path or '（空）'}")
    if os.path.abspath(path) == os.path.abspath(str(store.path)):
        raise HTTPException(
            status_code=400,
            detail="写入目标不能是任务库（tasks.db）本身",
        )
    return path, settings


@router.post("/projects/{project_id}/tasks/{task_id}/write/dry-run")
def write_dry_run(project_id: str, task_id: str, req: WriteDryRunRequest,
                  request: Request) -> dict[str, Any]:
    """预演写入：事务里真执行一次、统计影响行数、取前后样本，然后回滚。"""
    hub = _hub(request)
    store, _engine = _store_for(hub, project_id)
    task = _task_or_404(store, task_id)
    if task.kind != TASK_KIND_WRITE:
        raise HTTPException(
            status_code=400,
            detail="只有「写入」任务可以预演写入；SQL 任务请直接运行（只读）",
        )
    db_path, settings = _write_target(
        hub, project_id, req.source_id or "", store)
    max_affected = int(settings.get("max_affected") or 100)
    source_id = str(settings.get("source_id") or "")
    guard = WriteGuard(max_affected=max_affected)
    check = guard.check(task.sql)
    if not check.allowed:
        audit_id = store.record_write_audit(
            task=task, source_id=source_id, check=check,
            status="rejected", error=check.reason,
        )
        return {
            "status": "blocked", "reason": check.reason, "rule": check.rule,
            "sql": task.sql, "audit_id": audit_id, "task": _summary(task),
            "max_affected": max_affected,
        }
    try:
        preview = write_runner.dry_run(
            db_path, check, max_affected=max_affected)
    except write_runner.WriteRunError as exc:
        audit_id = store.record_write_audit(
            task=task, source_id=source_id, check=check,
            status="failed", error=exc.message,
        )
        return {
            "status": "failed", "reason": exc.message, "rule": exc.rule,
            "sql": task.sql, "audit_id": audit_id, "task": _summary(task),
            "max_affected": max_affected,
        }
    saved = store.save_dry_run(
        task_id=task.task_id, revision=task.revision, source_id=source_id,
        check=check, rows=int(preview["rows"]),
        before=preview["before"], after=preview["after"],
    )
    audit_id = store.record_write_audit(
        task=task, source_id=source_id, check=check,
        status="preview", rows=int(preview["rows"]),
    )
    return {
        "status": "preview",
        "dry_run_id": saved["dry_run_id"],
        "expires_at": saved["expires_at"],
        "rows": preview["rows"],
        "before": preview["before"],
        "after": preview["after"],
        "kind": check.kind,
        "table": check.table,
        "sql": task.sql,
        "audit_id": audit_id,
        "task": _summary(task),
        "max_affected": max_affected,
    }


@router.post("/projects/{project_id}/tasks/{task_id}/write/commit")
def write_commit(project_id: str, task_id: str, req: WriteCommitRequest,
                 request: Request) -> dict[str, Any]:
    """凭预演凭据提交。提交前复核 revision、SQL 与行数上限，全部一致才落库。"""
    if not req.confirm:
        raise HTTPException(status_code=400,
                            detail="提交前必须显式确认（confirm=true）")
    hub = _hub(request)
    store, _engine = _store_for(hub, project_id)
    task = _task_or_404(store, task_id)
    if task.kind != TASK_KIND_WRITE:
        raise HTTPException(status_code=400,
                            detail="只有「写入」任务可以提交写入")
    dry = store.get_dry_run(req.dry_run_id)
    if dry is None:
        raise HTTPException(
            status_code=400,
            detail="预演凭据不存在或已过期（5 分钟有效）；请重新预演",
        )
    if dry["project_id"] != project_id or dry["task_id"] != task_id:
        raise HTTPException(status_code=400,
                            detail="预演凭据与当前项目/任务不匹配")
    if task.revision != dry["revision"] or task.sql.strip() != dry["sql"].strip():
        store.delete_dry_run(req.dry_run_id)
        raise HTTPException(
            status_code=409,
            detail="任务在预演后又被修改过；为避免按旧预演提交，请重新预演",
        )
    db_path, settings = _write_target(
        hub, project_id, dry["source_id"], store)
    max_affected = int(settings.get("max_affected") or 100)
    source_id = str(settings.get("source_id") or "")
    guard = WriteGuard(max_affected=max_affected)
    check = guard.check(task.sql)
    if not check.allowed:
        store.delete_dry_run(req.dry_run_id)
        audit_id = store.record_write_audit(
            task=task, source_id=source_id, check=check,
            status="rejected", error=check.reason,
        )
        return {"status": "blocked", "reason": check.reason,
                "rule": check.rule, "audit_id": audit_id}
    try:
        done = write_runner.commit(
            db_path, check, max_affected=max_affected)
    except write_runner.WriteRunError as exc:
        store.delete_dry_run(req.dry_run_id)
        audit_id = store.record_write_audit(
            task=task, source_id=source_id, check=check,
            status="failed", error=exc.message,
        )
        return {"status": "failed", "reason": exc.message, "rule": exc.rule,
                "audit_id": audit_id}
    store.delete_dry_run(req.dry_run_id)
    audit_id = store.record_write_audit(
        task=task, source_id=source_id, check=check,
        status="committed", rows=int(done["rows"]),
    )
    return {
        "status": "committed",
        "rows": done["rows"],
        "kind": check.kind,
        "table": check.table,
        "before": done["before"],
        "audit_id": audit_id,
        "task": _summary(store.get(task_id) or task),
    }


@router.get("/projects/{project_id}/write-audit")
def list_write_audit(project_id: str, request: Request,
                     limit: int = 50) -> dict[str, Any]:
    hub = _hub(request)
    store, _engine = _store_for(hub, project_id)
    return {
        "project_id": project_id,
        "audit": store.list_write_audit(limit=_clamp_limit(limit, 50, 200)),
    }


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
