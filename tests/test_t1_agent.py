"""T1–T6 验收：工具契约 / 能力白名单 / 插件加载 / agent 循环 / 澄清 / 会话。

这是"把 LLM 升级为智能体"的第一步验收。核心要证明的是**循环真的存在**：
    工具结果回到模型眼前 → 模型据此决定下一步

因此测试里用 `ScriptedLLM`（按脚本逐步返回决策）而不是真模型：
这样才能断言"第 2 步是基于第 1 步的失败信息做出的"，
而真模型的行为不可复现、无法验证因果。

运行:
    python tests/test_t1_agent.py
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

from nl2sql.agent.builtin import BuiltinDeps, build_builtin_tools  # noqa: E402
from nl2sql.agent.runtime import AgentRuntime, RunConfig, parse_decision  # noqa: E402
from nl2sql.contracts.llm import ChatMessage, LLMResponse  # noqa: E402
from nl2sql.contracts.session import SessionState  # noqa: E402
from nl2sql.contracts.tool import (  # noqa: E402
    CAPABILITIES, Capability, ToolContext, ToolResult, ToolSpec,
)
from nl2sql.plugins import ToolRegistry  # noqa: E402
from nl2sql.plugins.loader import PluginLoader  # noqa: E402

_failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        _failures.append(f"{label} {detail}")


# ===================================================================== 测试替身
class ScriptedLLM:
    """按脚本逐步返回决策的模型替身。

    它的价值在于**可断言因果**：能验证"第 N 步的决策基于第 N-1 步的观察"。
    真模型做不到这一点（同一输入可能给不同输出）。
    """

    name = "scripted"
    model = "scripted-1"

    def __init__(self, script: list[dict | str]) -> None:
        self._script = list(script)
        self.prompts: list[str] = []
        self.model = "scripted-1"

    def available(self) -> bool:
        return True

    def chat(self, messages, **kwargs) -> LLMResponse:
        user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        self.prompts.append(user)
        if not self._script:
            return LLMResponse(text=json.dumps(
                {"kind": "final", "answer": "（脚本用尽）"}))
        item = self._script.pop(0)
        text = item if isinstance(item, str) else json.dumps(
            item, ensure_ascii=False)
        return LLMResponse(text=text, model=self.model, latency_ms=1)


class StubTool:
    """可编程替身工具，用于验证运行时行为而不牵扯真实依赖。"""

    def __init__(self, name: str, results: list[ToolResult] | None = None,
                 requires: tuple[str, ...] = (), kind: str = "read") -> None:
        self.spec = ToolSpec(name=name, description=f"替身工具 {name}",
                             params={"type": "object", "properties": {}},
                             requires=requires, kind=kind)  # type: ignore[arg-type]
        self._results = list(results or [ToolResult(ok=True, data="ok")])
        self.calls: list[dict] = []

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        self.calls.append(dict(args))
        if len(self._results) == 1:
            return self._results[0]
        return self._results.pop(0)


# ===================================================================== T1
def test_contracts() -> None:
    print("\n[T1] 工具契约与能力清单")
    check(len(CAPABILITIES) >= 10, "能力清单已定义", str(len(CAPABILITIES)))
    check("source.write" in CAPABILITIES, "写能力存在且单列（便于审批）")
    check("clarify.ask" in CAPABILITIES, "澄清能力存在")

    try:
        Capability(name="nonexistent.cap")
        check(False, "未知能力应被拒绝")
    except ValueError as e:
        check("未知能力" in str(e), "未知能力被拒绝", str(e)[:60])

    spec = ToolSpec(
        name="t.demo", description="演示",
        params={"type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"]},
    )
    schema = spec.to_json_schema()
    check(schema["name"] == "t.demo" and "x" in schema["parameters"]["properties"],
          "ToolSpec 可转 function-calling schema")
    check(schema["parameters"]["required"] == ["x"], "required 透传")

    # ToolResult 的观察文本要能表达失败 + 可行动建议
    r = ToolResult(ok=False, error="字段不存在: region",
                   hint="候选: 地区", candidates=["地区", "大区"])
    obs = r.to_observation()
    check("[失败]" in obs and "建议" in obs and "候选" in obs,
          "失败观察文本含错误/建议/候选", obs[:80])

    r2 = ToolResult(ok=True, data={"columns": ["a"], "rows": [[1], [2]],
                                   "row_count": 2})
    obs2 = r2.to_observation()
    check("列" in obs2 and "行数" in obs2, "表格结果被压成紧凑观察", obs2[:60])

    # ToolContext 的权限闸门
    ctx = ToolContext(capabilities=(Capability("source.read"),))
    check(ctx.has("source.read"), "能力已授予")
    check(not ctx.has("source.write"), "未授予的能力不可见")
    try:
        ctx.get("adapter", "source.write")
        check(False, "越权取用应被拒")
    except PermissionError as e:
        check("缺少能力" in str(e), "越权取用被拒", str(e)[:60])
    ctx.provide("adapter", "FAKE")
    check(ctx.get("adapter", "source.read") == "FAKE", "授权后可取用资源")

    # ---- 决策解析的健壮性（真实模型会给出畸形输出）
    print("\n  决策解析：畸形输出不应导致糟糕体验")
    d = parse_decision(json.dumps(
        {"kind": "final", "thought": "语义层中实收金额对应 net_revenue",
         "answer": "实收金额是 99998.5 元"}, ensure_ascii=False))
    check(d.kind == "final" and d.final_answer == "实收金额是 99998.5 元",
          "正常 JSON 解析")

    # 被 max_tokens 截断：JSON 不闭合
    d = parse_decision('{"kind":"final","thought":"推理很长","answer":"截断了的答案')
    check(d.kind == "final" and d.final_answer == "截断了的答案",
          "★ 截断的 JSON 能抢救出 answer（而不是把原文丢给用户）",
          str(d.final_answer))

    # JSON 前后有多余解释
    d = parse_decision('我先说明一下：{"kind":"final","answer":"答案在这里"}')
    check(d.final_answer == "答案在这里", "★ 带前后噪声也能解析",
          str(d.final_answer))

    # 是 JSON 但缺 answer：不应把 JSON 原文当回答
    d = parse_decision('{"kind":"final","thought":"只有思考没有答案"}')
    check("{" not in d.final_answer,
          "★ 缺 answer 时不把 JSON 原文当回答", str(d.final_answer))

    # 完全不是 JSON：当作纯文本回答
    d = parse_decision("数据显示本月销售额为 50322 元")
    check(d.final_answer == "数据显示本月销售额为 50322 元", "纯文本原样返回")

    print("\n  观察文本：结果形状由插件决定，不能假设")
    for value, label in [
        ({"columns": ["a"], "rows": 5}, "rows 不是列表"),
        ({"columns": ["a"], "rows": [[1], [2]]}, "正常表格"),
        (5, "整数"),
        ("文本", "字符串"),
        (None, "None"),
        ({"nested": {"deep": [1, 2, 3]}}, "嵌套字典"),
    ]:
        try:
            obs = ToolResult(ok=True, data=value).to_observation()
            check(bool(obs), f"形状「{label}」不崩", obs[:40])
        except Exception as e:  # noqa: BLE001
            check(False, f"形状「{label}」不崩", f"{type(e).__name__}: {e}")


# ===================================================================== T2
def test_capability_gate() -> None:
    print("\n[T2] 能力白名单：注册期即校验")
    reg = ToolRegistry(granted=(Capability("source.read"),))

    reg.register(StubTool("read.tool", requires=("source.read",)))
    check(reg.has("read.tool"), "能力匹配的工具可注册")

    try:
        reg.register(StubTool("write.tool", requires=("source.write",)))
        check(False, "需要未授权能力的工具应被拒绝")
    except PermissionError as e:
        check("未授予" in str(e), "★ 需要未授权能力的工具被拒绝", str(e)[:70])

    try:
        reg.register(StubTool("read.tool"))
        check(False, "重名工具应被拒绝")
    except ValueError as e:
        check("同名" in str(e), "重名工具被拒绝")

    # 显式追加授权后可通过
    reg.grant("source.write", granted_by="test")
    reg.register(StubTool("write.tool", requires=("source.write",)))
    check(reg.has("write.tool"), "追加授权后可注册")

    check(reg.origin_of("read.tool") == "core", "记录工具来源（便于审计）")


# ===================================================================== T3
def test_plugin_loader(tmp: str) -> None:
    print("\n[T3] 插件加载：发现 / 声明 / 隔离")

    plugins_dir = os.path.join(tmp, "plugins")
    good = os.path.join(plugins_dir, "hello")
    bad_cap = os.path.join(plugins_dir, "overreach")
    bad_import = os.path.join(plugins_dir, "sneaky")
    broken = os.path.join(plugins_dir, "broken")
    for d in (good, bad_cap, bad_import, broken):
        os.makedirs(d, exist_ok=True)

    # (1) 正常插件
    with open(os.path.join(good, "plugin.toml"), "w", encoding="utf-8") as f:
        f.write("""[plugin]
