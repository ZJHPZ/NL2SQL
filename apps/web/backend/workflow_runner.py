"""工作流执行器（一期原生调度）：把步骤 DAG 跑成「运行实例」。

设计取舍
--------
* 步骤只能是**只读 SQL 任务**：受控写不放进工作流——一次点击连带多表写入
  的风险，本地单用户产品不该默认提供；
* 串行执行：本地 SQLite 单连接下并发没有收益，反而让失败原因更难查；
* 失败即停：某一步失败后剩余步骤全部标 skipped（依赖未成功也是 skipped）；
* 实例与步骤状态全部落库：HTTP 手动触发与后台调度线程共用同一份记录，
  刷新页面也能看到历史；同一工作流同时只允许一个实例（`_running` + 库里
  running 双重判断）。
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from nl2sql.errors import TaskError
from nl2sql.tasks import (
    TASK_KIND_SQL,
    Task,
    TaskStore,
    parse_workflow,
    topological_order,
)

RunSql = Callable[[str], dict[str, Any]]

# 进程内的去重集合：调度线程与 HTTP 触发共用。重启后由库里的 running
# 状态兜底（单用户本地场景，不需要分布式锁）。
_running: set[str] = set()
_lock = threading.Lock()


def resolve_steps(store: TaskStore, task: Task) -> list[dict[str, Any]]:
    """把正文解析成步骤，并校验引用任务真实可用——触发前失败，不发车。"""
    steps = parse_workflow(task.sql)
    if not steps:
        raise TaskError("工作流至少需要 1 个步骤；请在编辑器里添加步骤并保存")
    resolved: list[dict[str, Any]] = []
    for step in steps:
        target = store.get(step["task_id"])
        if target is None:
            raise TaskError(
                f"步骤 {step['key']} 引用的任务不存在：{step['task_id']}")
        if target.task_id == task.task_id:
            raise TaskError("工作流不能引用自身")
        if target.kind != TASK_KIND_SQL:
            raise TaskError(
                f"步骤 {step['key']} 引用的「{target.name}」不是只读 SQL "
                f"任务（{target.kind}）；工作流只执行只读 SQL 步骤"
            )
        resolved.append({**step, "task_name": target.name})
    return resolved


def start(*, store: TaskStore, task: Task, run_sql: RunSql,
          trigger: str = "manual") -> dict[str, Any]:
    """创建并后台执行一个实例；已有实例在跑则抛 TaskError。"""
    with _lock:
        if task.task_id in _running:
            raise TaskError("该工作流已有实例在运行，请等它结束再触发")
        existing = store.running_workflow_run(task.task_id)
        if existing is not None:
            raise TaskError(
                f"该工作流已有实例在运行（{existing['run_id']}），请稍后再试")
        steps = resolve_steps(store, task)
        run = store.create_workflow_run(task, trigger=trigger, steps=steps)
        _running.add(task.task_id)
    thread = threading.Thread(
        target=_execute,
        args=(store, task, steps, run["run_id"], run_sql),
        name=f"workflow-{run['run_id']}",
        daemon=True,
    )
    thread.start()
    return run


def _execute(store: TaskStore, task: Task, steps: list[dict[str, Any]],
             run_id: str, run_sql: RunSql) -> None:
    statuses: dict[str, str] = {step["key"]: "pending" for step in steps}
    failed_key = ""
    failed_error = ""
    try:
        for step in topological_order(steps):
            if any(statuses.get(dep) != "success"
                   for dep in step["depends_on"]):
                store.update_workflow_step(
                    run_id, step["key"], status="skipped",
                    error="依赖步骤未成功，跳过")
                statuses[step["key"]] = "skipped"
                continue
            store.update_workflow_step(
                run_id, step["key"], status="running", started=True)
            target = store.get(step["task_id"])
            if target is None:
                result: dict[str, Any] = {
                    "status": "error", "rows": 0,
                    "error": "引用的任务已删除",
                }
            else:
                result = run_sql(target.sql)
            if result.get("status") == "ok":
                store.update_workflow_step(
                    run_id, step["key"], status="success",
                    rows=int(result.get("rows") or 0), finished=True)
                statuses[step["key"]] = "success"
            else:
                failed_key = step["key"]
                failed_error = str(result.get("error") or "执行失败")
                store.update_workflow_step(
                    run_id, step["key"], status="failed",
                    rows=int(result.get("rows") or 0),
                    error=failed_error, finished=True)
                statuses[step["key"]] = "failed"
                break
    except Exception as exc:  # noqa: BLE001 - 意外错误也要把实例收尾
        failed_key = failed_key or "(workflow)"
        failed_error = f"执行器异常：{exc}"
    finally:
        for step in steps:
            if statuses.get(step["key"]) == "pending":
                store.update_workflow_step(
                    run_id, step["key"], status="skipped",
                    error="前序步骤失败，未执行")
                statuses[step["key"]] = "skipped"
        if failed_key:
            status = "failed"
            error = f"步骤 {failed_key} 失败：{failed_error}"
        else:
            status = "success"
            error = ""
        try:
            store.finish_workflow_run(run_id, status=status, error=error)
            store.mark_run(task.task_id, status)
        finally:
            with _lock:
                _running.discard(task.task_id)
