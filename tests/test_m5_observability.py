"""M5 验收：观测与失败归因（统计接口 / 九态口径 / 工具调用 / 管理端点）。

这个套件守的是三件容易被"顺手改坏"的事：

| 事情 | 坏掉的方式 | 后果 |
|---|---|---|
| 状态口径 | 加了新 `QueryStatus` 却没加进统计 | 失败率**悄悄变低**，界面上看起来一切都好 |
| 追问不算失败 | 把 `need_clarification` 并进 failure | 主动澄清像系统出错，用户不敢回答 |
| 观测是旁路 | 回调抛异常把查询带崩 | 统计功能的存在把主功能搞挂了 |

统计的判分没有别的口径，就是"账本对得上"：写入 N 条、聚合出来的数
与手算一致；`since` 只筛时间；`recent_failures` 只列非成功条目。

运行:
    python tests/test_m5_observability.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import sys
import traceback
from typing import Any, get_args

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_DIR = os.path.join(ROOT, "golden_set")
GOLDEN_DB = os.path.join(GOLDEN_DIR, "ecommerce.db")

_failures: list[str] = []
_work = ""
_checks = 0


def check(cond: bool, label: str, detail: str = "") -> None:
    global _checks
    _checks += 1
    if cond:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        _failures.append(f"{label} {detail}")


def section(t: str) -> None:
    print("\n" + "=" * 68)
    print(t)
    print("=" * 68)


# --------------------------------------------------------------------- 装置
def _cfg(state: str):
    from nl2sql import NL2SQLConfig
    from nl2sql.config import SourceConfig, SourceOptions

    cfg = NL2SQLConfig(
        sources={"shop": SourceConfig(
            source_id="shop", adapter="sqlite",
            options=SourceOptions(path=GOLDEN_DB))},
        default_source="shop",
        state_dir=state,
    )
    cfg.agent.planner = "rule"
    return cfg


class ScriptedLLM:
    """按脚本返回决策 JSON（与 M2 测试同一契约：`chat` 不是 `complete`）。"""

    name = "scripted"
    model = "scripted-1"

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls = 0

    def available(self) -> bool:
        return True

    def chat(self, messages, **kwargs):
        from nl2sql.contracts.llm import LLMResponse

        self.calls += 1
        item = (self._script.pop(0) if self._script
                else {"kind": "final", "answer": "（脚本用尽）"})
        text = item if isinstance(item, str) else json.dumps(
            item, ensure_ascii=False)
        return LLMResponse(text=text, model=self.model, latency_ms=1)


class StubTool:
    def __init__(self, name: str) -> None:
        from nl2sql.contracts.tool import ToolSpec

        self.spec = ToolSpec(
            name=name, kind="read", description=f"替身工具 {name}",
            params={"type": "object", "properties": {}},
            requires=("source.read",),
        )

    def run(self, args, ctx):
        from nl2sql.contracts.tool import ToolResult

        return ToolResult(ok=True, data={"columns": ["x"], "rows": [[1]]})


def _registry():
    from nl2sql.contracts.tool import Capability
    from nl2sql.plugins import ToolRegistry

    reg = ToolRegistry(granted=(Capability(name="source.read"),), origin="core")
    reg.register(StubTool("query.read"))
    return reg


DECISION_TOOL = {"kind": "tool", "tool": "query.read",
                 "args": {"sql": "SELECT 1"}, "thought": "查一下"}
DECISION_FINAL = {"kind": "final", "answer": "查到了，是 1。", "thought": "够了"}


# --------------------------------------------------------------------- 1
def part_status_contract() -> None:
    section("[1] 状态契约：统计口径与 QueryStatus 必须同步")
    from nl2sql.contracts.pipeline import QueryStatus
    from nl2sql.storage import KNOWN_STATUSES

    statuses = set(get_args(QueryStatus))
    check(statuses == set(KNOWN_STATUSES),
          "KNOWN_STATUSES 与 QueryStatus 九态一一对应（新增状态忘了统计会让失败率变低）",
          f"差集：{statuses ^ set(KNOWN_STATUSES)}")
    check(len(KNOWN_STATUSES) == 9, "统计覆盖九种状态",
          str(len(KNOWN_STATUSES)))
    check("need_clarification" in KNOWN_STATUSES,
          "追问在统计里单列（不是失败）")


# --------------------------------------------------------------------- 2
def part_store_math() -> None:
    section("[2] SQLite 观测存储：账本对得上 + 口径正确")
    from nl2sql.contracts.storage import QueryLogRecord, ToolCallRecord
    from nl2sql.storage import SqliteStatsStore

    db = os.path.join(_work, "stats.db")
    store = SqliteStatsStore(db)

    conn = sqlite3.connect(db)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    check(version == 5, "迁移到 v5", str(version))
    check({"query_log", "tool_log"} <= tables, "统计表已建", str(sorted(tables)))

    empty = store.query_stats()
    check(empty["total"] == 0, "空库 total=0", str(empty["total"]))
    check(empty["by_status"] == {s: 0 for s in (
        "ok", "empty", "truncated", "unresolved", "invalid_sql",
        "blocked", "timeout", "error", "need_clarification")},
        "空库九态零填充（界面不用猜「没出现」是 0 还是没统计）",
        str(empty["by_status"]))
    check(store.recent_failures() == [], "空库没有失败记录")
    check(empty["latency_ms"] == {"p50": 0, "p95": 0, "avg": 0},
          "空库延迟全 0", str(empty["latency_ms"]))

    # 12 条：success 4（ok 2 / empty 1 / truncated 1）、clarify 2、failure 6
    rows = [
        ("ok", 10, False), ("ok", 20, True), ("empty", 30, False),
        ("truncated", 40, True), ("need_clarification", 5, False),
        ("need_clarification", 15, False), ("unresolved", 100, False),
        ("unresolved", 200, False), ("blocked", 300, False),
        ("timeout", 400, False), ("error", 500, False),
        ("invalid_sql", 600, True),
    ]
    for i, (status, ms, cached) in enumerate(rows):
        store.add_query(QueryLogRecord(
            created_at=f"2024-01-01T00:00:{i:02d}",
            source_id="shop", status=status, question=f"问题{i}",
            planner="rule", runtime="loop", cached=cached,
            elapsed_ms=ms, row_count=i, steps=i % 3,
        ))
    stats = store.query_stats()
    check(stats["total"] == 12, "total=12", str(stats["total"]))
    check(stats["success"] == 4, "success 含 ok/empty/truncated（空表是数据事实，不是失败）",
          str(stats["success"]))
    check(stats["clarify"] == 2, "追问单列 2 条", str(stats["clarify"]))
    check(stats["failure"] == 6, "failure = 12-4-2 = 6", str(stats["failure"]))
    check(stats["by_status"]["unresolved"] == 2, "unresolved 2 条",
          str(stats["by_status"]["unresolved"]))
    check(stats["success_rate"] == round(4 / 12, 4), "成功率 4/12",
          str(stats["success_rate"]))
    check(stats["failure_rate"] == round(6 / 12, 4), "失败率 6/12",
          str(stats["failure_rate"]))
    check(stats["cached"] == 3, "缓存 3 条（ok/truncated/invalid_sql 各一）",
          str(stats["cached"]))
    # 排序后 12 个耗时：5,10,15,20,30,40,100,200,300,400,500,600
    check(stats["latency_ms"]["p50"] == 40, "P50 最近秩 = 第 6 个（40ms）",
          str(stats["latency_ms"]))
    check(stats["latency_ms"]["p95"] == 600, "P95 最近秩 = 第 12 个（600ms）",
          str(stats["latency_ms"]))
    check(stats["latency_ms"]["avg"] == int(2225 / 12),
          "平均耗时取整", str(stats["latency_ms"]["avg"]))

    # 工具：query.read 4 次（1 失败，耗时 10/20/30/40）、nl2sql.solve 1 次
    for i, (name, ok, ms) in enumerate([
        ("query.read", True, 10), ("query.read", True, 20),
        ("query.read", True, 30), ("query.read", False, 40),
        ("nl2sql.solve", True, 50),
    ]):
        store.add_tool_call(ToolCallRecord(
            created_at=f"2024-01-01T00:00:{i:02d}", tool=name, ok=ok,
            elapsed_ms=ms, session_id="s1", source_id="shop",
            error=None if ok else "boom",
        ))
    stats = store.query_stats()
    check(stats["tools"]["total"] == 5, "工具调用总数 5",
          str(stats["tools"]))
    check(stats["tools"]["failures"] == 1, "工具失败 1", str(stats["tools"]))
    qr = stats["tools"]["by_tool"]["query.read"]
    check(qr["calls"] == 4 and qr["failures"] == 1,
          "按工具分组的次数/失败", str(qr))
    check(qr["failure_rate"] == 0.25, "query.read 失败率 1/4", str(qr))
    check(qr["p95_ms"] == 40, "工具 P95=40", str(qr["p95_ms"]))
    check(abs(qr["avg_ms"] - 25) <= 1, "工具平均耗时 25ms", str(qr["avg_ms"]))

    # 时间过滤
    recent = store.query_stats(since="2024-01-01T00:00:08")
    check(recent["total"] == 4, "since 只保留后 4 条", str(recent["total"]))
    check(recent["by_status"]["invalid_sql"] == 1, "since 内状态正确")
    check(recent["cached"] == 1, "since 内缓存数正确", str(recent["cached"]))

    # 失败列表：只列非成功、排除追问；按时间倒序
    fails = store.recent_failures(limit=7)
    check(len(fails) == 6, "recent_failures 6 条（排除 ok/empty/truncated/追问）",
          str(len(fails)))
    check(all(f["status"] not in ("ok", "empty", "truncated",
                                  "need_clarification")
              for f in fails), "失败列表不含成功与追问",
          str([f["status"] for f in fails]))
    check(fails[0]["status"] == "invalid_sql", "按时间倒序（最新在前）",
          fails[0]["status"])
    check(all("question" in f and "created_at" in f for f in fails),
          "失败条目带问题原文与时间")
    limited = store.recent_failures(limit=2)
    check(len(limited) == 2, "limit 生效", str(len(limited)))

    store.close()


# --------------------------------------------------------------------- 3
def part_api_integration() -> None:
    section("[3] 引擎集成：ask 一次就记一笔（含缓存命中与失败归因）")
    from nl2sql import NL2SQL
    from nl2sql.semantics.golden_model import build_model

    cfg = _cfg(os.path.join(_work, "state_api"))
    engine = NL2SQL(cfg, semantic_model=build_model())
    ok_q = "2024年6月，数码品类的实收金额是多少？"

    try:
        o1 = engine.ask(ok_q, now="2024-06-30")
        o2 = engine.ask(ok_q, now="2024-06-30")
        o3 = engine.ask("帮我看看最近怎么样", now="2024-06-30")
    except Exception:
        traceback.print_exc()
        check(False, "引擎问答抛出异常（统计代码不该拖垮问答）")
        return

    check(o1.status == "ok", "第一次查询成功", o1.status)
    check(o2.cached is True, "第二次命中缓存", str(o2.cached))
    check(o3.status == "unresolved", "第三问 unresolved", o3.status)

    st = engine.stats()
    check(st["total"] == 3, "三次问答都记了（total=3）", str(st["total"]))
    check(st["by_status"]["ok"] == 2 and st["by_status"]["unresolved"] == 1,
          "状态分布正确", str(st["by_status"]))
    check(st["failure"] == 1, "失败 1 条（unresolved）", str(st["failure"]))
    check(st["cached"] == 1, "缓存命中 1 条", str(st["cached"]))
    check(st["planner_mode"] == "rule", "带规划器来源", st["planner_mode"])
    check("runtime" in st, "带执行器来源", str(st.get("runtime")))
    check(len(st["recent_failures"]) == 1,
          "最近失败 1 条", str(len(st["recent_failures"])))
    check(st["recent_failures"][0]["question"] == "帮我看看最近怎么样",
          "失败条目是那句没有映射的问题",
          st["recent_failures"][0]["question"])

    st_window = engine.stats(since="2999-01-01T00:00:00")
    check(st_window["total"] == 0, "未来时间点作为 since 时统计为空",
          str(st_window["total"]))
    st_all = engine.stats(since="2000-01-01T00:00:00")
    check(st_all["total"] == 3, "since 覆盖全部时仍是 3", str(st_all["total"]))

    # 关闭存储后也应返回空结构而不是炸
    engine.close()
    engine2 = NL2SQL(_cfg(os.path.join(_work, "state_none")),
                     semantic_model=build_model())
    engine2.connect()
    engine2.storage.stats = None
    try:
        fallback = engine2.stats()
        check(fallback["total"] == 0 and fallback["recent_failures"] == [],
              "没有统计存储时返回空结构（管理页不整页失败）",
              str(fallback)[:120])
    finally:
        engine2.close()


# --------------------------------------------------------------------- 4
def part_tool_hook() -> None:
    section("[4] 工具调用观测：两个执行器都上报，回调坏了不影响查询")
    from nl2sql.agent.graph import LangGraphRuntime, langgraph_available
    from nl2sql.agent.runtime import AgentRuntime
    from nl2sql.agent.machine import RunConfig
    from nl2sql.contracts.llm import PlannerContext  # noqa: F401 - 保持契约导入
    from nl2sql.contracts.session import SessionState

    def _session(tag: str):
        return SessionState(session_id=tag, source_id="shop", now="2024-06-30")

    def _make(rt_cls, recorder):
        provider = ScriptedLLM([DECISION_TOOL, DECISION_FINAL])
        if rt_cls is AgentRuntime:
            rt = AgentRuntime(provider=provider, registry=_registry(),
                              config=RunConfig(max_steps=4),
                              on_tool_call=recorder)
        else:
            rt = LangGraphRuntime(provider=provider, registry=_registry(),
                                  config=RunConfig(max_steps=4),
                                  on_tool_call=recorder)
        return rt, provider

    for label, rt_cls in (("loop", AgentRuntime), ("langgraph", LangGraphRuntime)):
        if rt_cls is LangGraphRuntime and not langgraph_available():
            print(f"  skip  {label}：未安装 langgraph")
            continue
        records: list[dict[str, Any]] = []
        rt, provider = _make(rt_cls, records.append)
        session = _session(f"s-{label}")
        outcome = rt.run("查一下", session=session)
        check(outcome.status == "ok", f"{label}: 查询成功", outcome.status)
        check(len(records) == 1, f"{label}: 上报 1 次工具调用",
              str(len(records)))
        if records:
            rec = records[0]
            check(rec["tool"] == "query.read", f"{label}: 记录工具名",
                  str(rec.get("tool")))
            check(rec["ok"] is True, f"{label}: 记录成功标志", str(rec.get("ok")))
            check(rec["session_id"] == f"s-{label}",
                  f"{label}: 记录会话（能对上哪一轮）", str(rec.get("session_id")))
            check(provider.calls == 2, f"{label}: 模型调用 2 次",
                  str(provider.calls))

    # 观测回调抛异常 → 查询照常完成（观测是旁路）
    def boom(payload):
        raise RuntimeError("统计坏了")

    rt = AgentRuntime(provider=ScriptedLLM([DECISION_TOOL, DECISION_FINAL]),
                      registry=_registry(), config=RunConfig(max_steps=4),
                      on_tool_call=boom)
    outcome = rt.run("查一下", session=_session("s-boom"))
    check(outcome.status == "ok", "回调抛异常不影响查询结果", outcome.status)


# --------------------------------------------------------------------- 5
def part_admin_endpoint() -> None:
    section("[5] 管理端点：GET /api/admin/stats")
    web_work = os.path.join(_work, "web")
    os.makedirs(web_work, exist_ok=True)
    db = os.path.join(web_work, "shop.db")
    shutil.copy2(GOLDEN_DB, db)
    os.environ["NL2SQL_DB"] = db
    os.environ["NL2SQL_STATE_DIR"] = os.path.join(web_work, "state")
    os.environ["NL2SQL_SOURCE_ID"] = "shop"
    os.environ["NL2SQL_PLANNER"] = "rule"
    os.environ.pop("NL2SQL_LLM_API_KEY", None)

    try:
        from fastapi.testclient import TestClient

        from apps.web.backend.main import create_app
    except Exception:
        traceback.print_exc()
        check(False, "无法导入 Web 壳（admin 端点测试跳过）")
        return

    with TestClient(create_app()) as client:
        r = client.get("/api/admin/stats?days=30")
        check(r.status_code == 200, "200", str(r.status_code))
        d = r.json()
        for field in ("total", "by_status", "success", "clarify", "failure",
                      "success_rate", "failure_rate", "cached", "cached_rate",
                      "latency_ms", "tools", "recent_failures", "since",
                      "planner_mode", "runtime"):
            check(field in d, f"含字段 {field}",
                  json.dumps(d, ensure_ascii=False)[:150])
        check(d["total"] == 0, "新库 total=0", str(d["total"]))
        check(len(d["by_status"]) == 9, "九态零填充", str(d["by_status"]))

        client.post("/api/query", json={
            "question": "2024年6月，数码品类的实收金额是多少？",
            "now": "2024-06-30",
        })
        client.post("/api/query", json={
            "question": "帮我看看最近怎么样", "now": "2024-06-30",
        })
        d = client.get("/api/admin/stats").json()
        check(d["total"] == 2, "两次问答都进了统计", str(d["total"]))
        check(d["by_status"]["ok"] == 1 and d["by_status"]["unresolved"] == 1,
              "状态分布正确", str(d["by_status"]))
        check(d["failure"] == 1, "失败 1 条", str(d["failure"]))
        check(len(d["recent_failures"]) == 1, "最近失败列表 1 条",
              str(d["recent_failures"]))
        check(d["recent_failures"][0]["status"] == "unresolved",
              "失败条目状态为 unresolved",
              d["recent_failures"][0]["status"])
        check(d["planner_mode"] == "rule", "带规划器来源", d["planner_mode"])

        r_bad = client.get("/api/admin/stats?days=0")
        check(r_bad.status_code == 200, "days=0 被夹到至少 1 天且不报错",
              str(r_bad.status_code))
        check(r_bad.json()["since"] is not None, "返回 since 窗口")


# --------------------------------------------------------------------- main
def main() -> int:
    global _work
    base = os.environ.get("NL2SQL_TEST_TMP") or os.path.join(
        ROOT, "data", "_test_m5")
    _work = os.path.join(base, "run")
    shutil.rmtree(_work, ignore_errors=True)
    os.makedirs(_work, exist_ok=True)
    print(f"工作目录: {_work}")

    try:
        part_status_contract()
        part_store_math()
        part_api_integration()
        part_tool_hook()
        part_admin_endpoint()
    except Exception:
        traceback.print_exc()
        _failures.append("未捕获异常")

    print("\n" + "=" * 68)
    if _failures:
        print(f"M5 观测验收未通过：{len(_failures)} 项失败（共 {_checks} 项检查）")
        for x in _failures[:20]:
            print("  -", x)
        return 1
    print(f"M5 观测验收全部通过（{_checks} 项检查）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
