"""受控写（P1）MySQL 目标验收：预演 = 事务内执行后回滚，确认后提交。

跑法（需要本机有一个**专用的实验库**，脚本会建/删自己的表）::

    $env:NL2SQL_TEST_MYSQL_USER="nl2sql_sync"
    $env:NL2SQL_TEST_MYSQL_PASSWORD="..."
    $env:NL2SQL_TEST_MYSQL_DATABASE="nl2sql_sync_lab"
    python tests/test_write_mysql.py

可选 `NL2SQL_TEST_MYSQL_HOST`（默认 127.0.0.1）/`NL2SQL_TEST_MYSQL_PORT`（默认 3306）。
缺少账号环境变量时直接 SKIP，不当作失败；密码只从环境读，不写进仓库。
**不要把实验库指向业务库**：脚本会 DROP 自己的 `probe_order` 表。
"""
from __future__ import annotations

import os
import shutil
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

ENABLED = bool(MYSQL_USER and MYSQL_DATABASE)
if ENABLED and MYSQL_DATABASE.strip().lower() in SYSTEM_DBS:
    print(f"REFUSE: 不能把系统库 {MYSQL_DATABASE} 当作实验库")
    raise SystemExit(2)

BASE = os.path.join(ROOT, "data", "_test_write_mysql")
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

TABLE = "probe_order"
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))
        FAIL.append(name)


def section(title: str) -> None:
    print(f"\n[{title}]")


def mysql_connect():
    import pymysql

    return pymysql.connect(
        host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
        password=MYSQL_PASSWORD, database=MYSQL_DATABASE,
        charset="utf8mb4", autocommit=True, connect_timeout=5.0,
    )