name = "hello"
version = "1.0.0"
description = "示例插件"
entry = "plugin.py"
requires = ["semantic.read"]
tools = ["demo.hello"]
""")
    with open(os.path.join(good, "plugin.py"), "w", encoding="utf-8") as f:
        f.write('''
from nl2sql.plugins import ToolResult, ToolSpec


class HelloTool:
    spec = ToolSpec(
        name="demo.hello",
        description="打个招呼，演示插件怎么写",
        params={"type": "object", "properties": {"who": {"type": "string"}},
                "required": []},
        requires=("semantic.read",),
        kind="none",
    )

    def run(self, args, ctx):
        return ToolResult(ok=True, data=f"hello {args.get('who', 'world')}")


def register():
    return [HelloTool()]
''')

    # (2) 越权插件：工具需要的能力未在 plugin.toml 声明
    with open(os.path.join(bad_cap, "plugin.toml"), "w", encoding="utf-8") as f:
        f.write("""[plugin]
name = "overreach"
entry = "plugin.py"
requires = ["semantic.read"]
""")
    with open(os.path.join(bad_cap, "plugin.py"), "w", encoding="utf-8") as f:
        f.write('''
from nl2sql.plugins import ToolResult, ToolSpec


class SneakyWrite:
    spec = ToolSpec(name="sneaky.write", description="越权写",
                    requires=("source.write",), kind="write")

    def run(self, args, ctx):
        return ToolResult(ok=True)


def register():
    return [SneakyWrite()]
''')

    # (3) 违规导入：插件 import 核心私有模块
    with open(os.path.join(bad_import, "plugin.toml"), "w", encoding="utf-8") as f:
        f.write("""[plugin]
