"""原生同步（P1）Web API 验收：配置 → 预演 → 提交（全量/增量）→ 防线 → 审计。

跑法：python tests/test_sync.py
不依赖真实模型与运行中的服务（TestClient 直连 app）。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

BASE = os.environ.get("NL2SQL_TEST_TMP") or os.path.join(
    ROOT, "data", "_test_sync")
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

SRC_DB = os.path.join(WORK, "src.db")
DST_DB = os.path.join(WORK, "dst.db")
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))
        FAIL.append(name)


def section(title: str) -> None:
    print(f"\n[{title}]")


def _make_dbs() -> None:
    conn = sqlite3.connect(SRC_DB)
    conn.executescript(
        """
        CREATE TABLE probe_item (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            amount REAL NOT NULL
        );
        INSERT INTO probe_item (id, name, amount) VALUES
            (1, 'a', 10.5), (2, 'b', 20.0), (3, 'c', 30.25);
        """
    )
    conn.commit()
    conn.close()
    sqlite3.connect(DST_DB).close()   # 空目标库：表由同步自动创建


def _src_add_rows(rows: list[tuple[int, str, float]]) -> None:
    conn = sqlite3.connect(SRC_DB)
    conn.executemany(
        "INSERT INTO probe_item (id, name, amount) VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()


def _target_rows(table: str) -> list[dict]:
    conn = sqlite3.connect(DST_DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _create_project(client: TestClient, name: str) -> str:
    r = client.post("/api/projects", json={"name": name, "semantic": "none"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _add_source(client: TestClient, pid: str, source_id: str, path: str,
                *, read_only: bool = True) -> None:
    r = client.post(f"/api/projects/{pid}/sources", json={
        "source_id": source_id, "adapter": "sqlite", "label": source_id,
        "path": path, "read_only": read_only,
    })
    assert r.status_code == 200, r.text


def _set_write(client: TestClient, pid: str, **kwargs) -> None:
    payload = {"enabled": True, "source_id": "dst", "max_affected": 50}
    payload.update(kwargs)
    r = client.put(f"/api/projects/{pid}", json={"data_write": payload})
    assert r.status_code == 200, r.text


def _create_sync_task(client: TestClient, pid: str,
                      config: dict | str) -> tuple[int, dict]:
    content = config if isinstance(config, str) else json.dumps(
        config, ensure_ascii=False)
    r = client.post(f"/api/projects/{pid}/tasks", json={
        "name": "同步任务", "sql": content, "kind": "sync", "folder": "ods"})
    return r.status_code, r.json() if r.status_code == 200 else {}


def _update_content(client: TestClient, pid: str, tid: str,
                    config: dict) -> None:
    r = client.put(f"/api/projects/{pid}/tasks/{tid}", json={
        "sql": json.dumps(config, ensure_ascii=False), "note": "测试"})
    assert r.status_code == 200, r.text


def _preview(client: TestClient, pid: str, tid: str) -> dict:
    r = client.post(f"/api/projects/{pid}/tasks/{tid}/sync/preview")
    assert r.status_code == 200, r.text
    return r.json()


def _commit(client: TestClient, pid: str, tid: str, preview_id: str,
            *, confirm: bool = True) -> dict:
    r = client.post(f"/api/projects/{pid}/tasks/{tid}/sync/commit",
                    json={"preview_id": preview_id, "confirm": confirm})
    assert r.status_code == 200, r.text
    return r.json()


def main() -> int:
    _make_dbs()
    with TestClient(create_app()) as client:
        # ======================================================== A 配置校验
        section("A 同步配置校验")
        pid = _create_project(client, "同步验收")
        _add_source(client, pid, "src", SRC_DB, read_only=True)
        _add_source(client, pid, "dst", DST_DB, read_only=False)

        code, body = _create_sync_task(client, pid, "{}")
        check("草稿配置可保存", code == 200, str(code))
        tid = (body.get("task") or {}).get("task_id", "")
        check("任务类型 sync",
              (body.get("task") or {}).get("kind") == "sync",
              str((body.get("task") or {}).get("kind")))

        code, _ = _create_sync_task(client, pid, "not-json")
        check("非法 JSON 400", code == 400, str(code))
        code, _ = _create_sync_task(client, pid, {
            "source_id": "src", "table": "probe_item", "mode": "bad"})
        check("非法模式 400", code == 400, str(code))

        r = client.post(f"/api/projects/{pid}/tasks/{tid}/sync/preview")
        check("草稿预览 400", r.status_code == 400, r.text)

        # ======================================================== B 全量同步
        section("B 全量同步：预演 → 提交")
        _set_write(client, pid)
        _update_content(client, pid, tid, {
            "source_id": "src", "table": "probe_item",
            "target_table": "probe_item_copy", "mode": "full"})
        pv = _preview(client, pid, tid)
        check("预演状态 preview", pv.get("status") == "preview", pv.get("status"))
        check("预演行数 3", pv.get("rows") == 3, str(pv.get("rows")))
        check("预演样本 3 行", len(pv.get("sample") or []) == 3, str(pv.get("sample")))
        check("目标表尚不存在", pv.get("target_exists") is False, str(pv))
        check("预演不写目标库", _target_rows("probe_item_copy") == [], "")
        preview_id = pv.get("preview_id", "")

        r = client.post(f"/api/projects/{pid}/tasks/{tid}/sync/commit",
                        json={"preview_id": preview_id, "confirm": False})
        check("未确认 400", r.status_code == 400, r.text)
        r = client.post(f"/api/projects/{pid}/tasks/{tid}/sync/commit",
                        json={"preview_id": "sp-fake", "confirm": True})
        check("伪造凭据 400", r.status_code == 400, r.text)

        done = _commit(client, pid, tid, preview_id)
        check("提交 committed", done.get("status") == "committed", str(done))
        check("提交行数 3", done.get("rows") == 3, str(done.get("rows")))
        check("自动建表", done.get("created_table") is True, str(done))
        rows = _target_rows("probe_item_copy")
        check("目标库 3 行", len(rows) == 3, str(rows))
        check("目标值正确", rows[0]["name"] == "a" and rows[2]["amount"] == 30.25,
              str(rows))
        preview_reuse = client.post(
            f"/api/projects/{pid}/tasks/{tid}/sync/commit",
            json={"preview_id": preview_id, "confirm": True})
        check("凭据一次性", preview_reuse.status_code == 400,
              preview_reuse.text)

        # ======================================================== C 增量同步
        section("C 增量同步：水位线过滤")
        _update_content(client, pid, tid, {
            "source_id": "src", "table": "probe_item",
            "target_table": "probe_item_inc", "mode": "incremental",
            "watermark": "id"})
        pv = _preview(client, pid, tid)
        check("首次增量=全量 3 行", pv.get("rows") == 3, str(pv.get("rows")))
        check("首次水位前为空", pv.get("watermark_before") == "", str(pv))
        check("首次水位后 3", str(pv.get("watermark_after")) == "3", str(pv))
        _commit(client, pid, tid, pv["preview_id"])
        check("增量目标 3 行", len(_target_rows("probe_item_inc")) == 3, "")

        _src_add_rows([(4, "d", 40.0), (5, "e", 50.0)])
        pv = _preview(client, pid, tid)
        check("增量新行 2", pv.get("rows") == 2, str(pv.get("rows")))
        check("水位前 3", str(pv.get("watermark_before")) == "3", str(pv))
        check("水位后 5", str(pv.get("watermark_after")) == "5", str(pv))
        done = _commit(client, pid, tid, pv["preview_id"])
        check("增量提交 2 行", done.get("rows") == 2, str(done))
        check("增量目标 5 行", len(_target_rows("probe_item_inc")) == 5, "")

        pv = _preview(client, pid, tid)
        check("无新行时 0 行", pv.get("rows") == 0, str(pv.get("rows")))
        done = _commit(client, pid, tid, pv["preview_id"])
        check("0 行提交成功", done.get("status") == "committed", str(done))
        check("目标仍 5 行", len(_target_rows("probe_item_inc")) == 5, "")

        # ======================================================== D 防线
        section("D 防线与边界")
        r = client.put(f"/api/projects/{pid}", json={
            "data_write": {"enabled": False, "source_id": "dst",
                           "max_affected": 50}})
        assert r.status_code == 200, r.text
        r = client.post(f"/api/projects/{pid}/tasks/{tid}/sync/preview")
        check("未开启受控写 403", r.status_code == 403, r.text)
        _set_write(client, pid)

        _set_write(client, pid, max_affected=2)
        _update_content(client, pid, tid, {
            "source_id": "src", "table": "probe_item",
            "target_table": "probe_item_limit", "mode": "full"})
        pv = _preview(client, pid, tid)
        check("超上限预演 failed", pv.get("status") == "failed", str(pv))
        check("超上限规则 affected_limit",
              pv.get("rule") == "affected_limit", str(pv))
        _set_write(client, pid)

        pv = _preview(client, pid, tid)
        client.put(f"/api/projects/{pid}/tasks/{tid}", json={"note": "改一下"})
        r = client.post(f"/api/projects/{pid}/tasks/{tid}/sync/commit",
                        json={"preview_id": pv["preview_id"], "confirm": True})
        check("任务被改后提交 409", r.status_code == 409, r.text)

        r = client.put(f"/api/projects/{pid}", json={
            "data_write": {"enabled": True, "source_id": "nope",
                           "max_affected": 50}})
        assert r.status_code == 200, r.text
        r = client.post(f"/api/projects/{pid}/tasks/{tid}/sync/preview")
        check("写入源不存在 400", r.status_code == 400, r.text)
        _set_write(client, pid)

        # ======================================================== E 审计/状态
        section("E 同步状态与审计")
        info = client.get(f"/api/projects/{pid}/tasks/{tid}/sync/info").json()
        check("水位线 5", str(info["state"]["watermark"]) == "5", str(info))
        events = info.get("events") or []
        statuses = {str(e.get("status")) for e in events}
        check("审计含 preview/committed/failed",
              {"preview", "committed", "failed"} <= statuses, str(statuses))
        check("审计保留目标表", any(
            e.get("target_table") == "probe_item_inc" for e in events), "")

        r = client.delete(f"/api/projects/{pid}/tasks/{tid}")
        check("删除任务 200", r.status_code == 200, r.text)
        r = client.get(f"/api/projects/{pid}/tasks/{tid}/sync/info")
        check("删除后 404", r.status_code == 404, r.text)

    if FAIL:
        print(f"\nFAILED_CHECKS {FAIL}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