def reset_table() -> None:
    conn = mysql_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS `{TABLE}`")
            cur.execute(
                f"CREATE TABLE `{TABLE}` ("
                "`id` INT PRIMARY KEY, "
                "`status` VARCHAR(20) NOT NULL, "
                "`amount` DOUBLE NOT NULL"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
            cur.execute(
                f"INSERT INTO `{TABLE}` (`id`, `status`, `amount`) VALUES "
                "(1, 'pending', 10.5), (2, 'pending', 20.0), (3, 'keep', 30.25)"
            )
    finally:
        conn.close()


def rows() -> dict[int, tuple[str, float]]:
    conn = mysql_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT id, status, amount FROM `{TABLE}` ORDER BY id")
            return {int(r[0]): (str(r[1]), float(r[2])) for r in cur.fetchall()}
    finally:
        conn.close()


def drop_table() -> None:
    conn = mysql_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS `{TABLE}`")
    finally:
        conn.close()


# ---------------------------------------------------------------- API helpers
def create_project(client, name: str) -> str:
    r = client.post("/api/projects", json={"name": name, "semantic": "none"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def add_mysql_source(client, pid: str, *, sid: str = "write_target",
                     database: str | None = None) -> None:
    r = client.post(f"/api/projects/{pid}/sources", json={
        "source_id": sid, "adapter": "mysql", "label": "MySQL 实验库",
        "host": MYSQL_HOST, "port": MYSQL_PORT, "user": MYSQL_USER,
        "database": database or MYSQL_DATABASE, "password": MYSQL_PASSWORD,
        "read_only": False,
    })
    assert r.status_code == 200, r.text


def set_write(client, pid: str, **kwargs) -> None:
    payload = {"enabled": True, "source_id": "write_target", "max_affected": 5}
    payload.update(kwargs)
    r = client.put(f"/api/projects/{pid}", json={"data_write": payload})
    assert r.status_code == 200, r.text


def create_write_task(client, pid: str, sql: str) -> tuple[int, dict]:
    r = client.post(f"/api/projects/{pid}/tasks", json={
        "name": "MySQL 写入任务", "sql": sql, "kind": "write", "folder": "ods"})
    return r.status_code, (r.json() if r.status_code == 200 else {})


def dry_run(client, pid: str, tid: str) -> dict:
    r = client.post(f"/api/projects/{pid}/tasks/{tid}/write/dry-run", json={})
    assert r.status_code == 200, r.text
    return r.json()


def commit(client, pid: str, tid: str, dry_run_id: str) -> dict:
    r = client.post(f"/api/projects/{pid}/tasks/{tid}/write/commit",
                    json={"dry_run_id": dry_run_id, "confirm": True})
    assert r.status_code == 200, r.text
    return r.json()


def main() -> int:
    if not ENABLED:
        print("SKIP: 未设置 NL2SQL_TEST_MYSQL_USER / NL2SQL_TEST_MYSQL_PASSWORD / "
              "NL2SQL_TEST_MYSQL_DATABASE")
        return 0

    print(f"TARGET MySQL {MYSQL_HOST}:{MYSQL_PORT} / {MYSQL_DATABASE} / {MYSQL_USER}")

    from fastapi.testclient import TestClient  # noqa: E402

    from apps.web.backend.main import create_app  # noqa: E402

    reset_table()
    with TestClient(create_app()) as client:
        section("A 项目与写目标配置")
        pid = create_project(client, "MySQL 写入验收")
        add_mysql_source(client, pid)
        meta = client.get(f"/api/projects/{pid}").json()
        sources = {s["source_id"]: s for s in meta.get("sources") or []}
        check("MySQL 目标源已保存",
              sources.get("write_target", {}).get("adapter") == "mysql",
              str(sources.get("write_target")))
        check("口令只回 has_password",
              sources.get("write_target", {}).get("has_password") is True,
              str(sources.get("write_target")))
        check("响应不含 password",
              all("password" not in (s or {}) for s in (meta.get("sources") or [])))
        set_write(client, pid)
        code, body = create_write_task(
            client, pid,
            "UPDATE probe_order SET status = 'paid' WHERE id <= 2")
        check("写入任务创建", code == 200, str(code))
        task = body.get("task") or {}
        tid = str(task.get("task_id") or "")
        check("任务类型 write", task.get("kind") == "write", str(task.get("kind")))

        section("B 预演（事务内执行后回滚，不写目标）")
        pv = dry_run(client, pid, tid)
        check("预演状态", pv.get("status") == "preview", str(pv.get("status")))
        check("影响 2 行", pv.get("rows") == 2, str(pv.get("rows")))
        check("kind update", pv.get("kind") == "update", str(pv.get("kind")))
        check("目标适配器 mysql",
              pv.get("target_adapter") in (None, "mysql"),
              str(pv.get("target_adapter")))
        before = pv.get("before") or []
        after = pv.get("after") or []
        check("before 样本 2 行", len(before) == 2, str(len(before)))
        check("before 都是 pending",
              all(row.get("status") == "pending" for row in before),
              str(before))
        check("after 样本 2 行", len(after) == 2, str(len(after)))
        check("after 都是 paid",
              all(row.get("status") == "paid" for row in after), str(after))
        check("预演未写目标库", rows()[1][0] == "pending", str(rows()[1]))
        check("凭据已发放", bool(pv.get("dry_run_id")), str(pv.get("dry_run_id")))

        section("C 提交（确认后落库、凭据一次性、复核对不上拒绝）")
        r = client.post(f"/api/projects/{pid}/tasks/{tid}/write/commit",
                        json={"dry_run_id": pv.get("dry_run_id"), "confirm": False})
        check("未确认 400", r.status_code == 400, str(r.status_code))
        r = client.post(f"/api/projects/{pid}/tasks/{tid}/write/commit",
                        json={"dry_run_id": "wd-fake", "confirm": True})
        check("伪造凭据 400", r.status_code == 400, str(r.status_code))
        done = commit(client, pid, tid, str(pv["dry_run_id"]))
        check("提交状态", done.get("status") == "committed", str(done.get("status")))
        check("提交 2 行", done.get("rows") == 2, str(done.get("rows")))
        saved = rows()
        check("目标行 1 已 paid", saved[1][0] == "paid", str(saved[1]))
        check("目标行 2 已 paid", saved[2][0] == "paid", str(saved[2]))
        check("目标行 3 未动", saved[3][0] == "keep", str(saved[3]))
        r = client.post(f"/api/projects/{pid}/tasks/{tid}/write/commit",
                        json={"dry_run_id": pv.get("dry_run_id"), "confirm": True})
        check("凭据一次性（再提交 400）", r.status_code == 400, str(r.status_code))

        section("D 防线：守门 / 上限 / 预演后改任务 / 系统库")
        code, body = create_write_task(
            client, pid, "DELETE FROM probe_order")
        check("无 WHERE 的 DELETE 保存即拒（400）", code == 400, str(code))

        pv2 = dry_run(client, pid, tid)
        r = client.put(f"/api/projects/{pid}/tasks/{tid}", json={
            "sql": "UPDATE probe_order SET status = 'paid' WHERE id = 1",
            "note": "改任务"})
        check("改任务成功", r.status_code == 200, str(r.status_code))
        r = client.post(f"/api/projects/{pid}/tasks/{tid}/write/commit",
                        json={"dry_run_id": pv2.get("dry_run_id"), "confirm": True})
        check("预演后改任务 409", r.status_code == 409, str(r.status_code))

        set_write(client, pid, max_affected=1)
        code, body = create_write_task(
            client, pid,
            "UPDATE probe_order SET status = 'done' WHERE id <= 2")
        tid2 = str((body.get("task") or {}).get("task_id") or "")
        pv3 = dry_run(client, pid, tid2)
        check("超上限预演 failed", pv3.get("status") == "failed",
              str(pv3.get("status")))
        check("超上限规则", pv3.get("rule") == "affected_limit",
              str(pv3.get("rule")))
        check("超上限未写目标库", rows()[1][0] == "paid", str(rows()[1]))
        set_write(client, pid, max_affected=5)

        add_mysql_source(client, pid, sid="system_db", database="mysql")
        set_write(client, pid, source_id="system_db")
        r = client.post(f"/api/projects/{pid}/tasks/{tid2}/write/dry-run",
                        json={})
        check("系统库目标被拒（400）", r.status_code == 400,
              str(r.status_code))
        check("系统库被明确拒绝", "系统库" in r.text, r.text[:80])
        set_write(client, pid, source_id="write_target")

        section("E 审计")
        events = client.get(f"/api/projects/{pid}/write-audit?limit=20").json()
        audit = events.get("audit") or []
        statuses = [str(item.get("status")) for item in audit]
        check("审计含 preview", "preview" in statuses, str(statuses))
        check("审计含 committed", "committed" in statuses, str(statuses))
        check("审计含 failed", "failed" in statuses, str(statuses))
        committed = [item for item in audit if item.get("status") == "committed"]
        check("审计 committed 行数 2",
              bool(committed) and int(committed[0].get("rows") or 0) == 2,
              str(committed[:1]))

    drop_table()
    print()
    if FAIL:
        print(f"{len(FAIL)} FAILED: {FAIL}")
        return 1
    print("MySQL 写入验收全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
