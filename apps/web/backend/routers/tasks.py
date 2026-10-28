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
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from nl2sql.contracts.pipeline import QueryOutcome
from nl2sql.errors import NL2SQLError, TaskError
from nl2sql.guard.write import WriteGuard
from nl2sql.tasks import (
    DEFAULT_TASK_FOLDERS,
    MAX_SYNC_ROWS,
    TASK_KIND_SQL,
    TASK_KIND_SYNC,
    TASK_KIND_WORKFLOW,
    TASK_KIND_WRITE,
    TaskStore,
    parse_sync_config,
)

from .. import sync_runner, workflow_runner, write_runner
from ..projects import DEFAULT_PROJECT_ID, ProjectHub
from ..schemas import (
    SyncCommitRequest,
    TaskCreateRequest,
    TaskFolderCreateRequest,
    TaskFolderRenameRequest,
    TaskMoveRequest,
    TaskRunRequest,
    TaskUpdateRequest,
    WorkflowScheduleUpdate,
    WriteCommitRequest,
    WriteDryRunRequest,
)
from ..serializers import outcome_to_response
from ..task_exec import execute_readonly as _execute_readonly
from ..task_exec import run_sql_task
from ..workflow_schedule import next_fire_text, validate_cron

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
    if task.kind == TASK_KIND_WORKFLOW:
        raise HTTPException(
            status_code=400,
            detail="工作流任务请用「运行工作流」触发：会按步骤依赖顺序执行只读 "
                   "SQL，并记录一次运行实例（cron 到点也会自动触发）",
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


# ============================================================== 工作流（调度）
def _workflow_or_400(store: TaskStore, task_id: str):
    task = _task_or_404(store, task_id)
    if task.kind != TASK_KIND_WORKFLOW:
        raise HTTPException(status_code=400,
                            detail="只有「工作流」任务有运行实例与调度配置")
    return task


def _start_workflow(store: TaskStore, engine: Any, task,
                    trigger: str) -> dict[str, Any]:
    try:
        run = workflow_runner.start(
            store=store, task=task,
            run_sql=lambda sql: run_sql_task(engine, sql),
            trigger=trigger,
        )
    except TaskError as exc:
        message = str(exc)
        status = 409 if "实例在运行" in message else 400
        raise HTTPException(status_code=status, detail=message) from exc
    return {
        "status": "running",
        "run": run,
        "task": _summary(store.get(task.task_id) or task),
    }


@router.post("/projects/{project_id}/tasks/{task_id}/workflow/run")
def run_workflow(project_id: str, task_id: str,
                 request: Request) -> dict[str, Any]:
    """手动触发一次工作流：创建实例后立刻返回，前端轮询实例状态。"""
    hub = _hub(request)
    store, engine = _store_for(hub, project_id)
    task = _workflow_or_400(store, task_id)
    return _start_workflow(store, engine, task, "manual")


@router.post("/projects/{project_id}/tasks/{task_id}/runs/{run_id}/rerun")
def rerun_workflow(project_id: str, task_id: str, run_id: str,
                   request: Request) -> dict[str, Any]:
    hub = _hub(request)
    store, engine = _store_for(hub, project_id)
    task = _workflow_or_400(store, task_id)
    existing = store.workflow_run(run_id)
    if existing is None or existing.get("task_id") != task_id:
        raise HTTPException(status_code=404, detail=f"运行实例不存在：{run_id}")
    return _start_workflow(store, engine, task, "rerun")


@router.get("/projects/{project_id}/tasks/{task_id}/runs")
def list_workflow_runs(project_id: str, task_id: str, request: Request,
                       limit: int = 20) -> dict[str, Any]:
    hub = _hub(request)
    store, _engine = _store_for(hub, project_id)
    _workflow_or_400(store, task_id)
    running = store.running_workflow_run(task_id)
    return {
        "task_id": task_id,
        "running": running is not None,
        "running_run_id": running["run_id"] if running else "",
        "runs": store.list_workflow_runs(
            task_id, limit=_clamp_limit(limit, 20, 100)),
    }


@router.get("/projects/{project_id}/tasks/{task_id}/runs/{run_id}")
def get_workflow_run(project_id: str, task_id: str, run_id: str,
                     request: Request) -> dict[str, Any]:
    hub = _hub(request)
    store, _engine = _store_for(hub, project_id)
    task = _workflow_or_400(store, task_id)
    run = store.workflow_run(run_id)
    if run is None or run.get("task_id") != task_id:
        raise HTTPException(status_code=404, detail=f"运行实例不存在：{run_id}")
    return {"run": run, "task": _summary(store.get(task_id) or task)}


@router.get("/projects/{project_id}/tasks/{task_id}/schedule")
def get_workflow_schedule(project_id: str, task_id: str,
                          request: Request) -> dict[str, Any]:
    hub = _hub(request)
    store, _engine = _store_for(hub, project_id)
    _workflow_or_400(store, task_id)
    schedule = store.workflow_schedule(task_id) or {}
    running = store.running_workflow_run(task_id)
    next_run_at = str(schedule.get("next_run_at") or "")
    if schedule.get("enabled") and schedule.get("cron") and not next_run_at:
        try:
            next_run_at = next_fire_text(str(schedule["cron"]))
        except ValueError:
            next_run_at = ""
    return {
        "task_id": task_id,
        "cron": str(schedule.get("cron") or ""),
        "enabled": bool(schedule.get("enabled")),
        "next_run_at": next_run_at,
        "last_run_at": str(schedule.get("last_run_at") or ""),
        "running": running is not None,
        "running_run_id": running["run_id"] if running else "",
    }


@router.put("/projects/{project_id}/tasks/{task_id}/schedule")
def update_workflow_schedule(project_id: str, task_id: str,
                             req: WorkflowScheduleUpdate,
                             request: Request) -> dict[str, Any]:
    hub = _hub(request)
    store, _engine = _store_for(hub, project_id)
    _workflow_or_400(store, task_id)
    try:
        cron = validate_cron(req.cron)
        next_run_at = next_fire_text(cron) if req.enabled else ""
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    saved = store.save_workflow_schedule(
        task_id, cron=cron, enabled=req.enabled,
        next_run_at=next_run_at)
    saved["running"] = store.running_workflow_run(task_id) is not None
    return saved


def _project_meta(hub: ProjectHub, project_id: str) -> dict[str, Any]:
    try:
        meta = hub.store.get(project_id)
    except NL2SQLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if meta is None:
        raise HTTPException(status_code=404, detail=f"项目不存在：{project_id}")
    return meta


_WRITE_MYSQL_SYSTEM_DBS = {"mysql", "information_schema", "performance_schema", "sys"}


def _write_target(hub: ProjectHub, project_id: str, source_id: str,
                  store: TaskStore,
                  *, allow_mysql: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    """解析受控写 / 同步的目标连接；任何一项不满足都如实拒绝。

    返回 `(target, settings)`：SQLite 目标给 `{"adapter": "sqlite", "path": ...}`，
    MySQL 目标给 `{"adapter": "mysql", "host"/"port"/"user"/"password"/"database"}`。
    受控写（write dry-run/commit）与同步（sync preview/commit）都会传
    `allow_mysql=True`：目标是 SQLite 测试库或 MySQL 实验库；MySQL 系统库与
    任务库一律拒绝。PostgreSQL / JDBC 尚未接入。
    """
    meta = _project_meta(hub, project_id)
    settings = meta.get("data_write") or {}
    if not settings.get("enabled"):
        raise HTTPException(
            status_code=403,
            detail="项目未开启受控写。请到项目设置里显式开启，并绑定一个 "
                   "可写的数据源（SQLite 测试库或 MySQL 实验库）；"
                   "问答与只读任务永远不写业务数据",
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
    adapter = str(source.get("adapter") or "")
    label = str(source.get("label") or target_id)
    if adapter == "sqlite":
        path = str(source.get("path") or "")
        if not path or not os.path.isfile(path):
            raise HTTPException(status_code=400,
                                detail=f"写入数据源文件不存在：{path or '（空）'}")
        if os.path.abspath(path) == os.path.abspath(str(store.path)):
            raise HTTPException(
                status_code=400,
                detail="写入目标不能是任务库（tasks.db）本身",
            )
        return {"adapter": "sqlite", "path": path,
                "source_id": target_id, "label": label}, settings
    if allow_mysql and adapter == "mysql":
        database = str(source.get("database") or "").strip()
        host = str(source.get("host") or "").strip()
        if not host or not database:
            raise HTTPException(
                status_code=400,
                detail=f"MySQL 写入目标缺少 host/database：{target_id}",
            )
        if database.lower() in _WRITE_MYSQL_SYSTEM_DBS:
            raise HTTPException(
                status_code=400,
                detail=f"不能把 MySQL 系统库 {database} 当作写入目标",
            )
        try:
            secrets = hub.store.get_secrets(project_id) or {}
        except Exception:  # noqa: BLE001 - secrets 读不到就按空口令尝试
            secrets = {}
        password = str((secrets.get(target_id) or {}).get("password") or "")
        return {
            "adapter": "mysql",
            "host": host,
            "port": int(source.get("port") or 3306),
            "user": str(source.get("user") or ""),
            "password": password,
            "database": database,
            "source_id": target_id,
            "label": label,
        }, settings
    raise HTTPException(
        status_code=400,
        detail="受控写目标只支持 SQLite 测试库或 MySQL 实验库。"
               "PostgreSQL / JDBC 的写事务与回滚语义尚未接入",
    )


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
    target, settings = _write_target(
        hub, project_id, req.source_id or "", store, allow_mysql=True)
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
            target, check, max_affected=max_affected)
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
    target, settings = _write_target(
        hub, project_id, dry["source_id"], store, allow_mysql=True)
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
            target, check, max_affected=max_affected)
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


# ============================================================== 原生同步
def _sync_or_400(store: TaskStore, task_id: str):
    task = _task_or_404(store, task_id)
    if task.kind != TASK_KIND_SYNC:
        raise HTTPException(status_code=400,
                            detail="只有「数据同步」任务可以做同步预演与提交")
    return task


def _sync_config(task: Any) -> dict[str, Any]:
    try:
        return parse_sync_config(task.sql, draft=False)
    except TaskError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"同步任务配置不完整：{exc}",
        ) from exc


