"""工作流 cron 调度线程（一期原生调度）。

* 每 15 秒扫描所有项目的 `workflow_schedule`：启用且 `next_run_at` 到期的触发；
* 触发前**先**推进 next_run_at 再发车，避免同样一次到期被 tick 重复消费；
* 同一工作流的并发由 workflow_runner（进程内集合 + 库内 running 状态）兜底；
* 调度线程只记日志不抛异常：调度器死掉比个别任务失败严重得多。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any

from nl2sql.tasks import TASK_KIND_WORKFLOW

from . import workflow_runner
from .task_exec import run_sql_task
from .workflow_schedule import fmt_time, next_fire_time

log = logging.getLogger("nl2sql.workflow")


class WorkflowScheduler:
    """单进程 cron 调度器；lifespan 启动、关闭时停止。"""

    def __init__(self, hub: Any, *, interval_s: float = 15.0) -> None:
        self.hub = hub
        self.interval_s = float(interval_s)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.stats: dict[str, Any] = {
            "ticks": 0, "fired": 0, "errors": 0, "started_at": "",
        }

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self.stats["started_at"] = fmt_time(datetime.now())
        self._thread = threading.Thread(
            target=self._loop, name="workflow-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - 调度线程不能死
                self.stats["errors"] += 1
                log.exception("workflow scheduler tick failed")

    def tick(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """扫一遍到期的工作流；`now` 可注入，便于测试与探针。"""
        moment = now or datetime.now()
        stamp = fmt_time(moment)
        self.stats["ticks"] += 1
        fired: list[dict[str, Any]] = []
        for project_id in self.hub.store.list_ids():
            try:
                entry = self.hub.entry(project_id)
            except Exception:  # noqa: BLE001 - 单个项目坏了不影响其他项目
                continue
            engine = entry.engine
            store = getattr(engine, "task_store", None) if engine else None
            if store is None:
                continue
            for schedule in store.list_schedules():
                if not schedule.get("enabled"):
                    continue
                due = str(schedule.get("next_run_at") or "")
                if due and due > stamp:
                    continue
                task = store.get(str(schedule.get("task_id") or ""))
                if task is None or task.kind != TASK_KIND_WORKFLOW:
                    continue
                if store.running_workflow_run(task.task_id) is not None:
                    continue
                try:
                    cron = str(schedule.get("cron") or "")
                    next_text = fmt_time(next_fire_time(cron, after=moment))
                    store.mark_schedule_fired(
                        task.task_id, last_run_at=stamp,
                        next_run_at=next_text)
                    run = workflow_runner.start(
                        store=store, task=task,
                        run_sql=(lambda sql, _engine=engine:
                                 run_sql_task(_engine, sql)),
                        trigger="cron",
                    )
                    self.stats["fired"] += 1
                    fired.append({
                        "project_id": project_id,
                        "task_id": task.task_id,
                        "run_id": run["run_id"],
                    })
                except Exception as exc:  # noqa: BLE001
                    self.stats["errors"] += 1
                    log.warning("workflow %s 触发失败：%s", task.task_id, exc)
        return fired
