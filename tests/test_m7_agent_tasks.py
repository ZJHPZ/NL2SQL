"""M7 验收之二：项目信息 + 任务工具 + 项目简报注入。

验收重点：
1. **条件注册**：没有任务库时不注册任何项目/任务工具——工具清单与 M7
   之前逐字相同（旧回归断言不被破坏）；有任务库时才出现。
2. **agent 真的能建/改任务**：task.create 写任务库、task.update 改 SQL 时
   自动追加 `-- ai修改，时间` 并生成新版本。
3. **项目简报注入 system prompt**：常驻「## 当前项目」小节，空项目时
   系统 Prompt 与 M7 之前逐字相同。
4. **边界**：任务 SQL 仍然只读；工具失败返回可行动信息而不是抛异常。

运行:
    python tests/test_m7_agent_tasks.py
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import traceback
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_DB = os.path.join(ROOT, "golden_set", "ecommerce.db")
_failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        _failures.append(f"{label} {detail}")


def section(t: str) -> None:
    print("\n" + "=" * 68)
    print(t)
    print("=" * 68)


def _ctx(caps: tuple[str, ...] = ("project.read", "task.read", "task.write"),
         **resources):
    from nl2sql.contracts.tool import Capability, ToolContext

    ctx = ToolContext(
        capabilities=tuple(Capability(name=c) for c in caps),
        session_id="s1", source_id="shop", now="2026-10-22",
        question="帮我创建一个任务",
    )
    for k, v in resources.items():
        ctx.provide(k, v)
    return ctx


# --------------------------------------------------------------------- A
def test_specs_and_conditional_registration(work: str) -> None:
    section("[A] 工具规格 + 条件注册")
    from nl2sql.agent.tool_tasks import (build_task_tools,
                                         task_tool_classes)
    from nl2sql.contracts.tool import CAPABILITIES

    names = [cls.spec.name for cls in task_tool_classes()]
    check(names == ["project.info", "task.list", "task.get",
                    "task.create", "task.update"],
          "五类项目/任务工具规格齐全", str(names))
    for cap in ("project.read", "task.read", "task.write"):
        check(cap in CAPABILITIES, f"能力清单含 {cap}")

    cls = {c.spec.name: c for c in task_tool_classes()}
    check(cls["project.info"].spec.requires == ("project.read",),
          "project.info 需要 project.read")
    check(cls["task.create"].spec.kind == "write",
          "task.create 是 write 类工具（写任务库）")
    check(cls["task.update"].spec.kind == "write",
          "task.update 是 write 类工具")
    check(cls["task.create"].spec.requires == ("task.write",),
          "task.create 需要 task.write")

    # 没有任务库 → 一个任务工具都不注册
    empty = build_task_tools(SimpleNamespace(project_info=None,
                                             task_store=None))
    check(empty == [], "无任务库时不注册任务工具")
    # 只有任务库 → 只有任务工具，没有 project.info
    store = _store(work, "a")
    only_tasks = build_task_tools(SimpleNamespace(project_info=None,
                                                  task_store=store))
    check([t.spec.name for t in only_tasks] ==
          ["task.list", "task.get", "task.create", "task.update"],
          "只有任务库时排除 project.info")
    # 两者都有 → 五个
    both = build_task_tools(SimpleNamespace(project_info=lambda: {"name": "p"},
                                            task_store=store))
    check(len(both) == 5, "项目+任务库齐备时注册 5 个工具")


def _store(work: str, name: str):
    from nl2sql.tasks import TaskStore

    return TaskStore(os.path.join(work, f"tasks_{name}.db"),
                     project_id="proj-a")


# --------------------------------------------------------------------- B
def test_tool_runs(work: str) -> None:
    section("[B] 工具实跑：project.info / create / list / get / update")
    from nl2sql.agent.tool_tasks import (build_task_tools, task_tool_classes)

    store = _store(work, "b")
    info = {
        "project_id": "proj-a", "name": "项目 A",
        "sources": [{"source_id": "shop", "adapter": "sqlite"}],
        "task_count": 0, "tasks_enabled": True, "tasks": [],
    }
    tools = {t.spec.name: t for t in build_task_tools(
        SimpleNamespace(project_info=lambda: dict(info), task_store=store))}
    ctx = _ctx(task_store=store, project_info=lambda: dict(info))

    # project.info
    r = tools["project.info"].run({}, ctx)
    check(r.ok and r.data["project_id"] == "proj-a", "project.info 返回项目信息")

    # 空任务库
    r = tools["task.list"].run({}, ctx)
    check(r.ok and r.data["total"] == 0, "task.list 空库 total=0")

    # 创建任务
    r = tools["task.create"].run({
        "name": "渠道实收",
        "sql": "SELECT channel_name, SUM(amount) AS gmv FROM fact_order GROUP BY 1",
        "description": "按渠道汇总实收",
        "tags": ["财务"],
    }, ctx)
    check(r.ok and r.data.get("task_id"), "task.create 成功", str(r.error))
    task_id = r.data["task_id"]
    check(store.count() == 1, "任务已写入任务库")
    task = store.get(task_id)
    check(task.created_by == "ai", "AI 创建的任务 author=ai")
    check("不会自动执行" in r.data["note"], "创建结果明确说明不自动执行")

    # 列表里能看到
    r = tools["task.list"].run({"limit": 10}, ctx)
    check(r.ok and r.data["total"] == 1 and
          r.data["tasks"][0]["task_id"] == task_id, "task.list 能看到新任务")

    # get 带版本历史
    r = tools["task.get"].run({"task_id": task_id}, ctx)
    check(r.ok and r.data["revision"] == 1, "task.get 版本 1")
    check(len(r.data["revisions"]) == 1 and
          r.data["revisions"][0]["author"] == "ai", "task.get 带 AI 版本历史")

    # 更新 SQL → 自动加 ai修改注释 + 新版本
    new_sql = ("SELECT channel_name, SUM(amount) AS gmv FROM fact_order "
               "WHERE order_date >= '2024-01-01' GROUP BY 1")
    r = tools["task.update"].run({
        "task_id": task_id, "sql": new_sql, "note": "增加时间过滤",
    }, ctx)
    check(r.ok and r.data["revision"] == 2, "task.update 生成版本 2",
          str(r.error))
    updated = store.get(task_id)
    check("-- ai修改，" in updated.sql, "★ 修改后的 SQL 自动带 ai修改注释")
    check("增加时间过滤" in updated.sql, "备注并入注释")
    check(updated.updated_by == "ai", "updated_by=ai")
    revs = store.revisions(task_id)
    check(len(revs) == 2 and revs[0].revision == 2, "版本历史 2 条")
    check("WHERE" not in store.get_revision(task_id, 1).sql,
          "旧版本仍是不带过滤的原 SQL")

    # 非法 SQL 被拒绝且不产生版本
    r = tools["task.update"].run({"task_id": task_id,
                                  "sql": "DELETE FROM fact_order"}, ctx)
    check(not r.ok and "只读" in (r.error or ""), "★ 写 SQL 被工具拒绝",
          str(r.error))
    check(store.get(task_id).revision == 2, "非法更新不产生版本")

    # 不存在的任务 → 失败 + candidates
    r = tools["task.get"].run({"task_id": "nope"}, ctx)
    check(not r.ok and "不存在" in (r.error or ""), "task.get 不存在任务失败")
    r = tools["task.update"].run({"task_id": task_id}, ctx)
    check(not r.ok and "没有可更新" in (r.error or ""), "空更新被拒绝")

    # 缺参数
    ctx_no_store = _ctx()
    r = tools["task.list"].run({}, ctx_no_store)
    check(not r.ok and "资源" in (r.error or ""),
          "无资源时给出明确装配错误（不抛异常）")


# --------------------------------------------------------------------- C
def test_project_brief_injection(work: str) -> None:
    section("[C] 项目简报注入 system prompt")
    from nl2sql.agent.machine import AgentCore
    from nl2sql.contracts.llm import LLMResponse
    from nl2sql.contracts.tool import ToolResult, ToolSpec
    from nl2sql.plugins import ToolRegistry

    class ScriptedProvider:
        name = "scripted"
        model = "scripted"

        def __init__(self) -> None:
            self.calls: list[list] = []

        def available(self) -> bool:
            return True

        def chat(self, messages, **kw):  # noqa: ANN001, ANN003
            self.calls.append(list(messages))
            return LLMResponse(text='{"kind":"final","answer":"ok","thought":"x"}')

    class EchoTool:
        spec = ToolSpec(name="echo", description="回显", params={
            "type": "object", "properties": {}}, kind="none")

        def run(self, args, ctx):  # noqa: ANN001
            return ToolResult(ok=True, data="ok")

    def _core(provider, project_brief: str = "", schema_brief: str = "") -> AgentCore:
        reg = ToolRegistry(granted=(), origin="core")
        reg.register(EchoTool())
        return AgentCore(provider=provider, registry=reg,
                         project_brief=project_brief,
                         schema_brief=schema_brief)

    # 空项目简报 → 不注入（旧行为逐字不变）
    p0 = ScriptedProvider()
    _core(p0)._ask_model(["用户: 你好"])  # noqa: SLF001
    system0 = p0.calls[-1][0].content
    check("## 当前项目" not in system0, "★ 无项目时不注入「当前项目」小节")

    # 有项目简报 → 注入小节 + 说明任务操作
    p1 = ScriptedProvider()
    brief = "- 项目：项目 A（id=proj-a）\n- 已有任务：2 条（渠道汇总、区域汇总）"
    _core(p1, project_brief=brief)._ask_model(["用户: 帮我建个任务"])  # noqa: SLF001
    system1 = p1.calls[-1][0].content
    check("## 当前项目" in system1, "★ 有项目时注入「当前项目」小节")
    check("项目 A" in system1 and "proj-a" in system1,
          "简报内容进入 system prompt")
    check("task.create" in system1 and "不自动执行" in system1,
          "注入内容说明任务操作边界")

    # 注入顺序：项目在库表事实之前
    p2 = ScriptedProvider()
    _core(p2, project_brief=brief, schema_brief="- fact_order（810 行）：id")._ask_model(
        ["用户: 查询"])  # noqa: SLF001
    system2 = p2.calls[-1][0].content
    i_proj = system2.find("## 当前项目")
    i_schema = system2.find("## 当前数据源的库表事实")
    check(i_proj != -1 and i_schema != -1 and i_proj < i_schema,
          "★ 项目小节在库表事实小节之前")

    # 两个执行器都透传
    from nl2sql.agent.runtime import AgentRuntime

    rt = AgentRuntime(provider=p1, registry=_core(p1).registry,
                      project_brief=brief)
    p3 = ScriptedProvider()
    rt._core._provider = p3  # noqa: SLF001
    rt.core._ask_model(["用户: 查一下"])  # noqa: SLF001
    check("## 当前项目" in p3.calls[-1][0].content,
          "loop 执行器透传 project_brief")
    rt.set_project_brief("- 项目：B（id=b）")
    p4 = ScriptedProvider()
    rt.core._provider = p4  # noqa: SLF001
    rt.core._ask_model(["用户: 查一下"])  # noqa: SLF001
    check("项目：B" in p4.calls[-1][0].content,
          "set_project_brief 热生效")


# --------------------------------------------------------------------- D
def test_engine_wiring(work: str) -> None:
    section("[D] 引擎装配：默认项目不出现任务工具，项目引擎出现")
    from nl2sql import NL2SQL, NL2SQLConfig

    # 默认项目（无任务库）：工具清单与 M7 之前一致
    cfg1 = NL2SQLConfig(state_dir=os.path.join(work, "state_default"))
    cfg1.add_sqlite(GOLDEN_DB, source_id="shop")
    cfg1.agent.planner = "rule"
    cfg1.skills.dirs = ()
    e1 = NL2SQL(cfg1).connect()
    names1 = [t.spec.name for t in e1.tools.all()]
    check("task.create" not in names1 and "project.info" not in names1,
          "★ 默认项目不注册项目/任务工具（旧工具清单不变）", str(names1))
    check(e1.task_store is None, "默认项目 task_store=None")
    check(e1._project_brief() == "", "默认项目项目简报为空串")

    # 项目引擎（有任务库 + 项目身份）
    cfg2 = NL2SQLConfig(state_dir=os.path.join(work, "state_proj"))
    cfg2.add_sqlite(GOLDEN_DB, source_id="shop")
    cfg2.agent.planner = "rule"
    cfg2.skills.dirs = ()
    cfg2.project_id = "proj-a"
    cfg2.project_name = "项目 A"
    cfg2.project_description = "测试项目"
    cfg2.task_store_path = os.path.join(work, "state_proj", "tasks.db")
    e2 = NL2SQL(cfg2).connect()
    names2 = [t.spec.name for t in e2.tools.all()]
    for name in ("project.info", "task.list", "task.get",
                 "task.create", "task.update"):
        check(name in names2, f"项目引擎注册 {name}")
    check(e2.task_store is not None, "项目引擎装配了任务库")
    check(e2._tool_deps.task_store is e2.task_store,
          "任务库注入 BuiltinDeps")

    brief = e2._project_brief()
    check("项目 A" in brief and "proj-a" in brief, "项目简报含项目身份")
    check("任务" in brief, "项目简报提到任务能力")

    info = e2._project_info()
    check(info["project_id"] == "proj-a" and
          info["tasks_enabled"] is True, "项目信息 tasks_enabled=True")
    check(info["sources"] and info["sources"][0]["source_id"] == "shop",
          "项目信息含数据源")
    check("password" not in str(info).lower(), "★ 项目信息不含口令")

    # 运行期热更新不崩
    e2.apply_runtime_settings()
    check(True, "apply_runtime_settings 同步项目简报不抛异常")


def main() -> int:
    import shutil

    print("M7 验收：项目信息 + 任务工具 + 项目简报")
    base = os.environ.get("NL2SQL_TEST_TMP") or os.path.join(
        ROOT, "data", "_test_m7_agent_tasks")
    work = os.path.join(base, "run")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    print(f"工作目录: {work}")
    for fn in (test_specs_and_conditional_registration,
               test_tool_runs,
               test_project_brief_injection,
               test_engine_wiring):
        try:
            fn(work)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            _failures.append(f"{fn.__name__} 异常")
    shutil.rmtree(work, ignore_errors=True)
    print("\n" + "=" * 68)
    if _failures:
        print(f"失败 {len(_failures)} 项：")
        for f in _failures:
            print("  -", f)
        return 1
    print("M7 项目/任务工具验收全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
