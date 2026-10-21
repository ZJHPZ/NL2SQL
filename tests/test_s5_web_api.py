"""S5 验收（后端）：HTTP 接口契约与安全边界。

用 FastAPI TestClient 跑真实请求（含 lifespan），因此测到的不是
"函数能不能调"，而是**接口形状与状态语义**——这才是前端依赖的东西。

重点验证（这些错了前端一定坏）：
    1. 响应字段齐全且 JSON 安全（不能有 Decimal/date 泄漏成 500）
    2. `/api/execute` **确实走 guard**：写操作必须被拒
    3. 失败路径返回结构化反馈而不是 HTTP 500
    4. 知识库测试检索能给出命中原因

运行:
    python tests/test_s5_web_api.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_DB = os.path.join(ROOT, "golden_set", "ecommerce.db")
NOW = "2024-06-30"

_failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        _failures.append(f"{label} {detail}")


def main() -> int:
    base = os.environ.get("NL2SQL_TEST_TMP") or os.path.join(ROOT, "data", "_test_s5")
    work = os.path.join(base, "run")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    db = os.path.join(work, "shop.db")
    shutil.copy2(GOLDEN_DB, db)

    # 壳层从环境变量读配置——这里就把测试环境注入进去
    os.environ["NL2SQL_DB"] = db
    os.environ["NL2SQL_STATE_DIR"] = os.path.join(work, "state")
    os.environ["NL2SQL_SOURCE_ID"] = "shop"
    os.environ["NL2SQL_PLANNER"] = "rule"        # 不依赖模型，测试可离线跑
    os.environ.pop("NL2SQL_LLM_API_KEY", None)
    print(f"工作目录: {work}")
    print(f"数据源  : {db}")

    try:
        from fastapi.testclient import TestClient

        from apps.web.backend.main import create_app
    except Exception:
        traceback.print_exc()
        return 1

    with TestClient(create_app()) as client:
        test_info(client)
        test_query(client)
        test_execute_guard(client)
        test_metadata(client)
        test_knowledge(client)
        test_history(client)
        test_error_paths(client)

    shutil.rmtree(work, ignore_errors=True)
    print("\n" + "=" * 64)
    if _failures:
        print(f"S5 后端验收未通过：{len(_failures)} 项失败")
        for x in _failures[:20]:
            print("  -", x)
        return 1
    print("S5 后端验收全部通过")
    return 0


# --------------------------------------------------------------- 各组
def test_info(client) -> None:
    print("\n[1] /api/info —— 启动自检的界面化")
    r = client.get("/api/info")
    check(r.status_code == 200, "200", str(r.status_code))
    d = r.json()
    for field in ("version", "planner_mode", "llm_available", "sources",
                  "capabilities", "safety", "cache", "knowledge", "snapshot"):
        check(field in d, f"含字段 {field}", json.dumps(d, ensure_ascii=False)[:120])
    check(d["planner_mode"] == "rule", "无 Key 时 planner_mode=rule", d["planner_mode"])
    check(d["llm_available"] is False, "无 Key 时 llm_available=False")
    check(d["safety"]["read_only"] is True, "透出只读安全边界")
    check(d["safety"]["max_rows"] > 0, "透出强制 LIMIT")
    check(d["snapshot"].get("table_count") == 5, "快照已就绪（5 表）",
          str(d.get("snapshot")))
    check(d["knowledge"].get("total", 0) >= 5, "知识库已自动生成 schema 条目",
          str(d.get("knowledge")))
    print(f"        planner={d['planner_mode']} tables={d['snapshot'].get('table_count')} "
          f"knowledge={d['knowledge'].get('total')}")


def test_query(client) -> None:
    print("\n[2] /api/query —— 问答主路径")
    r = client.post("/api/query", json={
        "question": "2024年6月，数码品类的实收金额是多少？", "now": NOW})
    check(r.status_code == 200, "200", str(r.status_code))
    d = r.json()
    check(d["status"] == "ok", "status=ok", f"{d['status']} {d.get('message')}")
    check(d["ok"] is True, "ok=True")
    check(isinstance(d["sql"], str) and "SELECT" in d["sql"], "返回 SQL")
    check(d["result"] is not None, "返回结果")
    check(d["result"]["columns"] == ["实收金额"], "列名正确", str(d["result"]["columns"]))
    check(d["result"]["rows"] == [[99998.5]] or d["result"]["rows"] == [["99998.5"]],
          "结果值正确（JSON 安全）", str(d["result"]["rows"]))
    check(isinstance(d["result"]["rows"][0][0], (int, float)),
          "★ 数值保持 number 类型（前端排序依赖）",
          type(d["result"]["rows"][0][0]).__name__)
    check("plan_ms" in d["trace"], "带各阶段耗时 trace", str(d["trace"]))
    check(d["planner_mode"] == "rule", "带规划器来源标注")

    # 二次查询应命中缓存
    r2 = client.post("/api/query", json={
        "question": "2024年6月，数码品类的实收金额是多少？", "now": NOW})
    d2 = r2.json()
    check(d2["cached"] is True, "★ 二次查询命中缓存", str(d2["cached"]))
    check(any("缓存" in n for n in d2["notes"]), "缓存命中在 notes 里说明",
          str(d2["notes"]))

    # 无法解析 → 不返回假 SQL
    r3 = client.post("/api/query", json={"question": "帮我看看最近怎么样", "now": NOW})
    d3 = r3.json()
    check(r3.status_code == 200, "unresolved 仍是 200（不是 HTTP 错误）",
          str(r3.status_code))
    check(d3["status"] == "unresolved", "status=unresolved", d3["status"])
    check(d3["sql"] is None, "★ 不返回假 SQL", str(d3["sql"]))
    check(bool(d3["suggestions"]), "给出可操作建议", str(d3["suggestions"]))


def test_execute_guard(client) -> None:
    print("\n[3] /api/execute —— 手写 SQL 的安全边界（关键）")
    # 正常查询
    r = client.post("/api/execute", json={
        "sql": "SELECT order_status, COUNT(*) AS n FROM fact_order GROUP BY order_status"})
    check(r.status_code == 200, "正常 SQL 200", str(r.status_code))
    d = r.json()
    check(d["status"] in ("ok", "truncated"), "正常 SQL 执行成功", d["status"])
    check(d["result"]["row_count"] >= 1, "返回行", str(d["result"]["row_count"]))

    # 写操作必须被拒 —— 这是安全边界，不是便利功能
    for sql, label in [
        ("DROP TABLE fact_order", "DROP"),
        ("DELETE FROM fact_order", "DELETE"),
        ("UPDATE fact_order SET amount = 0", "UPDATE"),
        ("INSERT INTO dim_channel VALUES (99,'x','y')", "INSERT"),
        ("SELECT 1; DROP TABLE fact_order", "多语句注入"),
        ("PRAGMA user_version = 99", "写 PRAGMA"),
    ]:
        rr = client.post("/api/execute", json={"sql": sql})
        dd = rr.json()
        check(rr.status_code == 200 and dd["status"] == "blocked",
              f"拒绝 {label}", f"status={dd.get('status')} code={dd.get('error_code')}")
    # 拒绝时不应有结果
    rr = client.post("/api/execute", json={"sql": "DROP TABLE fact_order"})
    check(rr.json()["result"] is None, "被拒时不返回结果")

    # 注释/字符串里的写操作不该误杀
    for sql, label in [
        ("/* DROP TABLE x */ SELECT 1 AS ok", "注释含 DROP"),
        ("SELECT 'DROP TABLE x' AS note", "字符串含 DROP"),
    ]:
        rr = client.post("/api/execute", json={"sql": sql})
        check(rr.json()["status"] != "blocked", f"放行 {label}",
              rf"{rr.json().get('status')} {rr.json().get('message')}")

    # 强制 LIMIT：需要一个**真能超过 max_rows（1000）**的查询。
    # 单表最多 810 行，所以用自连接放大结果集——
    # 之前直接用 SELECT * FROM fact_order 是测试数据选错，不是功能问题。
    rr = client.post("/api/execute", json={
        "sql": "SELECT a.order_id AS x, b.order_id AS y"
               " FROM fact_order a CROSS JOIN fact_order b"})
    d = rr.json()
    check(d["status"] == "truncated", "超过上限时被强制截断",
          f"{d['status']} rows={d['result']['row_count'] if d['result'] else None}")
    check(d["result"] is not None and d["result"]["truncated"] is True,
          "truncated 标记为真")
    check(d["result"] is not None and d["result"]["row_count"] <= 1000,
          "行数被限制在上限内",
          str(d["result"]["row_count"] if d["result"] else None))
    check(bool(d["suggestions"]), "截断时给出建议", str(d["suggestions"]))

    # 810 行的查询**不该**被截断（验证上限判断没有过严）
    rr = client.post("/api/execute", json={"sql": "SELECT * FROM fact_order"})
    d = rr.json()
    check(d["status"] == "ok", "810 行不触发截断", d["status"])

    # 手写 SQL 结果也缓存
    sql = "SELECT COUNT(*) AS n FROM dim_customer"
    client.post("/api/execute", json={"sql": sql})
    rr = client.post("/api/execute", json={"sql": sql})
    check(rr.json()["result"] is not None, "手写 SQL 可重复执行")

    # 表名不存在 → 结构化错误而非 500
    rr = client.post("/api/execute", json={"sql": "SELECT * FROM no_such_table"})
    check(rr.status_code == 200, "错误 SQL 返回 200 而非 500", str(rr.status_code))
    d = rr.json()
    check(d["status"] == "error" and d["message"], "返回可展示的错误信息",
          f"{d['status']} {d.get('message')}")


def test_metadata(client) -> None:
    print("\n[4] 元数据接口")
    r = client.get("/api/tables")
    check(r.status_code == 200, "200")
    tables = r.json()
    check(len(tables) == 5, "5 张表", str(len(tables)))
    fact = next((t for t in tables if t["name"] == "fact_order"), None)
    check(fact is not None, "含 fact_order")
    check(fact and fact["row_estimate"] == 810, "行数 810", str(fact and fact["row_estimate"]))
    check(fact and fact["column_count"] == 10, "列数 10（含生成列）",
          str(fact and fact["column_count"]))
    check(fact and any(c["generated"] for c in fact["columns"]),
          "★ 生成列 amount 被标记", str([c["name"] for c in (fact or {}).get("columns", []) if c["generated"]]))

    r = client.get("/api/ddl?max_tables=2")
    check(r.status_code == 200 and "CREATE TABLE" in r.json()["ddl"], "DDL 可渲染")
    check("另有 3 张表未显示" in r.json()["ddl"], "DDL 限额生效")

    r = client.post("/api/snapshot", json={"force": False})
    check(r.status_code == 200 and r.json()["table_count"] == 5, "快照刷新")


def test_knowledge(client) -> None:
    print("\n[5] 知识库接口")
    r = client.get("/api/knowledge")
    check(r.status_code == 200, "200")
    items = r.json()
    check(len(items) >= 5, "自动生成的 schema 条目可列出", str(len(items)))
    check(all("version" in i for i in items), "条目带版本号")

    # 新增
    r = client.post("/api/knowledge", json={
        "type": "business", "title": "GMV 口径",
        "body": "GMV 含优惠、不剔除退款，与实收金额不同", "source_id": "shop"})
    check(r.status_code == 200, "新增条目")
    key = r.json()["key"]
    check(r.json()["version"] == 1, "首次写入 v1")

    # 更新 → 版本递增
    r = client.post("/api/knowledge", json={
        "key": key, "type": "business", "title": "GMV 口径",
        "body": "GMV = SUM(amount)，含优惠不剔除退款", "source_id": "shop"})
    check(r.json()["version"] == 2, "更新产生 v2", str(r.json()["version"]))

    # 历史与回滚
    r = client.get(f"/api/knowledge/{key}/history")
    check(r.status_code == 200 and len(r.json()) == 2, "历史 2 个修订",
          str(len(r.json()) if r.status_code == 200 else r.status_code))
    r = client.post(f"/api/knowledge/{key}/rollback?version=1")
    check(r.status_code == 200 and r.json()["version"] == 3,
          "回滚生成 v3（不复用旧号）", str(r.json().get("version")))

    # 测试检索（核心能力）
    r = client.post("/api/knowledge/test", json={
        "text": "fact_order 有哪些字段", "source_id": "shop", "top_k": 5})
    check(r.status_code == 200, "测试检索 200")
    hits = r.json()
    check(bool(hits), "有命中", str(len(hits)))
    check(all(h["why"] for h in hits), "★ 每条命中带 why（命中原因）",
          str([h.get("why") for h in hits]))
    check(any(h["type"] == "schema" for h in hits), "命中 schema 条目")
    print(f"        命中: {[(h['title'][:12], h['why']) for h in hits[:3]]}")

    # 停用 → 退出检索
    r = client.post(f"/api/knowledge/{key}/toggle", json={"enabled": False})
    check(r.status_code == 200 and r.json()["enabled"] is False, "停用成功")
    r = client.get("/api/knowledge")
    check(key not in [i["key"] for i in r.json()], "停用后不出现在默认列表")
    r = client.get("/api/knowledge?include_disabled=true")
    check(key in [i["key"] for i in r.json()], "include_disabled 可看到")

    # few-shot 排查
    r = client.get("/api/knowledge/few-shot?question=6月各渠道实收金额&top_k=2")
    check(r.status_code == 200, "few-shot 查询接口可用")


def test_history(client) -> None:
    print("\n[6] 历史接口")
    client.post("/api/query", json={"question": "2024年6月各渠道实收金额", "now": NOW})
    r = client.get("/api/history?limit=10")
    check(r.status_code == 200, "200")
    records = r.json()
    check(len(records) >= 1, "历史有记录", str(len(records)))
    check(all("plan_fingerprint" in x for x in records), "含计划指纹（可回放）")
    check(any(x["question"].startswith("2024年6月") for x in records),
          "记录的是用户原始问题")


def test_error_paths(client) -> None:
    print("\n[7] 错误路径（前端兜底依赖）")
    r = client.post("/api/query", json={"question": ""})
    check(r.status_code == 422, "空问题被校验拒绝", str(r.status_code))
    r = client.post("/api/execute", json={"sql": ""})
    check(r.status_code == 422, "空 SQL 被校验拒绝", str(r.status_code))
    r = client.post("/api/query", json={"question": "x", "source_id": "不存在"})
    check(r.status_code == 400, "未知数据源返回 400（不是 500 堆栈）", str(r.status_code))
    check(r.json().get("error") == "config_error", "错误体是结构化的",
          str(r.json()))
    r = client.get("/api/knowledge/不存在的key/history")
    check(r.status_code == 200 and r.json() == [], "不存在条目的历史返回空数组")
    r = client.get("/api/cache")
    check(r.status_code == 200 and "by_source" in r.json(), "缓存统计可读")
    r = client.get("/api/health")
    check(r.status_code == 200 and r.json()["ok"] is True, "健康检查通过")


if __name__ == "__main__":
    raise SystemExit(main())