def _sync_audit(store: TaskStore, task: Any, config: dict[str, Any],
                *, status: str, rows: int = 0, error: str = "",
                watermark_before: str = "", watermark_after: str = "") -> int:
    return store.record_sync_event(
        task, status=status, mode=config.get("mode", ""),
        source_id=config.get("source_id", ""),
        source_table=config.get("table", ""),
        target_table=config.get("target_table", ""),
        rows=rows, error=error, watermark_before=watermark_before,
        watermark_after=watermark_after,
    )


@router.post("/projects/{project_id}/tasks/{task_id}/sync/preview")
def sync_preview(project_id: str, task_id: str,
                 request: Request) -> dict[str, Any]:
    """预演同步：只读源表、读取目标表现状，不写任何数据。"""
    hub = _hub(request)
    store, engine = _store_for(hub, project_id)
    task = _sync_or_400(store, task_id)
    config = _sync_config(task)
    target, settings = _write_target(hub, project_id, "", store, allow_mysql=True)
    max_affected = min(int(settings.get("max_affected") or 100), MAX_SYNC_ROWS)
    state = store.sync_state(task_id)
    try:
        result = sync_runner.preview(
            target=target, engine=engine, config=config,
            state=state, max_affected=max_affected,
        )
    except sync_runner.SyncError as exc:
        audit_id = _sync_audit(store, task, config, status="failed",
                               error=str(exc))
        return {
            "status": "failed", "reason": str(exc), "rule": exc.rule,
            "audit_id": audit_id, "task": _summary(task),
            "max_affected": max_affected,
        }
    saved = store.save_sync_preview(
        task, mode=config["mode"], source_id=config["source_id"],
        source_table=config["table"], target_table=config["target_table"],
        rows=int(result["rows"]),
        watermark_before=result["watermark_before"],
        watermark_after=result["watermark_after"],
    )
    audit_id = _sync_audit(
        store, task, config, status="preview", rows=int(result["rows"]),
        watermark_before=result["watermark_before"],
        watermark_after=result["watermark_after"],
    )
    return {
        "status": "preview",
        "preview_id": saved["preview_id"],
        "expires_at": saved["expires_at"],
        "rows": result["rows"],
        "columns": result["columns"],
        "sample": result["sample"],
        "source_id": config["source_id"],
        "source_table": config["table"],
        "target_table": result["target_table"],
        "target_exists": result["target_exists"],
        "target_rows": result["target_rows"],
        "watermark_before": result["watermark_before"],
        "watermark_after": result["watermark_after"],
        "mode": result["mode"],
        "audit_id": audit_id,
        "task": _summary(task),
        "max_affected": max_affected,
    }