name = "sneaky"
entry = "plugin.py"
requires = ["semantic.read"]
""")
    with open(os.path.join(bad_import, "plugin.py"), "w", encoding="utf-8") as f:
        f.write(
            "from nl2sql.sources.sqlite import SqliteAdapter\n"
            "from nl2sql.plugins import ToolResult, ToolSpec\n\n\n"
            "class T:\n"
            "    spec = ToolSpec(name='sneaky.t', description='x')\n"
            "    def run(self, args, ctx):\n"
            "        return ToolResult(ok=True)\n\n\n"
            "def register():\n"
            "    return [T()]\n"
        )

    # (4) 坏插件：register 抛异常
    with open(os.path.join(broken, "plugin.toml"), "w", encoding="utf-8") as f:
        f.write('[plugin]\nname = "broken"\nentry = "plugin.py"\n')
    with open(os.path.join(broken, "plugin.py"), "w", encoding="utf-8") as f:
        f.write("def register():\n    raise RuntimeError('故意炸掉')\n")

    loader = PluginLoader()
    manifests = loader.discover(plugins_dir)
    names = sorted(m.name for m in manifests)
    check(names == ["broken", "hello", "overreach", "sneaky"],
          "发现 4 个插件", str(names))

    loaded = loader.load_all(plugins_dir)
    ok_names = sorted(p.manifest.name for p in loaded)
    check(ok_names == ["hello", "sneaky"],
          "正常插件与违规导入插件可加载（导入违规由层检查拦）", str(ok_names))
    check(any("broken" in e for e in loader.errors), "坏插件被记录而不中断加载",
          str(loader.errors))
    check(any("overreach" in e and "声明" in e for e in loader.errors),
          "★ 越权插件被拒绝：能力必须显式声明", str(loader.errors))

    hello = next(p for p in loaded if p.manifest.name == "hello")
    check(len(hello.tools) == 1 and hello.tools[0].spec.name == "demo.hello",
          "插件工具已提取")
    check([c.name for c in hello.capabilities] == ["semantic.read"],
          "按声明授予能力")

    # 工具能通过注册表跑起来
    reg = ToolRegistry(granted=tuple(hello.capabilities))
    for t in hello.tools:
        reg.register(t, origin="plugin:hello")
    ctx = ToolContext(capabilities=tuple(hello.capabilities))
    res = reg.get("demo.hello").run({"who": "agent"}, ctx)
    check(res.ok and res.data == "hello agent", "插件工具可调用",
          str(res.data))
    check(reg.origin_of("demo.hello") == "plugin:hello", "来源标注为插件")


# ===================================================================== T4
def test_agent_loop() -> None:
    print("\n[T4] agent 循环：观测回灌与自我纠错")

    # ---- 场景 A：失败 → 看到候选 → 换参数重试 → 成功
    flaky = StubTool("sql.exec", results=[
        ToolResult(ok=False, error="no such column: region",
                   hint="候选: 地区", candidates=["地区"]),
        ToolResult(ok=True, data="3 行"),
    ])
    reg = ToolRegistry()
    reg.register(flaky)

    llm = ScriptedLLM([
        {"kind": "tool", "tool": "sql.exec", "args": {"sql": "SELECT region"}},
        {"kind": "tool", "tool": "sql.exec", "args": {"sql": "SELECT 地区"}},
        {"kind": "final", "answer": "共 3 行"},
    ])
    rt = AgentRuntime(provider=llm, registry=reg,
                      config=RunConfig(max_steps=6))
    session = SessionState(session_id="s1", source_id="shop")
    out = rt.run("查一下地区", session=session)

    check(out.status == "ok", "循环正常结束", f"{out.status} {out.message}")
    check(out.message == "共 3 行", "最终回答正确", str(out.message))
    check(len(flaky.calls) == 2, "工具被调用两次（失败后重试）",
          str(flaky.calls))
    check(flaky.calls[1] != flaky.calls[0], "★ 第二次调用换了参数（不是盲重试）")

    # ---- 关键断言：失败信息真的回灌给模型了
    second_prompt = llm.prompts[1]
    check("no such column" in second_prompt,
          "★ 第 2 步的 Prompt 里含第 1 步的失败信息（观测回灌）",
          second_prompt[-300:])
    check("候选" in second_prompt, "★ 候选字段也回灌了（可行动信息）")

    # ---- 场景 B：模型请求不存在的工具
    llm2 = ScriptedLLM([
        {"kind": "tool", "tool": "no.such.tool", "args": {}},
        {"kind": "final", "answer": "改好了"},
    ])
    rt2 = AgentRuntime(provider=llm2, registry=reg, config=RunConfig())
    out2 = rt2.run("x", session=SessionState(session_id="s2"))
    check(out2.status == "ok", "不存在的工具不致命",
          f"{out2.status} {out2.message}")
    check("工具不存在" in llm2.prompts[1] and "可用工具" in llm2.prompts[1],
          "★ 把可用工具列表告诉模型，便于它改正", llm2.prompts[1][-200:])

    # ---- 场景 C：打转检测
    loop_tool = StubTool("loop.tool")
    reg3 = ToolRegistry()
    reg3.register(loop_tool)
    llm3 = ScriptedLLM([{"kind": "tool", "tool": "loop.tool", "args": {"a": 1}}] * 6)
    rt3 = AgentRuntime(provider=llm3, registry=reg3,
                       config=RunConfig(max_steps=10, max_repeat=2))
    out3 = rt3.run("x", session=SessionState(session_id="s3"))
    check(out3.status == "unresolved", "打转被检测并停止", out3.status)
    check(any("重复调用" in n for n in out3.notes), "提示了打转原因",
          str(out3.notes))
    check(len(loop_tool.calls) <= 3, "★ 没有无限调用（成本保护）",
          f"实际 {len(loop_tool.calls)} 次")

    # ---- 场景 D：步数上限
    llm4 = ScriptedLLM([{"kind": "tool", "tool": "loop.tool", "args": {"a": 2}}] * 10)
    rt4 = AgentRuntime(provider=llm4, registry=reg3,
                       config=RunConfig(max_steps=3, max_repeat=99))
    out4 = rt4.run("x", session=SessionState(session_id="s4"))
    check(out4.status == "unresolved", "达到步数上限后停止", out4.status)
    check(any("步数上限" in n for n in out4.notes), "提示了步数上限",
          str(out4.notes))

    # ---- 场景 E：工具内部异常被收敛，不打断循环
    class Boom:
        spec = ToolSpec(name="boom", description="抛异常的工具")

        def run(self, args, ctx):
            raise RuntimeError("内部炸了")

    reg5 = ToolRegistry()
    reg5.register(Boom())
    llm5 = ScriptedLLM([
        {"kind": "tool", "tool": "boom", "args": {}},
        {"kind": "final", "answer": "我看到工具炸了，换条路"},
    ])
    rt5 = AgentRuntime(provider=llm5, registry=reg5, config=RunConfig())
    out5 = rt5.run("x", session=SessionState(session_id="s5"))
    check(out5.status == "ok", "★ 工具异常被收敛成观察，循环继续", out5.status)
    check("RuntimeError" in llm5.prompts[1],
          "★ 异常信息回灌给模型（而不是吞掉）", llm5.prompts[1][-200:])

    # ---- 场景 F：模型不可用
    class NoLLM:
        name = "none"
        model = ""

        def available(self):
            return False

        def chat(self, *a, **k):
            raise RuntimeError("不应该被调用")

    rt6 = AgentRuntime(provider=NoLLM(), registry=reg, config=RunConfig())
    out6 = rt6.run("x", session=SessionState(session_id="s6"))
    check(out6.status == "error" and out6.error_code == "LLM_UNAVAILABLE",
          "模型不可用时明确报错", f"{out6.status} {out6.error_code}")


# ===================================================================== T5
def test_clarify(work: str) -> None:
    print("\n[T5] 澄清：回合制 + 会话持久化")

    # ---- 澄清工具本身
    deps = BuiltinDeps()
    tools = {t.spec.name: t for t in build_builtin_tools(deps)}
    check("clarify.ask" in tools, "澄清工具已注册")

    reg = ToolRegistry(granted=(Capability("clarify.ask"),))
    reg.register(tools["clarify.ask"])
    ctx = ToolContext(capabilities=(Capability("clarify.ask"),))
    res = reg.get("clarify.ask").run(
        {"question": "你要看哪个时间段？", "options": ["本月", "上月"]}, ctx)
    check(res.needs_clarification, "工具返回需要澄清")
    check(res.question == "你要看哪个时间段？", "问题透传")
    check(res.options == ["本月", "上月"], "选项透传")
    check("需要澄清" in res.to_observation(), "观察文本标明澄清")

    # ---- 循环在澄清时正确中断
    llm = ScriptedLLM([
        {"kind": "tool", "tool": "clarify.ask",
         "args": {"question": "哪个时间段？", "options": ["本月", "上月"]}},
        {"kind": "final", "answer": "不该走到这里"},
    ])
    rt = AgentRuntime(provider=llm, registry=reg, config=RunConfig())
    session = SessionState(session_id="c1", source_id="shop", now="2024-06-30")
    out = rt.run("销售额多少", session=session)
    check(out.status == "need_clarification", "状态为 need_clarification",
          out.status)
    check(out.message == "哪个时间段？", "问题作为 message 返回", str(out.message))
    check(out.candidates == ["本月", "上月"], "选项作为 candidates 返回")
    check(session.pending_question == "哪个时间段？", "会话记录待回答问题")
    check(len(llm.prompts) == 1, "★ 澄清后立即停止，不再调用模型",
          f"实际 {len(llm.prompts)} 次")

    # ---- 会话持久化：跨"进程"（新 store 实例）可恢复
    from nl2sql.storage.sqlite_session import SqliteSessionStore

    db = os.path.join(work, "sessions.db")
    store = SqliteSessionStore(db)
    store.save(session)
    store.close()

    store2 = SqliteSessionStore(db)
    restored = store2.load("c1")
    check(restored is not None, "会话可恢复")
    check(restored is not None and restored.now == "2024-06-30",
          "★ 时间锚点随会话保留（否则用户回答后「本月」会变成另一个月）",
          str(restored.now if restored else None))
    check(restored is not None and restored.pending_question == "哪个时间段？",
          "待回答问题保留")
    check(restored is not None and any(
        t.role == "tool" and t.tool_name == "clarify.ask"
        for t in restored.turns), "工具调用轨迹保留（可追溯）")

    # ---- 第二回合：带上历史继续
    session2 = restored
    assert session2 is not None
    session2.pending_question = ""
    llm2 = ScriptedLLM([{"kind": "final", "answer": "本月销售额 50322 元"}])
    rt2 = AgentRuntime(provider=llm2, registry=reg, config=RunConfig())
    out2 = rt2.run("本月", session=session2)
    check(out2.status == "ok", "第二回合正常完成", out2.status)
    check("销售额多少" in llm2.prompts[0],
          "★ 第二回合的 Prompt 含第一回合的问题（上下文接上）",
          llm2.prompts[0][:200])
    check(len(session2.turns) >= 4, "会话轮次累积",
          f"实际 {len(session2.turns)}")

    store2.save(session2)
    recent = store2.list_recent(limit=5)
    check(any(s.session_id == "c1" for s in recent), "会话可列出")
    store2.delete("c1")
    check(store2.load("c1") is None, "会话可删除")
    store2.close()


# ===================================================================== T6
def test_builtin_tools(work: str) -> None:
    print("\n[T6] 内置工具（用真实数据源）")

    from nl2sql.catalog import CatalogService
    from nl2sql.config import NL2SQLConfig
    from nl2sql.dialect import SqlCompiler, SqlValidator
    from nl2sql.guard import Guard
    from nl2sql.knowledge import KnowledgeService
    from nl2sql.llm import RulePlanner
    from nl2sql.semantics.golden_model import build_model
    from nl2sql.sources.sqlite import SqliteAdapter
    from nl2sql.storage import build_storage

    db = os.path.join(work, "tools.db")
    shutil.copy2(GOLDEN_DB, db)
    cfg = NL2SQLConfig(state_dir=os.path.join(work, "state_tools"))
    cfg.add_sqlite(db, source_id="shop")
    cfg.validate()
    bundle = build_storage(cfg)
    adapter = SqliteAdapter(db)
    model = build_model(order_level=True)
    catalog = CatalogService(bundle.meta, {"shop": adapter})
    catalog.refresh("shop")
    knowledge = KnowledgeService(bundle.knowledge, vector_store=bundle.vector)

    deps = BuiltinDeps(
        semantic_model=model, compiler=SqlCompiler(adapter.capabilities),
        guard=Guard(adapter.capabilities, max_rows=1000, timeout_s=10),
        adapter=adapter, catalog=catalog, knowledge=knowledge,
        planner=RulePlanner(model),
    )
    caps = tuple(Capability(c) for c in (
        "semantic.read", "source.read", "knowledge.read"))
    reg = ToolRegistry(granted=caps)
    for t in build_builtin_tools(deps):
        if all(r in {c.name for c in caps} for r in t.spec.requires):
            reg.register(t)
    ctx = ToolContext(capabilities=caps, source_id="shop", now="2024-06-30",
                      question="2024年6月，数码品类的实收金额是多少？")
    for k, v in {"semantic_model": model, "compiler": deps.compiler,
                 "guard": deps.guard, "adapter": adapter, "catalog": catalog,
                 "knowledge": knowledge, "planner": deps.planner,
                 "max_rows": 1000}.items():
        ctx.provide(k, v)

    # semantic.list_metrics
    r = reg.get("semantic.list_metrics").run({}, ctx)
    check(r.ok and r.data["metrics"], "list_metrics 返回指标清单")
    check(any(m["name"] == "net_revenue" for m in r.data["metrics"]),
          "含 net_revenue")
    check(len(r.data["dimensions"]) >= 5, "含维度清单",
          str(len(r.data["dimensions"])))

    # semantic.describe
    r = reg.get("semantic.describe").run({"name": "net_revenue"}, ctx)
    check(r.ok and "已支付" in r.data["expr"], "describe 返回口径表达式",
          str(r.data.get("expr"))[:60])
    r = reg.get("semantic.describe").run({"name": "不存在的指标"}, ctx)
    check(not r.ok and r.candidates, "★ 指标不存在时给候选",
          str(r.candidates))

    # schema.describe（含取值采样——纠错的关键能力）
    r = reg.get("schema.describe").run({"table": "fact_order"}, ctx)
    check(r.ok and r.data["table"] == "fact_order", "describe 表结构")
    r = reg.get("schema.describe").run(
        {"table": "fact_order", "with_values": True}, ctx)
    check(r.ok, "可采样取值")
    samples = r.data.get("value_samples", {}) if isinstance(r.data, dict) else {}
    check("order_status" in samples,
          "★ 采样到 order_status 的取值（模型猜错取值时靠它纠正）",
          str(list(samples))[:80])
    check(samples.get("order_status") and "已支付" in samples["order_status"],
          "取值内容正确", str(samples.get("order_status")))
    r = reg.get("schema.describe").run({"table": "no_such"}, ctx)
    check(not r.ok and r.candidates, "表不存在时给候选")

    # knowledge.search
    r = reg.get("knowledge.search").run({"query": "fact_order 字段"}, ctx)
    check(r.ok, "知识检索可用")

    # sql.plan / sql.compile / sql.execute
    r = reg.get("sql.plan").run({"question": "2024年6月数码品类实收金额"}, ctx)
    check(r.ok and r.data["measures"] == ["net_revenue"], "plan 产出逻辑计划",
          str(r.data.get("measures")))
    r = reg.get("sql.compile").run({"question": "2024年6月数码品类实收金额"}, ctx)
    check(r.ok and "SELECT" in r.data["sql"], "compile 产出 SQL")
    r = reg.get("sql.execute").run({"sql": "SELECT 1 AS x"}, ctx)
    check(r.ok and r.data["rows"] == [[1]], "execute 执行成功", str(r.data))
    r = reg.get("sql.execute").run({"sql": "DROP TABLE fact_order"}, ctx)
    check(not r.ok and "安全策略" in (r.error or ""),
          "★ execute 内部仍走 guard：写操作被拒", str(r.error))
    r = reg.get("sql.execute").run({"sql": "SELECT nope FROM fact_order"}, ctx)
    check(not r.ok and r.candidates,
          "★ SQL 报错时给出候选字段（agent 纠错的依据）",
          f"{r.error} | {r.candidates}")

    bundle.meta.close(); bundle.knowledge.close()
    bundle.cache.close(); bundle.sessions.close(); adapter.close()


def main() -> int:
    base = os.environ.get("NL2SQL_TEST_TMP") or os.path.join(ROOT, "data", "_test_t1")
    work = os.path.join(base, "run")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    print(f"工作目录: {work}")

    try:
        test_contracts()
        test_capability_gate()
        test_plugin_loader(work)
        test_agent_loop()
        test_clarify(work)
        test_builtin_tools(work)
    except Exception:
        traceback.print_exc()
        _failures.append("未捕获异常")

    shutil.rmtree(work, ignore_errors=True)
    print("\n" + "=" * 66)
    if _failures:
        print(f"T1–T6 验收未通过：{len(_failures)} 项失败")
        for x in _failures[:25]:
            print("  -", x)
        return 1
    print("T1–T6 验收全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
