"""原生调度编排一期验收：工作流 DAG 校验 → 实例执行 → cron 配置 → 调度触发。

跑法：python tests/test_workflow.py
不依赖真实模型与运行中的服务（TestClient 直连 app，临时项目 + 临时 SQLite 库）。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

BASE = os.environ.get("NL2SQL_TEST_TMP") or os.path.join(
    ROOT, "data", "_test_workflow")
WORK = os.path.join(BASE, "run")
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(WORK, exist_ok=True)

os.environ["NL2SQL_DB"] = os.path.join(ROOT, "golden_set", "ecommerce.db")
os.environ["NL2SQL_STATE_DIR"] = os.path.join(WORK, "state")
os.environ["NL2SQL_PROJECTS_DIR"] = os.path.join(WORK, "projects")
os.environ["NL2SQL_CONFIG"] = os.path.join(WORK, "config", "nl2sql.toml")
os.environ["NL2SQL_SKILLS"] = ""
os.environ.pop("NL2SQL_LLM_API_KEY", None)
os.environ.pop("NL2SQL_PLUGINS", None)
os.environ.pop("NL2SQL_TASKS_ENABLED", None)

from fastapi.testclient import TestClient  # noqa: E402

from apps.web.backend.main import create_app  # noqa: E402

PROBE_DB = os.path.join(WORK, "probe.db")
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))
        FAIL.append(name)


def section(title: str) -> None:
    print(f"\n[{title}]")


def _make_probe_db() -> None:
    conn = sqlite3.connect(PROBE_DB)
    conn.executescript(
        """
        CREATE TABLE probe_item (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            amount REAL NOT NULL
        );
        INSERT INTO probe_item (id, name, amount) VALUES
            (1, 'a', 10.0), (2, 'b', 20.0), (3, 'c', 30.0);
        """
    )
    conn.commit()
    conn.close()


def _create_project(client: TestClient, name: str) -> str:
    r = client.post("/api/projects", json={"name": name, "semantic": "none"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _add_source(client: TestClient, pid: str) -> None:
    r = client.post(f"/api/projects/{pid}/sources", json={
        "source_id": "wdb", "adapter": "sqlite",
        "label": "工作流测试库", "path": PROBE_DB})
    assert r.status_code == 200, r.text


def _create_task(client: TestClient, pid: str, name: str, sql: str,
                 kind: str = "sql") -> dict:
    r = client.post(f"/api/projects/{pid}/tasks", json={
        "name": name, "sql": sql, "kind": kind, "folder": "ods"})
    assert r.status_code == 200, r.text
    return r.json()["task"]


def _workflow_body(steps: list[dict]) -> str:
    return json.dumps({"steps": steps}, ensure_ascii=False)


def _wait_run(client: TestClient, pid: str, task_id: str, run_id: str,
              *, timeout_s: float = 15.0) -> dict:
    deadline = time.time() + timeout_s
    last: dict = {}
    while time.time() < deadline:
        r = client.get(f"/api/projects/{pid}/tasks/{task_id}/runs/{run_id}")
        assert r.status_code == 200, r.text
        last = r.json()["run"]
        if last.get("status") != "running":
            return last
        time.sleep(0.15)
    return last


def main() -> int:
    _make_probe_db()
    app = create_app()
    with TestClient(app) as client:
        # ============================================================ A 定义
        section("A 工作流定义与依赖校验")
        pid = _create_project(client, "工作流验收")
        _add_source(client, pid)
        task_a = _create_task(client, pid, "统计行数",
                              "SELECT COUNT(*) AS n FROM probe_item")
        task_b = _create_task(client, pid, "明细样本",
                              "SELECT id, name FROM probe_item ORDER BY id")
        task_w = _create_task(client, pid, "每日汇总工作流", _workflow_body([
            {"key": "s1", "task_id": task_a["task_id"]},
            {"key": "s2", "task_id": task_b["task_id"],
             "depends_on": ["s1"]},
            {"key": "s3", "task_id": task_a["task_id"],
             "depends_on": ["s2"]},
        ]), kind="workflow")
        wf_id = task_w["task_id"]
        check("工作流任务 kind=workflow", task_w.get("kind") == "workflow",
              str(task_w.get("kind")))

        r = client.post(f"/api/projects/{pid}/tasks", json={
            "name": "环依赖", "kind": "workflow",
            "sql": _workflow_body([
                {"key": "a", "task_id": task_a["task_id"],
                 "depends_on": ["b"]},
                {"key": "b", "task_id": task_b["task_id"],
                 "depends_on": ["a"]},
            ])})
        check("循环依赖被拒（400）", r.status_code == 400, r.text[:200])

        r = client.post(f"/api/projects/{pid}/tasks", json={
            "name": "坏 JSON", "kind": "workflow", "sql": "{not json"})
        check("坏 JSON 被拒（400）", r.status_code == 400, r.text[:200])

        write_task = _create_task(client, pid, "写入任务",
                                  "UPDATE probe_item SET amount = 1 WHERE id = 1",
                                  kind="write")
        r = client.post(f"/api/projects/{pid}/tasks", json={
            "name": "引用写任务", "kind": "workflow",
            "sql": _workflow_body([
                {"key": "s1", "task_id": write_task["task_id"]}])})
        check("引用写任务的工作流可保存（结构合法）", r.status_code == 200,
              r.text[:200])
        bad_wf_id = (r.json().get("task") or {}).get("task_id", "")
        r = client.post(
            f"/api/projects/{pid}/tasks/{bad_wf_id}/workflow/run", json={})
        check("触发时拒绝引用写任务（400）", r.status_code == 400,
              r.text[:200])

        r = client.post(f"/api/projects/{pid}/tasks/{wf_id}/run", json={})
        check("工作流不能走 /run 只读执行（400）", r.status_code == 400,
              r.text[:200])

        # ============================================================ B 执行
        section("B 手动触发与实例")
        r = client.post(
            f"/api/projects/{pid}/tasks/{wf_id}/workflow/run", json={})
        body = r.json()
        check("触发返回 running",
              r.status_code == 200 and body.get("status") == "running",
              r.text[:300])
        run_id = (body.get("run") or {}).get("run_id", "")
        check("拿到 run_id", bool(run_id))
        run = _wait_run(client, pid, wf_id, run_id)
        check("实例成功", run.get("status") == "success", str(run)[:300])
        check("实例 3 步全部成功",
              run.get("total_steps") == 3
              and run.get("succeeded_steps") == 3,
              f"{run.get('total_steps')}/{run.get('succeeded_steps')}")
        steps = run.get("steps") or []
        check("步骤顺序按依赖（s1→s2→s3）",
              [s.get("step_key") for s in steps] == ["s1", "s2", "s3"],
              str([s.get("step_key") for s in steps]))
        check("步骤状态 success 且行数正确",
              all(s.get("status") == "success" for s in steps)
              and steps[0].get("rows") == 1 and steps[1].get("rows") == 3,
              str([(s.get("step_key"), s.get("rows")) for s in steps]))
        check("触发方式 manual", run.get("trigger") == "manual",
              str(run.get("trigger")))

        r = client.get(f"/api/projects/{pid}/tasks/{wf_id}/runs")
        check("实例列表可读", r.status_code == 200
              and len(r.json().get("runs") or []) >= 1, r.text[:200])

        r = client.post(
            f"/api/projects/{pid}/tasks/{wf_id}/runs/{run_id}/rerun", json={})
        check("重跑返回 running", r.status_code == 200
              and r.json().get("status") == "running", r.text[:200])
        rerun_id = (r.json().get("run") or {}).get("run_id", "")
        rerun = _wait_run(client, pid, wf_id, rerun_id)
        check("重跑实例成功", rerun.get("status") == "success",
              str(rerun)[:200])
        check("重跑 trigger=rerun", rerun.get("trigger") == "rerun",
              str(rerun.get("trigger")))

        # ============================================================ C cron
        section("C 调度配置")
        r = client.get(f"/api/projects/{pid}/tasks/{wf_id}/schedule")
        sched = r.json()
        check("默认未配置", sched.get("enabled") is False
              and sched.get("cron") == "", str(sched))

        r = client.put(f"/api/projects/{pid}/tasks/{wf_id}/schedule",
                       json={"cron": "0 2 * * *", "enabled": True})
        sched = r.json()
        check("保存 cron 并计算下次触发", r.status_code == 200
              and sched.get("enabled") is True
              and str(sched.get("next_run_at", "")).endswith("02:00:00"),
              str(sched)[:300])

        r = client.put(f"/api/projects/{pid}/tasks/{wf_id}/schedule",
                       json={"cron": "bad cron", "enabled": True})
        check("非法 cron 被拒（400）", r.status_code == 400, r.text[:200])

        r = client.put(f"/api/projects/{pid}/tasks/{wf_id}/schedule",
                       json={"cron": "0 2 * * *", "enabled": False})
        check("关闭调度清空下次触发",
              r.json().get("enabled") is False
              and r.json().get("next_run_at") == "", r.text[:200])

        # ============================================================ D 调度器
        section("D 调度线程触发（注入时间）")
        from apps.web.backend.workflow_scheduler import WorkflowScheduler

        hub = app.state.project_hub
        scheduler = WorkflowScheduler(hub, interval_s=9999)
        r = client.put(f"/api/projects/{pid}/tasks/{wf_id}/schedule",
                       json={"cron": "*/5 * * * *", "enabled": True})
        check("cron 已启用", r.json().get("enabled") is True, r.text[:200])
        fired = scheduler.tick(now=datetime.now() + timedelta(minutes=10))
        check("调度触发 1 次", len(fired) == 1, str(fired))
        if fired:
            cron_run_id = fired[0]["run_id"]
            cron_run = _wait_run(client, pid, wf_id, cron_run_id)
            check("调度实例成功", cron_run.get("status") == "success",
                  str(cron_run)[:300])
            check("调度实例 trigger=cron",
                  cron_run.get("trigger") == "cron",
                  str(cron_run.get("trigger")))
            again = scheduler.tick(now=datetime.now() + timedelta(minutes=10))
            check("同一次到期不会重复触发", again == [], str(again))

    print(f"\n{'ALL PASS' if not FAIL else 'FAILED_CHECKS ' + str(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
