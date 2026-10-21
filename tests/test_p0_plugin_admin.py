"""P0 验收：配置文件读写 + 插件/工具管理 + 能力配置。

覆盖：
    [A] 配置文件层：读、写、校验、合并
    [B] 插件与工具清单接口
    [C] 工具级启停（落盘 + 重启后保持）
    [D] 能力配置（授予/收窄，含"还有几道防线"的元数据）
    [E] 运行期参数热生效（改 max_steps 无需重启）

运行:
    python tests/test_p0_plugin_admin.py
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

from nl2sql.config import AgentConfig  # noqa: E402
from nl2sql.config_file import (  # noqa: E402
    apply_to_agent_config, load_runtime_settings, render_config_toml,
    save_runtime_settings,
)
from nl2sql.contracts.tool import CAPABILITIES  # noqa: E402
from nl2sql.errors import ConfigError  # noqa: E402

_failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        _failures.append(f"{label} {detail}")


# ===================================================================== A
def test_config_file(work: str) -> None:
    print("\n[A] 配置文件层：读 / 写 / 校验 / 合并")
    path = os.path.join(work, "nl2sql.toml")

    # 1) 文件不存在不是错误：用代码默认
    s = load_runtime_settings(path)
    check(s.loop == {} and s.disabled_tools == [],
          "文件不存在时返回空设置（不报错）", s.describe())
    merged = apply_to_agent_config(AgentConfig(), s)
    check(merged.max_steps == 8, "合并空设置后仍是代码默认",
          str(merged.max_steps))

    # 2) 写回 + 读回
    save_runtime_settings(path, {
        "loop": {"max_steps": 5, "max_repeat": 3, "temperature": 0.3},
        "tools": {"disabled": ["demo.check_null_rate"]},
        "capabilities": ["semantic.read", "source.read"],
        "prompt": {"decision": "prompts/decision.md"},
        "model": {"planner": "deepseek-chat"},
    })
    check(os.path.isfile(path), "配置已写盘")
    text = open(path, encoding="utf-8").read()
    check("#" in text and "max_steps" in text, "写出内容带注释与字段")
    check("禁用" not in text or "停用" in text, "注释是可读中文")

    s2 = load_runtime_settings(path)
    check(s2.loop["max_steps"] == 5 and s2.loop["max_repeat"] == 3,
          "循环参数读回正确", str(s2.loop))
    check(abs(s2.loop["temperature"] - 0.3) < 1e-9, "浮点参数读回正确")
    check(s2.disabled_tools == ["demo.check_null_rate"], "停用工具读回")
    check(s2.capabilities == ["semantic.read", "source.read"], "能力读回")
    check(s2.prompt_files.get("decision") == "prompts/decision.md", "Prompt 路径读回")

    # 3) 合并进 AgentConfig（不可变：返回新对象）
    base = AgentConfig()
    merged2 = apply_to_agent_config(base, s2)
    check(merged2.max_steps == 5 and merged2.max_repeat == 3, "参数已合并")
    check(merged2.disabled_tools == ("demo.check_null_rate",), "停用列表已合并")
    check(merged2.capabilities == ("semantic.read", "source.read"), "能力已合并")
    check(merged2.prompt_files.get("decision") == "prompts/decision.md",
          "Prompt 映射已合并")
    check(base.max_steps == 8 and base.disabled_tools == (),
          "★ 原配置对象未被修改（不可变数据）")

    # 4) 非法值必须报明确错误，不能静默忽略
    bad_cases = [
        ("[loop]\nmax_steps = 0\n", "超出下界"),
        ("[loop]\nmax_steps = 999\n", "超出上界"),
        ("[loop]\nmax_steps = \"x\"\n", "类型错"),
        ("[loop]\nunknown_key = 1\n", "未知键"),
        ("[tools]\ndisabled = 5\n", "disabled 不是数组"),
        ("[plugins]\nenabled = \"x\"\n", "enabled 不是数组"),
        ("[capabilities]\nallow = 3\n", "allow 不是数组"),
        ("[loop]\nmax_steps = 3\n[loop]\n", "语法错"),
    ]
    for content, label in bad_cases:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        try:
            load_runtime_settings(path)
            check(False, f"非法配置被拒绝：{label}", content.strip())
        except ConfigError as e:
            check(True, f"非法配置被拒绝：{label}")
            print(f"          → {str(e)[:70]}")

    # 5) 部分配置：只写 loop，其它保持默认
    with open(path, "w", encoding="utf-8") as f:
        f.write("[loop]\nmax_steps = 12\n")
    s3 = load_runtime_settings(path)
    m3 = apply_to_agent_config(AgentConfig(), s3)
    check(m3.max_steps == 12, "只写 loop 时该项生效")
    check(m3.capabilities == AgentConfig().capabilities,
          "未写 capabilities 时沿用代码默认（不是清空！）", str(m3.capabilities))
    check(m3.disabled_tools == (), "未写 tools 时无停用项")

    # 6) 注释与手写内容不会被覆盖（因为我们只按字段读取，不整份重写）
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 我自己写的说明\n[loop]\nmax_steps = 6  # 注释\n")
    s4 = load_runtime_settings(path)
    check(s4.loop["max_steps"] == 6, "带注释的配置可解析", str(s4.loop))


# ===================================================================== B/C/D
def test_plugin_admin(work: str) -> None:
    print("\n[B] 插件与工具清单（界面「看得见」的数据源）")
    cfg_path = os.path.join(work, "nl2sql.toml")
    os.environ["NL2SQL_DB"] = GOLDEN_DB
    os.environ["NL2SQL_STATE_DIR"] = os.path.join(work, "state")
    os.environ["NL2SQL_SOURCE_ID"] = "shop"
    os.environ["NL2SQL_PLANNER"] = "agent"
    # 配置路径必须注入：否则测试会写到仓库的 config/ 下，
    # 与真实部署互相覆盖（这个坑一开始就是这么踩到的）
    os.environ["NL2SQL_CONFIG"] = cfg_path
    os.environ.pop("NL2SQL_LLM_API_KEY", None)

    from fastapi.testclient import TestClient

    from apps.web.backend.main import create_app

    # 外部插件默认**不加载**（技能是可执行代码，显式点名才运行）。
    # 本测试要验证插件管理，所以显式启用 demo。
    os.environ["NL2SQL_PLUGINS"] = "demo"

    with TestClient(create_app()) as client:
        r = client.get("/api/admin/tools")
        check(r.status_code == 200, "GET /api/admin/tools 200", str(r.status_code))
        d = r.json()
        check("tools" in d and "plugins" in d, "返回 tools 与 plugins")
        check("capabilities" in d, "返回能力清单（供界面渲染勾选框）")
        check("safety" in d, "返回安全边界状态（告诉用户还有几道防线）")
        check("plugins_enabled" in d and "plugins_available" in d,
              "★ 区分配置里可用的插件与当前已启用的插件",
              str([k for k in d if k.startswith("plugins")]))

        tools = d["tools"]
        names = [t["name"] for t in tools]
        check(len(tools) >= 9, "内置工具已列出", str(len(tools)))
        check("nl2sql.solve" in names, "含 nl2sql.solve")
        check("clarify.ask" in names, "含 clarify.ask")
        check(all("origin" in t for t in tools), "每个工具都带来源")
        check(any(t["origin"].startswith("plugin:") for t in tools),
              "★ 插件来源被标注", str(sorted({t["origin"] for t in tools})))
        check(all("enabled" in t for t in tools), "每个工具都带启用状态")
        check(all("risk" in t for t in tools), "每个工具都带风险等级")

        plugins = d["plugins"]
        check(isinstance(plugins, list) and plugins, "插件清单非空", str(plugins))
        demo = next((p for p in plugins if p["name"] == "demo"), None)
        check(demo is not None, "示例插件 demo 在清单里")
        if demo:
            check(demo["tools"] and len(demo["tools"]) == 2,
                  "demo 贡献了 2 个工具", str(demo.get("tools")))
            check(demo["requires"], "demo 声明了需要的能力", str(demo.get("requires")))
            check(demo.get("enabled") is True, "已启用的插件标为 enabled")
        # "磁盘上有但未启用"的插件也要能看到，否则无从启用
        avail = d["plugins_available"]
        check(isinstance(avail, list), "★ 单独列出磁盘上可用的插件")
        check("trend" in [a["name"] for a in avail],
              "★ 未启用的插件出现在 available 里（可被启用）",
              str([a.get("name") for a in avail]))
        check(any(a.get("reason") for a in avail if a["name"] == "trend"),
              "并说明为什么没加载", str([a.get("reason") for a in avail
                                        if a["name"] == "trend"]))

        caps = d["capabilities"]
        check(len(caps) == len(CAPABILITIES), "能力清单完整",
              f"{len(caps)} vs {len(CAPABILITIES)}")
        check(all("granted" in c and "risk" in c and "unlocks" in c for c in caps),
              "每项能力带 granted/risk/unlocks（界面据此渲染与告警）")
        write_cap = next((c for c in caps if c["name"] == "source.write"), None)
        check(write_cap is not None, "source.write 在清单里")
        if write_cap:
            # 断言"说明了解锁什么"与"标为高危"，而不是匹配某个具体字词——
            # 前者是设计意图，后者是文案细节，文案改了不该让测试红
            check(bool(write_cap.get("unlocks")),
                  "★ source.write 说明了解锁什么", str(write_cap.get("unlocks")))
            check(write_cap.get("risk") == "high",
                  "★ source.write 被标为高危", str(write_cap.get("risk")))
        check(d["safety"].get("read_only") is True,
              "★ 透出 guard 的只读状态（第二道防线）")
        check(d["safety"].get("write_tools_implemented") is False,
              "★ 说明当前没有任何实现写操作的工具（授予也不解锁）",
              str(d["safety"]))

        # ---------------------------------------------------------- C 启停
        print("\n[C] 工具级启停（落盘 + 重启后保持）")
        r = client.post("/api/admin/tools/toggle", json={
            "name": "demo.check_null_rate", "enabled": False})
        check(r.status_code == 200, "停用请求 200", str(r.status_code))
        d2 = r.json()
        check(d2["ok"] is True, "停用成功", str(d2))
        check(d2["disabled"], "返回停用列表", str(d2.get("disabled")))

        # 停用后不应再出现在清单的"已注册"里
        d3 = client.get("/api/admin/tools").json()
        t3 = next(t for t in d3["tools"] if t["name"] == "demo.check_null_rate")
        check(t3["enabled"] is False, "清单反映停用状态")

        # 配置文件里应该记下来了
        loaded = load_runtime_settings(cfg_path)
        check("demo.check_null_rate" in loaded.disabled_tools,
              "★ 停用已落盘到配置文件", str(loaded.disabled_tools))

        # 重新装配（模拟重启）后仍应停用
        r = client.post("/api/admin/reload", json={})
        check(r.status_code == 200, "重载请求 200", str(r.status_code))
        d4 = client.get("/api/admin/tools").json()
        t4 = next(t for t in d4["tools"] if t["name"] == "demo.check_null_rate")
        check(t4["enabled"] is False, "★ 重载后停用状态保持")
        check(t4["registered"] is False, "停用工具未被注册",
              str(t4))

        # 恢复启用
        r = client.post("/api/admin/tools/toggle", json={
            "name": "demo.check_null_rate", "enabled": True})
        check(r.status_code == 200 and r.json()["enabled"] is True,
              "恢复启用请求成功", str(r.json()))
        client.post("/api/admin/reload", json={})
        d5 = client.get("/api/admin/tools").json()
        t5 = next(t for t in d5["tools"] if t["name"] == "demo.check_null_rate")
        check(t5["enabled"] is True and t5["registered"] is True,
              "恢复启用后重新注册", str(t5))

        # 停用不存在的工具应给明确错误
        r = client.post("/api/admin/tools/toggle", json={
            "name": "no.such.tool", "enabled": False})
        check(r.status_code == 400, "停用不存在的工具返回 400", str(r.status_code))

        # 停用主力工具要有警告
        r = client.post("/api/admin/tools/toggle", json={
            "name": "nl2sql.solve", "enabled": False})
        d6 = r.json()
        check(bool(d6.get("warning")),
              "★ 停用主力工具时给出退化警告", str(d6.get("warning")))
        client.post("/api/admin/tools/toggle", json={
            "name": "nl2sql.solve", "enabled": True})

        # ---------------------------------------------------------- D 能力
        print("\n[D] 能力配置（授予 / 收窄）")
        r = client.post("/api/admin/capabilities", json={
            "grant": ["source.write"]})
        check(r.status_code == 200, "授予能力 200", str(r.status_code))
        d7 = r.json()
        check("source.write" in d7.get("capabilities", []),
              "能力已授予", str(d7.get("capabilities")))
        check(bool(d7.get("notice")),
              "★ 授予高危能力时返回提示（说明还有哪些防线）",
              str(d7.get("notice")))
        print(f"        notice: {str(d7.get('notice'))[:90]}")

        r = client.post("/api/admin/capabilities", json={
            "revoke": ["source.write"]})
        d8 = r.json()
        check("source.write" not in d8.get("capabilities", []), "能力已收回")

        # 收回某能力会导致依赖它的工具不再注册（需重载生效）
        r = client.post("/api/admin/capabilities", json={"revoke": ["clarify.ask"]})
        d_rev = r.json()
        check(bool(d_rev.get("notice")),
              "★ 收回能力时提示受影响工具", str(d_rev.get("notice"))[:80])
        client.post("/api/admin/reload", json={})
        d9 = client.get("/api/admin/tools").json()
        t9 = next(t for t in d9["tools"] if t["name"] == "clarify.ask")
        check(t9["registered"] is False,
              "★ 收回能力并重载后，依赖它的工具不再注册", str(t9))
        check(t9["missing_capabilities"] == ["clarify.ask"],
              "并说明缺哪个能力", str(t9.get("missing_capabilities")))
        client.post("/api/admin/capabilities", json={"grant": ["clarify.ask"]})
        client.post("/api/admin/reload", json={})

        # 未知能力被拒
        r = client.post("/api/admin/capabilities", json={"grant": ["nope.cap"]})
        check(r.status_code == 400, "未知能力返回 400", str(r.status_code))

        # ---------------------------------------------------------- E 热生效
        print("\n[E] 运行期参数热生效")
        r = client.post("/api/admin/loop", json={
            "max_steps": 4, "max_repeat": 1})
        check(r.status_code == 200, "改循环参数 200", str(r.status_code))
        d10 = r.json()
        check(d10["loop"]["max_steps"] == 4, "参数已更新", str(d10.get("loop")))
        loaded2 = load_runtime_settings(cfg_path)
        check(loaded2.loop.get("max_steps") == 4,
              "★ 已落盘（重启后仍生效）", str(loaded2.loop))

        # 运行时真的用上了新值
        engine = client.app.state.engine_holder.engine
        check(engine._runtime.config.max_steps == 4,
              "★ 运行时已生效（无需重启）",
              str(engine._runtime.config.max_steps))

        # 非法参数被拒
        r = client.post("/api/admin/loop", json={"max_steps": 0})
        check(r.status_code == 400, "非法参数返回 400", str(r.status_code))

        # ---------------------------------------------------------- 配置原文
        print("\n[F] 配置文件原文（界面可编辑）")
        r = client.get("/api/admin/config")
        check(r.status_code == 200, "GET /api/admin/config 200")
        d11 = r.json()
        check("text" in d11 and "path" in d11, "返回原文与路径", str(list(d11)))
        check("max_steps" in d11["text"], "原文含当前参数")
        r = client.post("/api/admin/config", json={
            "text": "# 界面改的\n[loop]\nmax_steps = 7\n"})
        check(r.status_code == 200, "写回配置原文 200", str(r.status_code))
        check(load_runtime_settings(cfg_path).loop.get("max_steps") == 7,
              "★ 界面手改的配置生效")

        # 写回非法内容要被拒且不破坏原文件
        r = client.post("/api/admin/config", json={
            "text": "[loop]\nmax_steps = -5\n"})
        check(r.status_code == 400, "非法配置被拒", str(r.status_code))
        check(load_runtime_settings(cfg_path).loop.get("max_steps") == 7,
              "★ 拒绝后原配置未被破坏")


        # ------------------------------------------------- 插件级启停（真实加载）
        print("\n[G] 插件级启停（界面启用未加载的插件）")
        # 这是"插件管理"最关键的一条：默认不加载任何外部插件，
        # 用户必须在界面里显式启用，且启用后**真的**贡献工具。
        r = client.post("/api/admin/plugins/toggle",
                        json={"name": "trend", "enabled": True})
        check(r.status_code == 200, "启用插件 200", str(r.status_code))
        if r.status_code == 200:
            check("trend" in r.json().get("plugins_enabled", []),
                  "★ 启用后插件进入白名单", str(r.json().get("plugins_enabled")))

        d7 = client.get("/api/admin/tools").json()
        check("trend" in [p["name"] for p in d7["plugins"]],
              "★ 启用后该插件出现在已加载清单",
              str([p["name"] for p in d7["plugins"]]))
        check("trend" not in [a["name"] for a in d7["plugins_available"]],
              "★ 已启用的插件不再出现在 available 里",
              str([a["name"] for a in d7["plugins_available"]]))
        trend_tools = [t for t in d7["tools"] if t["origin"] == "plugin:trend"]
        check(bool(trend_tools), "★ 启用后插件工具出现在工具清单",
              str([t["name"] for t in d7["tools"] if t["origin"].startswith("plugin:")]))
        check(all(t["registered"] for t in trend_tools),
              "★ 插件工具真的注册进了运行时（不只是列出来）",
              str([(t["name"], t["registered"]) for t in trend_tools]))

        # 落盘：重启进程后应保持启用
        persisted = load_runtime_settings(cfg_path)
        check("trend" in persisted.enabled_plugins,
              "★ 插件启用状态已落盘（重启后仍启用）",
              str(persisted.enabled_plugins))

        # 停用
        r = client.post("/api/admin/plugins/toggle",
                        json={"name": "trend", "enabled": False})
        check(r.status_code == 200, "停用插件 200", str(r.status_code))
        d8 = client.get("/api/admin/tools").json()
        check("trend" not in [p["name"] for p in d8["plugins"]],
              "★ 停用后插件不再加载")
        check("trend" in [a["name"] for a in d8["plugins_available"]],
              "★ 停用后回到 available（可以再启用）")
        still = [t for t in d8["tools"] if t["origin"] == "plugin:trend"]
        check(all(not t["registered"] for t in still),
              "★ 停用后插件工具不再注册（模型看不到它）",
              str([(t["name"], t["registered"]) for t in still]))

        # 不存在的插件要明确报错并给出候选，而不是静默成功
        r = client.post("/api/admin/plugins/toggle",
                        json={"name": "no_such_plugin", "enabled": True})
        check(r.status_code == 400, "启用不存在的插件返回 400", str(r.status_code))
        check("no_such_plugin" in r.json().get("detail", ""),
              "报错里包含插件名", str(r.json().get("detail"))[:80])

        # 全量加载模式下不允许单独停用——否则用户以为关掉了其实没关
        engine7 = client.app.state.engine_holder.engine
        saved = engine7.config.agent.enabled_plugins
        engine7.config.agent.enabled_plugins = ("*",)
        r = client.post("/api/admin/plugins/toggle",
                        json={"name": "trend", "enabled": False})
        check(r.status_code == 400,
              "★ 全量加载（*）时拒绝单独停用（避免假象）", str(r.status_code))
        check("白名单" in r.json().get("detail", ""),
              "并说明该怎么改", str(r.json().get("detail"))[:90])
        engine7.config.agent.enabled_plugins = saved

        # 恢复：把 loop 参数改回一个正常值，避免影响后续手动试用
        client.post("/api/admin/loop", json={"max_steps": 8})


def main() -> int:
    base = os.environ.get("NL2SQL_TEST_TMP") or os.path.join(ROOT, "data", "_test_p0")
    work = os.path.join(base, "run")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    print(f"工作目录: {work}")

    try:
        test_config_file(work)
        test_plugin_admin(work)
    except Exception:
        traceback.print_exc()
        _failures.append("未捕获异常")

    shutil.rmtree(work, ignore_errors=True)
    print("\n" + "=" * 66)
    if _failures:
        print(f"P0 验收未通过：{len(_failures)} 项失败")
        for x in _failures[:25]:
            print("  -", x)
        return 1
    print("P0 验收全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
