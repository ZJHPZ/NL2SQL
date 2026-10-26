"""受控写 Web API 验收（P1）：项目设置 → 写入任务 → 预演 → 提交 → 审计。

跑法：python tests/test_write_api.py
不依赖真实模型与运行中的服务（TestClient 直连 app）。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

BASE = os.environ.get("NL2SQL_TEST_TMP") or os.path.join(
    ROOT, "data", "_test_write_api")
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
        CREATE TABLE probe_order (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'new'
        );
        INSERT INTO probe_order (id, name, amount, status) VALUES
            (1, 'a', 10.0, 'new'),
            (2, 'b', 20.0, 'new'),
            (3, 'c', 30.0, 'new');
        """
    )
    conn.commit()
    conn.close()


def _probe_rows() -> dict[int, dict]:
    conn = sqlite3.connect(PROBE_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM probe_order ORDER BY id").fetchall()
    conn.close()
    return {int(row["id"]): dict(row) for row in rows}


def _create_project(client: TestClient, name: str) -> str:
    r = client.post("/api/projects", json={"name": name, "semantic": "none"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _add_source(client: TestClient, pid: str, path: str,
                source_id: str = "wdb") -> None:
    r = client.post(f"/api/projects/{pid}/sources", json={
        "source_id": source_id, "adapter": "sqlite",
        "label": "写入测试库", "path": path})
    assert r.status_code == 200, r.text


def _set_write(client: TestClient, pid: str, **kwargs) -> None:
    payload = {"enabled": True, "source_id": "wdb", "max_affected": 10}
    payload.update(kwargs)
    r = client.put(f"/api/projects/{pid}", json={"data_write": payload})
    assert r.status_code == 200, r.text


def _create_task(client: TestClient, pid: str, sql: str, *,
                 name: str = "受控写校验") -> tuple[int, dict]:
    r = client.post(f"/api/projects/{pid}/tasks", json={
        "name": name, "sql": sql, "kind": "write", "folder": "ods"})
    return r.status_code, r.json() if r.status_code == 200 else {}


def main() -> int:
    _make_probe_db()
    with TestClient(create_app()) as client:
        # ============================================================ A 设置
        section("A 项目写入设置")
        pid = _create_project(client, "受控写验收")
        _add_source(client, pid, PROBE_DB)
        r = client.get(f"/api/projects/{pid}")
        dw = r.json().get("data_write") or {}
        check("默认关闭", dw.get("enabled") is False, str(dw))
        check("默认上限 100", dw.get("max_affected") == 100, str(dw))

        _set_write(client, pid)
        r = client.get(f"/api/projects/{pid}")
        dw = r.json().get("data_write") or {}
        check("开启并绑定数据源",
              dw.get("enabled") is True and dw.get("source_id") == "wdb",
              str(dw))

        # ============================================================ B 写任务
        section("B 写入任务保存校验")
        status, body = _create_task(
            client, pid, "UPDATE probe_order SET status='paid' WHERE id=1")
        check("合法写任务可保存", status == 200, str(body)[:200])
        task_id = (body.get("task") or {}).get("task_id", "")
        check("kind=write", (body.get("task") or {}).get("kind") == "write")

        r = client.post(f"/api/projects/{pid}/tasks", json={
            "name": "非法全表更新", "sql": "UPDATE probe_order SET status='x'",
            "kind": "write"})
        check("无 WHERE 的写任务被拒（400）", r.status_code == 400,
              r.text[:200])
        r = client.post(f"/api/projects/{pid}/tasks", json={
            "name": "非法 DDL", "sql": "DROP TABLE probe_order",
            "kind": "write"})
        check("DDL 写任务被拒（400）", r.status_code == 400, r.text[:200])

        r = client.post(f"/api/projects/{pid}/tasks/{task_id}/run", json={})
        check("写任务不能走只读运行（400）", r.status_code == 400,
              r.text[:200])

        # ============================================================ C 预演
        section("C 预演（dry-run）")
        before_rows = _probe_rows()
        r = client.post(
            f"/api/projects/{pid}/tasks/{task_id}/write/dry-run", json={})
        body = r.json()
        check("预演 200 + status=preview",
              r.status_code == 200 and body.get("status") == "preview",
              r.text[:300])
        dry_run_id = body.get("dry_run_id", "")
        check("预演影响行数 1", body.get("rows") == 1, str(body.get("rows")))
        check("预演前后样本", len(body.get("before") or []) == 1
              and len(body.get("after") or []) == 1, str(body)[:300])
        check("预演不落库", _probe_rows()[1]["status"] == "new",
              str(_probe_rows()[1]))
        check("预演凭据存在", bool(dry_run_id))

        # ============================================================ D 提交
        section("D 提交（commit）")
        r = client.post(
            f"/api/projects/{pid}/tasks/{task_id}/write/commit",
            json={"dry_run_id": dry_run_id, "confirm": False})
        check("未确认提交被拒（400）", r.status_code == 400, r.text[:200])
        r = client.post(
            f"/api/projects/{pid}/tasks/{task_id}/write/commit",
            json={"dry_run_id": "wd-not-exist", "confirm": True})
        check("伪造凭据被拒（400）", r.status_code == 400, r.text[:200])

        r = client.post(
            f"/api/projects/{pid}/tasks/{task_id}/write/commit",
            json={"dry_run_id": dry_run_id, "confirm": True})
        body = r.json()
        check("提交 200 + status=committed",
              r.status_code == 200 and body.get("status") == "committed",
              r.text[:300])
        check("提交影响行数 1", body.get("rows") == 1, str(body.get("rows")))
        check("数据已落库", _probe_rows()[1]["status"] == "paid",
              str(_probe_rows()[1]))
        r = client.post(
            f"/api/projects/{pid}/tasks/{task_id}/write/commit",
            json={"dry_run_id": dry_run_id, "confirm": True})
        check("凭据一次性（重放 400）", r.status_code == 400, r.text[:200])

        # 预演后改动 SQL → 提交被拒，需重新预演
        r = client.post(
            f"/api/projects/{pid}/tasks/{task_id}/write/dry-run", json={})
        dry2 = r.json().get("dry_run_id", "")
        r = client.put(f"/api/projects/{pid}/tasks/{task_id}", json={
            "sql": "UPDATE probe_order SET status='done' WHERE id=2",
            "note": "改条件"})
        check("修改写任务 SQL", r.status_code == 200, r.text[:200])
        r = client.post(
            f"/api/projects/{pid}/tasks/{task_id}/write/commit",
            json={"dry_run_id": dry2, "confirm": True})
        check("预演后改动 → 409", r.status_code == 409, r.text[:200])

        # ============================================================ E 上限
        section("E 影响行数上限")
        r = client.post(f"/api/projects/{pid}/tasks", json={
            "name": "批量更新", "kind": "write",
            "sql": "UPDATE probe_order SET status='hot' WHERE amount > 0"})
        batch_id = (r.json().get("task") or {}).get("task_id", "")
        _set_write(client, pid, max_affected=2)
        r = client.post(
            f"/api/projects/{pid}/tasks/{batch_id}/write/dry-run", json={})
        body = r.json()
        check("超上限预演被拦截",
              body.get("status") == "failed"
              and body.get("rule") == "affected_limit", str(body)[:300])
        check("超上限数据未变",
              all(row["status"] != "hot" for row in _probe_rows().values()),
              str(_probe_rows()))

        # ============================================================ F 审计
        section("F 审计")
        r = client.get(f"/api/projects/{pid}/write-audit")
        audit = (r.json() or {}).get("audit") or []
        statuses = {row["status"] for row in audit}
        check("审计包含 preview / committed / failed",
              {"preview", "committed", "failed"} <= statuses, str(statuses))
        committed = [row for row in audit if row["status"] == "committed"]
        check("审计记录行数", bool(committed) and committed[0]["rows"] == 1,
              str(committed[:1]))

        # ============================================================ G 拒绝
        section("G 未开启项目 / 任务库保护")
        pid2 = _create_project(client, "未开启受控写")
        _add_source(client, pid2, PROBE_DB)
        status, body = _create_task(
            client, pid2, "DELETE FROM probe_order WHERE id=3")
        task2 = (body.get("task") or {}).get("task_id", "")
        r = client.post(
            f"/api/projects/{pid2}/tasks/{task2}/write/dry-run", json={})
        check("未开启受控写 → 403", r.status_code == 403, r.text[:200])

        # 把写入源指向任务库本身 → 拒绝
        _set_write(client, pid, source_id="taskdb")
        _add_source(
            client, pid,
            os.path.join(WORK, "projects", pid, "state", "tasks.db"),
            source_id="taskdb",
        )
        # 触发一次任务库初始化（GET 任务列表会构建 store）
        client.get(f"/api/projects/{pid}/tasks")
        r = client.post(
            f"/api/projects/{pid}/tasks/{task_id}/write/dry-run", json={})
        check("写入目标=任务库被拒（400）", r.status_code == 400,
              r.text[:300])

    print()
    if FAIL:
        print(f"FAILED: {len(FAIL)} 项")
        for name in FAIL:
            print(" -", name)
        return 1
    print("受控写 Web API 验收全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