@router.post("/projects/{project_id}/tasks/{task_id}/sync/commit")
def sync_commit(project_id: str, task_id: str, req: SyncCommitRequest,
                request: Request) -> dict[str, Any]:
    """凭预演凭据提交：重读源表核对后，在目标 SQLite 单事务里写入。"""
    if not req.confirm:
        raise HTTPException(status_code=400,
                            detail="提交前必须显式确认（confirm=true）")
    hub = _hub(request)
    store, engine = _store_for(hub, project_id)
    task = _sync_or_400(store, task_id)
    preview = store.get_sync_preview(req.preview_id)
    if preview is None:
        raise HTTPException(
            status_code=400,
            detail="同步预演凭据不存在或已过期（5 分钟有效）；请重新预演",
        )
    if preview["project_id"] != project_id or preview["task_id"] != task_id:
        raise HTTPException(status_code=400,
                            detail="预演凭据与当前项目/任务不匹配")
    if int(task.revision) != int(preview["revision"]):
        store.delete_sync_preview(req.preview_id)
        raise HTTPException(
            status_code=409,
            detail="任务在预演后又被修改过；请重新预演",
        )
    config = _sync_config(task)
    try:
        target, settings = _write_target(
            hub, project_id, "", store, allow_mysql=True)
    except HTTPException:
        store.delete_sync_preview(req.preview_id)
        raise
    max_affected = min(int(settings.get("max_affected") or 100), MAX_SYNC_ROWS)
    state = store.sync_state(task_id)
    try:
        done = sync_runner.commit(
            target=target, engine=engine, config=config, state=state,
            max_affected=max_affected,
            expected_rows=int(preview["rows"]),
            expected_watermark_after=str(preview["watermark_after"] or ""),
        )
    except sync_runner.SyncError as exc:
        store.delete_sync_preview(req.preview_id)
        audit_id = _sync_audit(store, task, config, status="failed",
                               error=str(exc))
        return {
            "status": "failed", "reason": str(exc), "rule": exc.rule,
            "audit_id": audit_id, "task": _summary(task),
        }
    store.delete_sync_preview(req.preview_id)
    store.save_sync_state(
        task_id, watermark=str(done["watermark_after"] or ""),
        rows=int(done["rows"]))
    audit_id = _sync_audit(
        store, task, config, status="committed", rows=int(done["rows"]),
        watermark_before=str(preview["watermark_before"] or ""),
        watermark_after=str(done["watermark_after"] or ""),
    )
    return {
        "status": "committed",
        "rows": done["rows"],
        "target_rows": done["target_rows"],
        "target_table": done["target_table"],
        "created_table": done["created_table"],
        "watermark_after": done["watermark_after"],
        "audit_id": audit_id,
        "task": _summary(store.get(task_id) or task),
    }


@router.get("/projects/{project_id}/tasks/{task_id}/sync/info")
def sync_info(project_id: str, task_id: str, request: Request,
              limit: int = 50) -> dict[str, Any]:
    hub = _hub(request)
    store, _engine = _store_for(hub, project_id)
    _sync_or_400(store, task_id)
    state = store.sync_state(task_id) or {
        "task_id": task_id, "watermark": "", "rows": 0, "updated_at": "",
    }
    return {
        "task_id": task_id,
        "state": state,
        "events": store.list_sync_events(
            task_id=task_id, limit=_clamp_limit(limit, 50, 200)),
    }
