"""M7 Web API 验收：多项目 / 数据源 / 任务 / 项目路由。

不依赖真实模型：没有 Key 时 agent 会安全降级为规则规划器；
本测试只覆盖"项目中心 + 任务库 + 只读执行"这条与模型无关的路径。

跑法：python tests/test_m7_projects_api.py
"""
from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

BASE = os.environ.get("NL2SQL_TEST_TMP") or os.path.join(
    ROOT, "data", "_test_m7_projects_api")
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

GOLDEN = os.path.join(ROOT, "golden_set", "ecommerce.db")
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))
        FAIL.append(name)


def section(title: str) -> None:
    print(f"\n[{title}]")


def _new_client() -> TestClient:
    return TestClient(create_app())


def _create_project(client: TestClient, name: str = "演示项目") -> dict:
    r = client.post("/api/projects", json={
        "name": name, "description": "M7 验收", "semantic": "none"})
    assert r.status_code == 200, r.text
    return r.json()


def _add_source(client: TestClient, pid: str) -> dict:
    r = client.post(f"/api/projects/{pid}/sources", json={
        "source_id": "shop", "adapter": "sqlite", "label": "电商演示库",
        "path": GOLDEN})
    assert r.status_code == 200, r.text
    return r.json()


TASK_SQL = (
    "SELECT c.channel_name, ROUND(SUM(f.amount), 2) AS amount "
    "FROM fact_order f JOIN dim_channel c ON f.channel_id = c.channel_id "
    "GROUP BY c.channel_name ORDER BY amount DESC LIMIT 10"
)


