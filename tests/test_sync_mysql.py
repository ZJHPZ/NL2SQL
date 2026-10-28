"""原生同步（P1）MySQL 目标验收：把 SQLite 源表搬到 MySQL 实验库。

跑法（需要本机有一个**专用的实验库**，脚本会建/删自己的表）::

    $env:NL2SQL_TEST_MYSQL_USER="nl2sql_sync"
    $env:NL2SQL_TEST_MYSQL_PASSWORD="..."
    $env:NL2SQL_TEST_MYSQL_DATABASE="nl2sql_sync_lab"
    python tests/test_sync_mysql.py

可选 `NL2SQL_TEST_MYSQL_HOST`（默认 127.0.0.1）/`NL2SQL_TEST_MYSQL_PORT`（默认 3306）。
缺少账号环境变量时直接 SKIP，不当作失败；密码只从环境读，不写进仓库。
**不要把实验库指向业务库**：脚本会 DROP 自己的 `probe_item` 表。
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

MYSQL_HOST = os.environ.get("NL2SQL_TEST_MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.environ.get("NL2SQL_TEST_MYSQL_PORT", "3306"))
MYSQL_USER = os.environ.get("NL2SQL_TEST_MYSQL_USER", "")
MYSQL_PASSWORD = os.environ.get("NL2SQL_TEST_MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.environ.get("NL2SQL_TEST_MYSQL_DATABASE", "")
SYSTEM_DBS = {"mysql", "information_schema", "performance_schema", "sys"}

SYNC_ENABLED = bool(MYSQL_USER and MYSQL_DATABASE)
# 缺账号时也要求库名不是系统库，避免误删环境自带的表
if SYNC_ENABLED and MYSQL_DATABASE.strip().lower() in SYSTEM_DBS:
    print(f"REFUSE: 不能把系统库 {MYSQL_DATABASE} 当作实验库")
    raise SystemExit(2)

BASE = os.path.join(ROOT, "data", "_test_sync_mysql")
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

SRC_DB = os.path.join(WORK, "src.db")
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))
        FAIL.append(name)


def section(title: str) -> None:
    print(f"\n[{title}]")


# ---------------------------------------------------------------- 环境与源表
def mysql_connect(database: str | None = None):
    import pymysql

    return pymysql.connect(
        host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
        password=MYSQL_PASSWORD, database=database or MYSQL_DATABASE,
        charset="utf8mb4", autocommit=True, connect_timeout=5.0,
    )


def drop_target_table(table: str) -> None:
    conn = mysql_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS `{table}`")
    finally:
        conn.close()


def target_table_exists(table: str) -> bool:
    conn = mysql_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES LIKE %s", (table,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def target_rows(table: str) -> list[dict]:
    conn = mysql_connect()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(f"SELECT * FROM `{table}` ORDER BY id")
            except Exception:  # noqa: BLE001 - 表还没建
                return []
            columns = [item[0] for item in cur.description or []]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def make_src_db() -> None:
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


def src_exec(sql: str, params: tuple = ()) -> None:
    conn = sqlite3.connect(SRC_DB)
    conn.execute(sql, params)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- API helpers
def create_project(client, name: str) -> str:
    r = client.post("/api/projects", json={"name": name, "semantic": "none"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def add_sqlite_source(client, pid: str, path: str, *, read_only: bool = True) -> None:
    r = client.post(f"/api/projects/{pid}/sources", json={
        "source_id": "src", "adapter": "sqlite", "label": "同步源",
        "path": path, "read_only": read_only,
    })
    assert r.status_code == 200, r.text


def add_mysql_source(client, pid: str) -> None:
    r = client.post(f"/api/projects/{pid}/sources", json={
        "source_id": "dst", "adapter": "mysql", "label": "MySQL 实验库",
        "host": MYSQL_HOST, "port": MYSQL_PORT, "user": MYSQL_USER,
        "database": MYSQL_DATABASE, "password": MYSQL_PASSWORD,
        "read_only": False,
    })
    assert r.status_code == 200, r.text


def set_write(client, pid: str, **kwargs) -> None:
    payload = {"enabled": True, "source_id": "dst", "max_affected": 10}
    payload.update(kwargs)
    r = client.put(f"/api/projects/{pid}", json={"data_write": payload})
    assert r.status_code == 200, r.text


def create_sync_task(client, pid: str, config: dict) -> tuple[int, dict]:
    r = client.post(f"/api/projects/{pid}/tasks", json={
        "name": "MySQL 同步任务", "sql": json.dumps(config, ensure_ascii=False),
        "kind": "sync", "folder": "ods"})
    return r.status_code, r.json() if r.status_code == 200 else {}


def update_content(client, pid: str, tid: str, config: dict) -> None:
    r = client.put(f"/api/projects/{pid}/tasks/{tid}", json={
        "sql": json.dumps(config, ensure_ascii=False), "note": "测试"})
    assert r.status_code == 200, r.text


def preview(client, pid: str, tid: str) -> dict:
    r = client.post(f"/api/projects/{pid}/tasks/{tid}/sync/preview")
    assert r.status_code == 200, r.text
    return r.json()


def commit(client, pid: str, tid: str, preview_id: str) -> dict:
    r = client.post(f"/api/projects/{pid}/tasks/{tid}/sync/commit",
                    json={"preview_id": preview_id, "confirm": True})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- 主流程
def main() -> int:
    if not SYNC_ENABLED:
        print("SKIP: 未设置 NL2SQL_TEST_MYSQL_USER / NL2SQL_TEST_MYSQL_PASSWORD / "
              "NL2SQL_TEST_MYSQL_DATABASE")
        return 0

    print(f"TARGET MySQL {MYSQL_HOST}:{MYSQL_PORT} / {MYSQL_DATABASE} / {MYSQL_USER}")
    make_src_db()
    drop_target_table("probe_item")

    from fastapi.testclient import TestClient  # noqa: E402

    from apps.web.backend.main import create_app  # noqa: E402

    with TestClient(create_app()) as client:
        section("A 项目与目标配置")
        pid = create_project(client, "MySQL 同步验收")
        add_sqlite_source(client, pid, SRC_DB, read_only=True)
        add_mysql_source(client, pid)
        meta = client.get(f"/api/projects/{pid}").json()
        sources = {s["source_id"]: s for s in meta.get("sources") or []}
        check("MySQL 源已保存", sources.get("dst", {}).get("adapter") == "mysql",
              str(sources.get("dst")))
        check("口令只回 has_password", sources.get("dst", {}).get("has_password") is True,
              str(sources.get("dst")))
        check("响应不含 password",
              all("password" not in (s or {}) for s in (meta.get("sources") or [])),
              str(meta.get("sources")))
        set_write(client, pid)
        code, body = create_sync_task(client, pid, {
            "source_id": "src", "table": "probe_item", "mode": "full"})
        check("同步任务创建", code == 200, str(code))
        tid = (body.get("task") or {}).get("task_id", "")
        check("任务类型 sync", (body.get("task") or {}).get("kind") == "sync",
              str((body.get("task") or {}).get("kind")))

        section("B 全量预演（不写目标）")
        pv = preview(client, pid, tid)
        check("预演状态", pv.get("status") == "preview", str(pv.get("status")))
        check("预演行数 3", pv.get("rows") == 3, str(pv.get("rows")))
        check("目标表尚不存在", pv.get("target_exists") is False, str(pv.get("target_exists")))
        check("目标适配器 mysql",
              pv.get("target_adapter") in (None, "mysql"), str(pv.get("target_adapter")))
        check("MySQL 无表", not target_table_exists("probe_item"))

        section("C 提交（自动建表 + 单事务写入）")
        done = commit(client, pid, tid, pv.get("preview_id", ""))
        check("提交 committed", done.get("status") == "committed", str(done))
        check("提交行数 3", done.get("rows") == 3, str(done.get("rows")))
        check("自动建表", done.get("created_table") is True, str(done))
        rows = target_rows("probe_item")
        check("MySQL 目标 3 行", len(rows) == 3, str(rows))
        check("目标值正确", rows and rows[0]["name"] == "a"
              and abs(float(rows[2]["amount"]) - 30.25) < 1e-6, str(rows))
        info = client.get(f"/api/projects/{pid}/tasks/{tid}/sync/info").json()
        statuses = [e["status"] for e in info.get("events") or []]
        check("审计含 preview/committed", "preview" in statuses and "committed" in statuses,
              str(statuses))

        section("D 全量重跑（目标表已存在）")
        src_exec("UPDATE probe_item SET amount = 22.0 WHERE id = 2")
        pv2 = preview(client, pid, tid)
        check("重跑预演 3 行", pv2.get("rows") == 3, str(pv2.get("rows")))
        check("目标表已存在", pv2.get("target_exists") is True, str(pv2.get("target_exists")))
        check("目标表已有 3 行", pv2.get("target_rows") == 3, str(pv2.get("target_rows")))
        done2 = commit(client, pid, tid, pv2.get("preview_id", ""))
        check("重跑提交", done2.get("status") == "committed", str(done2))
        rows2 = target_rows("probe_item")
        check("全量覆盖为 3 行", len(rows2) == 3, str(rows2))
        check("更新值已覆盖",
              any(abs(float(r["amount"]) - 22.0) < 1e-6 for r in rows2), str(rows2))

        section("E 增量（水位线）")
        # 目标表已有数据、但增量任务还没有水位：应拒绝而不是追加出重复行。
        update_content(client, pid, tid, {
            "source_id": "src", "table": "probe_item",
            "mode": "incremental", "watermark": "id"})
        guard = preview(client, pid, tid)
        check("增量无水位且目标已有数据被拦",
              guard.get("status") == "failed"
              and guard.get("rule") == "incremental_needs_init", str(guard))
        # 换一张空表初始化增量，从 0 行开始累积。
        update_content(client, pid, tid, {
            "source_id": "src", "table": "probe_item",
            "target_table": "probe_item_inc", "mode": "incremental",
            "watermark": "id"})
        pv3 = preview(client, pid, tid)
        check("增量首次读全表 3 行", pv3.get("rows") == 3, str(pv3.get("rows")))
        check("增量水位 0→3", pv3.get("watermark_after") == "3",
              str(pv3.get("watermark_after")))
        commit(client, pid, tid, pv3.get("preview_id", ""))
        src_exec("INSERT INTO probe_item (id, name, amount) VALUES "
                 "(4, 'd', 40.0), (5, 'e', 50.5)")
        pv4 = preview(client, pid, tid)
        check("增量预演 2 行", pv4.get("rows") == 2, str(pv4.get("rows")))
        check("增量水位 3→5", pv4.get("watermark_before") == "3"
              and pv4.get("watermark_after") == "5", str(pv4))
        done4 = commit(client, pid, tid, pv4.get("preview_id", ""))
        check("增量提交 2 行", done4.get("rows") == 2, str(done4))
        rows4 = target_rows("probe_item_inc")
        check("增量目标累计 5 行", len(rows4) == 5, str(rows4))
        check("全量表仍 3 行", len(target_rows("probe_item")) == 3,
              str(target_rows("probe_item")))

        section("F 防线")
        set_write(client, pid, max_affected=2, source_id="dst")
        update_content(client, pid, tid, {
            "source_id": "src", "table": "probe_item", "mode": "full"})
        bad = preview(client, pid, tid)
        check("超上限被拦 affected_limit",
              bad.get("status") == "failed" and bad.get("rule") == "affected_limit",
              str(bad))
        set_write(client, pid, max_affected=10)
        set_write(client, pid, enabled=False, source_id="")
        r = client.post(f"/api/projects/{pid}/tasks/{tid}/sync/preview")
        check("未开启受控写 403", r.status_code == 403, r.text)

    # 清理项目与实验表
    for table in ("probe_item", "probe_item_inc"):
        try:
            drop_target_table(table)
        except Exception as exc:  # noqa: BLE001
            print(f"  清理目标表 {table} 失败：{exc}")
    print("\nALL PASS" if not FAIL else f"\nFAILED: {FAIL}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
