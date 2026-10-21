"""容错与上下文预算验收：模型重试 / 工具重试 / 决策修复 / Prompt 压缩。

这个套件守的是 agent 的"坏天气"行为——真模型和真数据库每天都会抖动：

| 机制 | 要防的坏情况 | 坏掉的后果 |
|---|---|---|
| 模型调用重试 | 超时/连接重置/限流这类**瞬态**故障 | 一次网络抖动就让用户看到"模型调用失败" |
| 不回退不可重试错误 | 把 401/参数错也重试两遍 | 白等两轮，错误信息还被埋掉 |
| 决策修复 | 输出被 max_tokens 截断、JSON 坏掉 | 状态 ok 但 message 是"格式无法解析" |
| 工具重试 | 只读工具撞上 `database is locked` | 明明重查就好，却把错误回灌给模型 |
| 写工具不重试 | 写工具重试一次 = 多写一次 | **数据被重复写入** |
| 上下文预算 | 多轮会话把工具输出全塞给模型 | Prompt 越来越长、token 越来越贵 |

口径说明：这里**不依赖真 Key**，用脚本化 provider / 替身工具，
所以每次运行结果确定；真模型下的重试证据在 `_tmp` 的排障日志里。

运行:
    python tests/test_agent_resilience.py
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")

from nl2sql.agent.context import (  # noqa: E402
    _OMIT_MARK,
    compress_prompt_history,
    compress_session_context,
)
from nl2sql.agent.factory import make_runtime  # noqa: E402
from nl2sql.agent.machine import (  # noqa: E402
    AgentCore,
    RunConfig,
    parse_decision,
)
from nl2sql.agent.resilience import (  # noqa: E402
    RetryPolicy,
    is_retryable_model_error,
    is_retryable_tool_error,
    retry_call,
)
from nl2sql.contracts.llm import LLMResponse  # noqa: E402
from nl2sql.contracts.session import SessionState  # noqa: E402
from nl2sql.contracts.tool import (  # noqa: E402
    Capability,
    ToolContext,
    ToolResult,
    ToolSpec,
)
from nl2sql.llm.providers import LLMError  # noqa: E402
from nl2sql.plugins import ToolRegistry  # noqa: E402

NOW = "2024-06-30T00:00:00"
_failures: list[str] = []
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


# ===================================================================== 替身
class ScriptedProvider:
    """按脚本回放：dict → JSON 决策；str → 原文；Exception → 抛出。

    和 M2 的 `ScriptedLLM` 同一契约（`chat` 返回 `LLMResponse`），
    只是多了"按项抛异常"和"自带 finish_reason"的能力，用来模拟坏天气。
    """

    name = "scripted"
    model = "scripted-1"

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls = 0
        self.last_messages: list[Any] = []

    def available(self) -> bool:
        return True

    def chat(self, messages, **kwargs) -> LLMResponse:
        self.calls += 1
        self.last_messages = list(messages)
        if not self._script:
            return LLMResponse(
                text=json.dumps({"kind": "final", "answer": "（脚本用尽）"},
                                ensure_ascii=False),
                model=self.model, latency_ms=1)
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, LLMResponse):
            return item
        text = item if isinstance(item, str) else json.dumps(
            item, ensure_ascii=False)
        return LLMResponse(text=text, model=self.model, latency_ms=1)


class StubTool:
    """按顺序返回结果；脚本项是 Exception 时抛出，模拟工具内部异常。"""

    def __init__(self, name: str = "query.read",
                 results: list[Any] | None = None,
                 kind: str = "read") -> None:
        self.spec = ToolSpec(
            name=name, kind=kind, description=f"替身工具 {name}",
            params={"type": "object", "properties": {}},
            requires=("source.read",),
        )
        self._results: list[Any] = list(
            results or [ToolResult(ok=True, data="ok")])
        self.calls = 0

    def run(self, args, ctx) -> ToolResult:
        self.calls += 1
        item = self._results.pop(0) if len(self._results) > 1 else self._results[0]
        if isinstance(item, BaseException):
            raise item
        return item


def _rows(n: int = 1) -> ToolResult:
    return ToolResult(ok=True,
                      data={"columns": ["x"], "rows": [[i] for i in range(n)]})


def _registry(*tools: Any) -> ToolRegistry:
    reg = ToolRegistry(granted=(Capability(name="source.read"),), origin="core")
    for t in tools or (StubTool(),):
        reg.register(t)
    return reg


def _core(provider: Any, *, registry: ToolRegistry | None = None,
          **cfg_kwargs: Any) -> AgentCore:
    events: list[dict] = []

    def _on_event(e: dict) -> None:
        events.append(dict(e))

    core = AgentCore(
        provider=provider,
        registry=registry or _registry(),
        config=RunConfig(**cfg_kwargs),
        capabilities=(Capability(name="source.read"),),
        on_event=_on_event,
    )
    core._test_events = events  # type: ignore[attr-defined] - 测试记录用
    return core


DECISION_TOOL = {"kind": "tool", "tool": "query.read",
                 "args": {"sql": "SELECT 1"}, "thought": "查一下"}
DECISION_FINAL = {"kind": "final", "answer": "查到了，是 1。", "thought": "够了"}
HISTORY = ["用户问题: 查一下"]

FAST = {"llm_retry_initial_delay": 0.0, "tool_retry_initial_delay": 0.0}


def _ask(core: AgentCore, *, trace: dict | None = None,
         notes: list | None = None) -> Any:
    return core._ask_model(
        list(HISTORY), trace={} if trace is None else trace,
        notes=[] if notes is None else notes)


# ===================================================================== 1) 策略
def test_policy() -> None:
    print("\n[1] 重试策略对象：退避、上限、抖动")
    p = RetryPolicy(jitter=False)
    check((p.max_retries, p.initial_delay, p.backoff_factor, p.max_delay)
          == (2, 0.5, 2.0, 8.0),
          "默认值保守（2 次、0.5s 起、×2 退避、8s 封顶）", str(p))
    delays = [p.delay_for(i) for i in range(6)]
    check(delays == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0],
          "指数退避且封顶", str(delays))
    check(p.delay_for(0) <= p.delay_for(99),
          "次数越大等待越久（不反转）")

    j = RetryPolicy(jitter=True)
    vals = {j.delay_for(3) for _ in range(20)}
    check(len(vals) > 1, "抖动让等待不固定（避免惊群）", str(len(vals)))
    check(all(4.0 * 0.75 <= v <= 4.0 * 1.25 for v in vals),
          "抖动幅度在 ±25% 内", str(sorted(vals)[:3]))
    check(all(v >= 0 for v in vals), "抖动后不为负")

    for kw in ({"max_retries": -1}, {"initial_delay": -0.1},
               {"backoff_factor": -2}, {"max_delay": -1}):
        try:
            RetryPolicy(**kw)
            check(False, f"非法参数 {kw} 应报错")
        except ValueError:
            check(True, f"非法参数 {kw} 报 ValueError")


def test_classify() -> None:
    print("\n[2] 分类器：什么该重试、什么不该")
    check(is_retryable_model_error(ConnectionResetError("connection reset by peer")),
          "模型：连接重置 → 可重试")
    check(is_retryable_model_error(TimeoutError("timed out")),
          "模型：超时 → 可重试")
    check(is_retryable_model_error(LLMError("模型调用失败: rate limit exceeded (429)")),
          "模型：限流文字 → 可重试")
    check(not is_retryable_model_error(
              LLMError("模型调用失败: Error code: 401 - invalid api key")),
          "模型：Key 无效 → **不**重试（重试也没用）")
    check(not is_retryable_model_error(
              LLMError("模型调用失败: model not found")),
          "模型：模型名不存在 → 不重试")
    check(not is_retryable_model_error(ValueError("bad request")),
          "模型：坏请求 → 不重试")

    check(is_retryable_tool_error(sqlite3.OperationalError("database is locked")),
          "工具：库被锁 → 可重试")
    check(is_retryable_tool_error(ConnectionResetError("connection reset")),
          "工具：连接重置 → 可重试")
    check(not is_retryable_tool_error(sqlite3.OperationalError("no such column: x")),
          "工具：字段不存在 → **不**重试（确定性失败，回灌给模型改 SQL）")
    check(not is_retryable_tool_error(sqlite3.OperationalError("syntax error")),
          "工具：SQL 语法错 → 不重试")
    check(not is_retryable_tool_error(PermissionError("无权限")),
          "工具：权限问题 → 不重试")


# ===================================================================== 2) 循环
def test_retry_call() -> None:
    print("\n[3] retry_call：成功、重试、耗尽、不可重试")
    calls = [0]
    recorded: list[tuple[int, float]] = []

    def _fail_twice() -> str:
        calls[0] += 1
        if calls[0] <= 2:
            raise ConnectionResetError("connection reset")
        return "ok"

    out, retries = retry_call(
        _fail_twice,
        policy=RetryPolicy(max_retries=2, initial_delay=0.0, jitter=False),
        classify=is_retryable_model_error,
        on_retry=lambda exc, attempt, delay: recorded.append((attempt, delay)),
    )
    check(out == "ok" and retries == 2, "两次瞬态失败后成功，返回重试次数",
          f"out={out!r} retries={retries}")
    check(calls[0] == 3, "实际调用了 3 次", str(calls[0]))
    check([a for a, _ in recorded] == [1, 2], "on_retry 按次回调",
          str(recorded))

    calls[0] = 0

    def _always_reset() -> str:
        calls[0] += 1
        raise ConnectionResetError("connection reset")

    try:
        retry_call(_always_reset,
                   policy=RetryPolicy(max_retries=2, initial_delay=0.0,
                                      jitter=False),
                   classify=is_retryable_model_error)
        check(False, "一直失败应该抛异常")
    except ConnectionResetError as e:
        check("connection reset" in str(e), "耗尽后原样抛出最后一个错误", str(e))
    check(calls[0] == 3, "最多 1+max_retries 次", str(calls[0]))

    calls[0] = 0

    def _bad_request() -> str:
        calls[0] += 1
        raise ValueError("bad request")

    try:
        retry_call(_bad_request,
                   policy=RetryPolicy(max_retries=5, initial_delay=0.0),
                   classify=is_retryable_model_error)
        check(False, "不可重试错误应该直接抛")
    except ValueError:
        check(calls[0] == 1, "不可重试错误**只调一次**", str(calls[0]))


# ===================================================================== 3) 模型
def test_model_retry() -> None:
    print("\n[4] 模型调用重试：瞬态失败可恢复、留痕")
    provider = ScriptedProvider([
        ConnectionResetError("connection reset by peer"),
        TimeoutError("timed out"),
        DECISION_FINAL,
    ])
    core = _core(provider, **FAST)
    trace: dict[str, Any] = {}
    notes: list[str] = []
    decision = _ask(core, trace=trace, notes=notes)
    check(decision.kind == "final", "重试后正常拿到决策", decision.kind)
    check(provider.calls == 3, "两次失败 + 一次成功 = 3 次调用", str(provider.calls))
    check(trace.get("llm_retries") == 2, "trace 记了重试次数", str(trace))
    check(sum("重试" in n for n in notes) == 2, "notes 里能追溯每一次重试",
          str(notes))
    events = [e["event"] for e in core._test_events]  # type: ignore[attr-defined]
    check(events.count("model_retry") == 2, "发出 model_retry 事件（界面可见）",
          str(events))


def test_model_retry_exhausted() -> None:
    print("\n[5] 模型重试耗尽：降级成可读答复，不崩循环")
    provider = ScriptedProvider([ConnectionResetError("connection reset")] * 3)
    core = _core(provider, **FAST)
    notes: list[str] = []
    decision = _ask(core, notes=notes)
    check(decision.kind == "final", "耗尽后仍在决策层降级", decision.kind)
    check("模型调用失败" in decision.final_answer,
          "答复说明模型调用失败", decision.final_answer)
    check(provider.calls == 3, "总共 3 次调用（1+2 重试）", str(provider.calls))

    provider2 = ScriptedProvider([LLMError("模型调用失败: 401 invalid api key")])
    core2 = _core(provider2, **FAST)
    decision2 = _ask(core2)
    check(provider2.calls == 1, "不可重试错误不浪费重试", str(provider2.calls))
    check("invalid api key" in decision2.final_answer,
          "错误信息原样透出（不被吞掉）", decision2.final_answer)


def test_model_repair() -> None:
    print("\n[6] 决策修复：输出被截断 / JSON 坏掉时带提示重问")
    trunc = LLMResponse(text='{"kind": "final", "answer": "查到了',
                        model="m", finish_reason="length", latency_ms=1)
    provider = ScriptedProvider([trunc, DECISION_FINAL])
    core = _core(provider, **FAST)
    trace: dict[str, Any] = {}
    decision = _ask(core, trace=trace)
    check(decision.kind == "final" and "查到了" in decision.final_answer,
          "修复后拿到合法决策", decision.final_answer)
    check(provider.calls == 2, "只重问一次", str(provider.calls))
    check(trace.get("llm_repairs") == 1, "trace 记了修复次数", str(trace))
    events = [e["event"] for e in core._test_events]  # type: ignore[attr-defined]
    check("model_repair" in events, "发出 model_repair 事件", str(events))
    # 第二次请求的末尾应带上纠正提示，而不是原样重发
    tail = [m.content for m in provider.last_messages if m.role == "user"][-1]
    check("长度上限" in tail or "截断" in tail,
          "修复请求里带上了'被截断'的纠正提示", tail[:120])

    bad = LLMResponse(text='{"kind": "final", "answer": ', model="m",
                      finish_reason="stop", latency_ms=1)
    provider2 = ScriptedProvider([bad, DECISION_FINAL])
    core2 = _core(provider2, **FAST)
    trace2: dict[str, Any] = {}
    decision2 = _ask(core2, trace=trace2)
    check(decision2.kind == "final", "坏 JSON 也能修复", decision2.final_answer)
    check(trace2.get("llm_repairs") == 1, "坏 JSON 记一次修复", str(trace2))

    # 关掉修复：坏输出直接返回解析兜底，行为可配置
    provider3 = ScriptedProvider([bad])
    core3 = _core(provider3, llm_repair_attempts=0, **FAST)
    decision3 = _ask(core3)
    check("格式无法解析" in decision3.final_answer,
          "llm_repair_attempts=0 时不修复、按兜底返回", decision3.final_answer)
    check(provider3.calls == 1, "不修复就只调一次", str(provider3.calls))

    # 纯文本回答刻意不修（parse_decision 容忍它当 final）
    plain = LLMResponse(text="这个月一共卖出了 206 件。", model="m",
                        finish_reason="stop", latency_ms=1)
    provider4 = ScriptedProvider([plain])
    core4 = _core(provider4, **FAST)
    decision4 = _ask(core4)
    check(decision4.kind == "final" and "206 件" in decision4.final_answer,
          "纯文本回答直接当 final，不触发修复", decision4.final_answer)
    check(provider4.calls == 1, "纯文本只调一次", str(provider4.calls))


# ===================================================================== 4) 工具
def _ctx() -> ToolContext:
    return ToolContext(capabilities=(Capability(name="source.read"),),
                       session_id="s-1", source_id="shop", now=NOW,
                       question="各渠道金额")


def test_tool_retry() -> None:
    print("\n[7] 工具重试：只读+瞬态才重试，且留痕")
    tool = StubTool(results=[
        sqlite3.OperationalError("database is locked"),
        sqlite3.OperationalError("database is locked"),
        _rows(2),
    ])
    core = _core(ScriptedProvider([DECISION_FINAL]), registry=_registry(tool),
                 tool_max_retries=2, **FAST)
    ctx = _ctx()
    result = core.invoke("query.read", {"sql": "SELECT 1"}, ctx)
    check(result.ok, "锁等待后重试成功", result.error or "")
    check(tool.calls == 3, "工具被调用 3 次", str(tool.calls))
    tool_notes = [n for n in ctx.trace if n.get("step") == "tool"]
    check(tool_notes and tool_notes[-1].get("retries") == 2,
          "轨迹里留下重试次数（否则用户不知道慢在哪）", str(tool_notes))


def test_tool_no_retry() -> None:
    print("\n[8] 工具不重试的两种情形：写工具、确定性失败")
    writer = StubTool(name="db.write", kind="write", results=[
        sqlite3.OperationalError("database is locked")])
    core = _core(ScriptedProvider([DECISION_FINAL]), registry=_registry(writer),
                 **FAST)
    ctx = _ctx()
    result = core.invoke("db.write", {}, ctx)
    check(not result.ok, "写工具失败如实返回", result.error or "")
    check(writer.calls == 1, "★ 写工具**绝不重试**（重试=重复写）",
          str(writer.calls))

    bad = StubTool(results=[sqlite3.OperationalError("no such column: x")])
    core2 = _core(ScriptedProvider([DECISION_FINAL]), registry=_registry(bad),
                  **FAST)
    ctx2 = _ctx()
    result2 = core2.invoke("query.read", {"sql": "SELECT x"}, ctx2)
    check(not result2.ok, "确定性失败如实返回", result2.error or "")
    check(bad.calls == 1, "确定性失败不重试（错误回灌给模型改 SQL）",
          str(bad.calls))
    check("no such column" in (result2.error or ""),
          "错误原文保留（模型据此改 SQL）", result2.error or "")


# ===================================================================== 5) 上下文
def test_compress_prompt() -> None:
    print("\n[9] Prompt 历史压缩：旧观察压成一句、最近保留全文")
    entries = ["=== 之前的对话 ===", "用户: 早"]
    for i in range(6):
        entries.append(f"观察: 第{i}次查询 " + "数" * 2000)
        entries.append(f"推理: 第{i}步")
    entries.append("用户问题: 现在几点了")
    compressed = compress_prompt_history(
        entries, max_chars=3000, keep_observations=2, old_observation_cap=100)
    text = "\n".join(compressed)
    original = "\n".join(entries)
    check(len(text) < len(original), "总长度显著变小",
          f"{len(original)} → {len(text)}")
    check(len(text) <= 3000 + 400, "压到预算附近（允许结构行少量超出）",
          str(len(text)))
    recent = [e for e in compressed if e.startswith("观察: ")]
    check(len(recent[-1]) > 1000,
          "最近一条观察保留全文（模型需要它接着推理）",
          str([len(e) for e in recent]))
    check(all(len(e) <= 130 for e in recent[:-1]),
          "更早的观察压成一句摘要", str([len(e) for e in recent]))

    # 结构行（=== xxx ===）不参与逐字符截断
    tiny = compress_prompt_history(
        ["=== 之前的对话 ==="] + ["x" * 500] * 5 + ["用户问题: q"],
        max_chars=200, keep_observations=0, old_observation_cap=10,
        hard_cap=10)
    check("=== 之前的对话 ===" in tiny, "硬截断时结构行保持完整",
          str([e[:24] for e in tiny]))
    check(any(e == _OMIT_MARK for e in tiny),
          "丢弃的历史有明确省略标记", str([e[:24] for e in tiny]))


def test_compress_session() -> None:
    print("\n[10] 会话上下文压缩：保留最近行、超长行截断")
    text = "\n".join(f"第{i}行 " + "a" * 1000 for i in range(8))
    out = compress_session_context(text, max_chars=2500, line_cap=500)
    check(len(out) <= 2500 + 60, "总长度守预算", str(len(out)))
    check(out.startswith(_OMIT_MARK), "被省略的部分有明确标记", out[:20])
    kept = [ln for ln in out.split("\n") if ln.startswith("第")]
    check(kept and kept[-1].startswith("第7行"),
          "**最近的**内容优先保留（模型靠它接上下文）", str(kept[:1]))
    check(all(len(ln) <= 520 for ln in kept), "单行超长被截断",
          str([len(ln) for ln in kept]))
    check(_OMIT_MARK in out or len(text) <= 2500,
          "没超预算时不加省略标记",
          f"text={len(text)} out={len(out)}")


def test_begin_budget() -> None:
    print("\n[11] 跨轮会话渲染也走预算（持久化完整、Prompt 有界）")
    session = SessionState(session_id="s-1", source_id="shop", now=NOW)
    from nl2sql.contracts.session import Turn
    session.add(Turn(role="user", content="最早的原始问题"))
    for i in range(10):
        session.add(Turn(role="tool",
                         content=f"查询{i}: " + '{"columns":["x"],"rows":[...]}'
                         * 500))
    core = _core(ScriptedProvider([DECISION_FINAL]),
                 session_context_chars=1200)
    state, _rctx = core.begin("追问一下", session=session)
    history_text = "\n".join(state["history"])
    check("=== 之前的对话 ===" in history_text, "历史结构保留")
    check(len(history_text) < 6000,
          "★ 渲染长度有界（不把持久化的完整输出全塞给模型）",
          str(len(history_text)))
    check(len(session.turns) >= 11,
          "session 里仍是完整历史（压缩只发生在渲染层）",
          str(len(session.turns)))


# ===================================================================== 6) 端到端
def test_runtime_end_to_end() -> None:
    print("\n[12] 执行器端到端：一次模型抖动不影响整轮答案")
    provider = ScriptedProvider([
        ConnectionResetError("connection reset by peer"),
        DECISION_TOOL,
        DECISION_FINAL,
    ])
    events: list[dict] = []
    rt, note = make_runtime(
        "loop", provider=provider, registry=_registry(StubTool(results=[_rows(2)])),
        config=RunConfig(max_steps=4, llm_retry_initial_delay=0.0),
    )
    session = SessionState(session_id="s-e2e", source_id="shop", now=NOW)
    outcome = rt.run("各渠道金额", session=session,
                     on_event=lambda e: events.append(dict(e)))
    check(outcome.status == "ok", "最终仍拿到 ok", f"{outcome.status} {note}")
    check(provider.calls == 3, "模型调用共 3 次（1 次重试）", str(provider.calls))
    check(int(outcome.trace.get("llm_retries") or 0) == 1,
          "outcome.trace 保留了重试记录（可观测）", str(outcome.trace))
    check(any(e.get("event") == "model_retry" for e in events),
          "SSE 流里能看到重试事件", str([e.get("event") for e in events[-6:]]))


def main() -> int:
    print("容错与上下文预算验收（模型/工具重试 + 决策修复 + Prompt 压缩）")
    try:
        test_policy()
        test_classify()
        test_retry_call()
        test_model_retry()
        test_model_retry_exhausted()
        test_model_repair()
        test_tool_retry()
        test_tool_no_retry()
        test_compress_prompt()
        test_compress_session()
        test_begin_budget()
        test_runtime_end_to_end()
    except Exception:
        import traceback
        traceback.print_exc()
        _failures.append("未捕获异常")
    print("\n" + "-" * 68)
    if _failures:
        print(f"失败 {len(_failures)} 项 / 共 {_checks} 项")
        for f in _failures:
            print("  -", f)
        return 1
    print(f"全部通过（{_checks} 项断言）：容错与上下文预算")
    return 0


if __name__ == "__main__":
    sys.exit(main())