def main() -> int:
    with _new_client() as client:
        # ============================================================ A 项目
        section("A 项目 CRUD")
        r = client.get("/api/projects")
        check("项目列表 200", r.status_code == 200, r.text[:200])
        data = r.json()
        check("默认项目在列表里",
              data["default_project_id"] == "default"
              and any(p["id"] == "default" for p in data["projects"]),
              str(data)[:300])

        project = _create_project(client)
        pid = project["id"]
        check("创建项目成功且 slug 合法",
              bool(pid) and pid.isascii() and pid.islower(), pid)
        check("项目详情字段完整",
              project["name"] == "演示项目"
              and project["semantic"] == "none"
              and project["is_default"] is False, str(project)[:300])

        r = client.get(f"/api/projects/{pid}")
        check("查询项目详情", r.status_code == 200
              and r.json()["id"] == pid, r.text[:200])

        r = client.put(f"/api/projects/{pid}", json={"name": "改名项目"})
        check("修改项目名", r.status_code == 200
              and r.json()["name"] == "改名项目", r.text[:200])

        r = client.put("/api/projects/default", json={"name": "x"})
        check("默认项目不可修改（400）", r.status_code == 400, r.text[:200])
        r = client.delete("/api/projects/default")
        check("默认项目不可删除（400）", r.status_code == 400, r.text[:200])

        r = client.get("/api/projects/not-a-project")
        check("不存在的项目 404", r.status_code == 404, r.text[:200])

        # ============================================================ B 数据源
        section("B 数据源连接")
        source = _add_source(client, pid)
        check("数据源保存成功", source["source_id"] == "shop"
              and source["adapter"] == "sqlite", str(source)[:200])
        check("数据源回包不含口令", "password" not in source, str(source)[:200])

        r = client.post(f"/api/projects/{pid}/sources/test", json={
            "source_id": "shop", "adapter": "sqlite", "path": GOLDEN})
        payload = r.json()
        check("连接测试 ok", r.status_code == 200 and payload.get("ok") is True,
              r.text[:300])
        check("连接测试返回表", len(payload.get("tables") or []) > 0,
              str(len(payload.get("tables") or [])))

        r = client.post(f"/api/projects/{pid}/sources/test", json={
            "source_id": "bad", "adapter": "sqlite",
            "path": os.path.join(WORK, "missing.db")})
        check("坏路径 → 200 + ok:false",
              r.status_code == 200 and r.json().get("ok") is False, r.text[:200])

        r = client.get(f"/api/projects/{pid}/sources/shop/tables")
        check("表结构接口", r.status_code == 200
              and len(r.json().get("tables") or []) > 0, r.text[:200])

        # ============================================================ C 任务
        section("C 任务 CRUD / 版本 / 执行")
        r = client.post(f"/api/projects/{pid}/tasks", json={
            "name": "各渠道成交金额", "sql": TASK_SQL,
            "description": "验收任务", "tags": ["渠道", "金额"],
            "source_id": "shop"})
        check("创建任务", r.status_code == 200, r.text[:300])
        body = r.json()
        task = body["task"]
        tid = task["task_id"]
        check("任务初始 v1", body["revision"] == 1, str(body)[:200])
        check("任务带 source_id", task["source_id"] == "shop", str(task)[:200])

        r = client.post(f"/api/projects/{pid}/tasks", json={
            "name": "写任务", "sql": "DELETE FROM fact_order"})
        check("写 SQL 任务被拒（400）", r.status_code == 400, r.text[:200])
        r = client.post(f"/api/projects/{pid}/tasks", json={
            "name": "多语句", "sql": "SELECT 1; SELECT 2"})
        check("多语句任务被拒（400）", r.status_code == 400, r.text[:200])

        r = client.get(f"/api/projects/{pid}/tasks")
        check("任务列表", r.status_code == 200 and r.json()["total"] == 1,
              r.text[:200])

        r = client.post(f"/api/projects/{pid}/tasks/{tid}/run", json={})
        payload = r.json()
        check("任务执行 200", r.status_code == 200, r.text[:300])
        check("任务执行 status=ok", payload.get("status") == "ok",
              str(payload)[:300])
        rows = (payload.get("result") or {}).get("rows") or []
        check("任务执行返回 5 行", len(rows) == 5, str(rows)[:200])
        check("执行后任务记录 last_status",
              payload.get("task", {}).get("last_status") == "ok",
              str(payload.get("task"))[:200])

        # 执行失败路径：列不存在 → HTTP 200 + status=error
        r = client.post(f"/api/projects/{pid}/tasks", json={
            "name": "坏列任务", "sql": "SELECT no_such_column FROM fact_order"})
        bad_tid = r.json()["task"]["task_id"]
        r = client.post(f"/api/projects/{pid}/tasks/{bad_tid}/run", json={})
        check("坏 SQL 执行 → 200 + error 状态",
              r.status_code == 200 and r.json().get("status") in ("error", "invalid_sql"),
              r.text[:300])

        # 用户修改 → v2
        r = client.put(f"/api/projects/{pid}/tasks/{tid}", json={
            "sql": TASK_SQL.replace("LIMIT 10", "LIMIT 5"),
            "note": "缩小到 5 行"})
        check("用户修改生成 v2", r.status_code == 200
              and r.json()["revision"] == 2, r.text[:300])

        r = client.get(f"/api/projects/{pid}/tasks/{tid}")
        check("任务详情的版本历史含 2 条",
              len(r.json().get("revisions") or []) == 2, r.text[:300])

        # 回滚 → v3
        r = client.post(
            f"/api/projects/{pid}/tasks/{tid}/revisions/1/restore")
        check("回滚生成 v3", r.status_code == 200
              and r.json()["revision"] == 3, r.text[:300])

        # 项目列表里的任务计数
        r = client.get("/api/projects")
        target = next(p for p in r.json()["projects"] if p["id"] == pid)
        check("项目列表任务计数 = 2", target["task_count"] == 2, str(target))

        # 删除任务
        r = client.delete(f"/api/projects/{pid}/tasks/{bad_tid}")
        check("删除任务", r.status_code == 200, r.text[:200])
        r = client.get(f"/api/projects/{pid}/tasks")
        check("删除后任务计数 = 1", r.json()["total"] == 1, r.text[:200])

        # ============================================================ D 默认项目
        section("D 默认项目边界")
        r = client.get("/api/projects/default/tasks")
        check("默认项目未启用任务库 → 400 可读提示",
              r.status_code == 400 and "任务" in str(r.json().get("detail")),
              r.text[:300])
        r = client.post("/api/projects/default/sources", json={
            "source_id": "x", "adapter": "sqlite", "path": GOLDEN})
        check("默认项目数据源不可编辑（400）", r.status_code == 400, r.text[:200])

        # ============================================================ E 项目路由
        section("E X-Project-Id 路由")
        r = client.get("/api/info", headers={"X-Project-Id": pid})
        check("项目 /api/info 200", r.status_code == 200, r.text[:200])
        info = r.json()
        check("项目 /api/info engine_ready",
              info.get("engine_ready") is True, str(info)[:300])
        check("项目 /api/info 语义层为空（semantic=none）",
              (info.get("semantic") or {}).get("metrics") == 0,
              str(info.get("semantic"))[:200])

        r = client.get("/api/info", headers={"X-Project-Id": "nope"})
        check("未知项目头 → 404", r.status_code == 404, r.text[:200])

        # 删除项目（连带项目目录）
        r = client.delete(f"/api/projects/{pid}")
        check("删除项目", r.status_code == 200, r.text[:200])
        r = client.get(f"/api/projects/{pid}")
        check("删除后项目 404", r.status_code == 404, r.text[:200])

    # ============================================================ F 默认项目任务库
    section("F NL2SQL_TASKS_ENABLED=1 时默认项目可建任务")
    os.environ["NL2SQL_TASKS_ENABLED"] = "1"
    try:
        with _new_client() as client:
            r = client.get("/api/projects/default/tasks")
            check("默认项目任务列表 200", r.status_code == 200, r.text[:300])
            r = client.post("/api/projects/default/tasks", json={
                "name": "默认项目任务",
                "sql": "SELECT COUNT(*) AS n FROM fact_order"})
            check("默认项目可建任务", r.status_code == 200, r.text[:300])
            tid = r.json()["task"]["task_id"]
            r = client.post(f"/api/projects/default/tasks/{tid}/run", json={})
            check("默认项目任务可执行",
                  r.status_code == 200 and r.json().get("status") == "ok",
                  r.text[:300])
    finally:
        os.environ.pop("NL2SQL_TASKS_ENABLED", None)

    shutil.rmtree(WORK, ignore_errors=True)
    print()
    if FAIL:
        print(f"M7 Web API 验收失败 {len(FAIL)} 项：{FAIL}")
        return 1
    print("M7 Web API 验收全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
