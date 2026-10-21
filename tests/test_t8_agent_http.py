"""验证 agent 模式下的 HTTP 契约（界面依赖的字段）。

关心三件事：
    1. agent 的散文答案与结构化结果表**同时**返回
    2. 澄清返回 `need_clarification` + 选项，且**会话标识透出**
    3. 第二轮带上 session_id 能接上上下文

运行:
    $env:NL2SQL_LLM_API_KEY='sk-xxx'
    python tests/test_t8_agent_http.py
"""
from __future__ import annotations

import io
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
    key = os.environ.get("NL2SQL_LLM_API_KEY", "")
    if not key:
        print("未设置 NL2SQL_LLM_API_KEY，跳过。")
        return 1

    base = os.environ.get("NL2SQL_TEST_TMP") or os.path.join(ROOT, "data", "_test_t8")
    work = os.path.join(base, "run")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    db = os.path.join(work, "shop.db")
    shutil.copy2(GOLDEN_DB, db)

    os.environ["NL2SQL_DB"] = db
    os.environ["NL2SQL_STATE_DIR"] = os.path.join(work, "state")
    os.environ["NL2SQL_SOURCE_ID"] = "shop"
    os.environ["NL2SQL_PLANNER"] = "agent"

    from fastapi.testclient import TestClient

    from apps.web.backend.main import create_app

    print("=" * 70)
    print("agent 模式的 HTTP 契约")
    print("=" * 70)

    with TestClient(create_app()) as client:
        info = client.get("/api/info").json()
        print(f"planner_mode : {info['planner_mode']}")
        check(info["planner_mode"] == "agent", "服务运行在 agent 模式",
              info["planner_mode"])

        # ---------------------------------------------------------- 1) 表格问题
        print("\n[1] 表格类问题：散文答案 + 结构化结果表都要有")
        r = client.post("/api/query", json={
            "question": "2024年上半年，各渠道的实收金额和有效订单数是多少？"
                        "按实收金额降序排列。",
            "now": NOW,
        })
        check(r.status_code == 200, "200", str(r.status_code))
        d = r.json()
        check(d["status"] == "ok", "status=ok", d["status"])
        check(bool(d.get("answer")), "★ 返回 answer（给结论）",
              str(d.get("answer"))[:60])
        check(d.get("result") is not None, "★ 返回 result（给明细表）")
        if d.get("result"):
            res = d["result"]
            check(len(res["columns"]) == 3, "结果表 3 列", str(res["columns"]))
            check(res["row_count"] == 5, "结果表 5 行", str(res["row_count"]))
            check(all(isinstance(v, (int, float))
                      for v in res["rows"][0][1:]),
                  "★ 数值保持 number 类型（前端排序依赖）",
                  str(res["rows"][0]))
            print(f"        列: {res['columns']}")
            print(f"        首行: {res['rows'][0]}")
        check(d.get("sql"), "返回 SQL", str(d.get("sql"))[:50])
        check(bool(d.get("session_id")), "★ 透出 session_id（下一轮要用）",
              str(d.get("session_id")))
        print(f"        answer: {str(d.get('answer'))[:110]}")

        # ---------------------------------------------------------- 2) 澄清
        print("\n[2] 澄清：need_clarification + 选项 + 会话标识")
        r2 = client.post("/api/query", json={"question": "销售额是多少？", "now": NOW})
        d2 = r2.json()
        print(f"        status={d2['status']}")
        print(f"        message={d2.get('message')}")
        check(d2["status"] == "need_clarification",
              "追问时状态为 need_clarification", d2["status"])
        check(d2["ok"] is False, "追问不算 ok（但也不是错误）")
        check(bool(d2.get("candidates")), "★ 返回可选项供界面渲染按钮",
              str(d2.get("candidates")))
        check(d2.get("session_id"), "★ 追问也带 session_id")
        check(d2.get("result") is None, "追问时没有结果表")
        if d2.get("candidates"):
            print(f"        选项: {d2['candidates'][:4]}")

        # ---------------------------------------------------------- 3) 第二回合
        print("\n[3] 第二回合：带上 session_id 应接上上下文")
        sid = d2.get("session_id")
        if sid:
            r3 = client.post("/api/query", json={
                "question": "本月", "session_id": sid,
            })
            d3 = r3.json()
            print(f"        status={d3['status']}")
            print(f"        answer={str(d3.get('answer'))[:110]}")
            check(d3["status"] in ("ok", "empty"),
                  "第二回合能完成", f"{d3['status']} {d3.get('message')}")
            check(d3.get("session_id") == sid, "会话标识保持一致",
                  f"{d3.get('session_id')} vs {sid}")

        # ---------------------------------------------------------- 4) 缺字段
        print("\n[4] 契约完整性：界面需要的字段一个都不能少")
        for field in ("status", "question", "source_id", "sql", "result",
                      "error_code", "message", "suggestions", "candidates",
                      "applied_filters", "notes", "plan_fingerprint", "cached",
                      "elapsed_ms", "data_time", "trace", "ok", "planner_mode",
                      "session_id", "answer"):
            check(field in d, f"含字段 {field}")

        # ---------------------------------------------------------- 5) 安全不变
        print("\n[5] agent 模式下安全边界不变")
        r5 = client.post("/api/execute", json={"sql": "DROP TABLE fact_order"})
        check(r5.json()["status"] == "blocked", "★ 手写写操作仍被拒",
              r5.json()["status"])

    shutil.rmtree(work, ignore_errors=True)
    print("\n" + "=" * 70)
    if _failures:
        print(f"T8 验收未通过：{len(_failures)} 项失败")
        for x in _failures[:20]:
            print("  -", x)
        return 1
    print("T8 验收全部通过")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
