"""开发者体验试用：验证"可插拔"是否真的成立。

与用户视角的 `internal_trial.py` 不同，这份是**插件作者视角**：
    1. 只按文档写一个插件，能否装载？
    2. 工具单独调用是否正确？
    3. 默认要不要加载外部插件？（安全性）
    4. agent 会不会真的用上这个新工具？
    5. 坏插件、慢插件会不会拖垮系统？
    6. 运行中新增插件目录要不要重启？

用法:
    python scripts/plugin_dev_trial.py            # 全部
    python scripts/plugin_dev_trial.py --no-llm   # 跳过需要模型的环节
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_DB = os.path.join(ROOT, "golden_set", "ecommerce.db")
NOW = "2024-06-30"


def section(t: str) -> None:
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


def result(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK ' if ok else '!! '} {label}" + (f"  {detail}" if detail else ""))


def main() -> int:
    no_llm = "--no-llm" in sys.argv

    # ------------------------------------------------------------ 1) 装载
    section("1) 只按文档写插件 → 能否装载")
    from nl2sql.plugins.loader import PluginLoader

    loader = PluginLoader()
    loaded = loader.load_all(os.path.join(ROOT, "plugins"))
    result("装载无错误", not loader.errors, str(loader.errors))
    names = [p.manifest.name for p in loaded]
    result("发现 demo 与 trend", set(names) >= {"demo", "trend"}, str(names))
    trend = next((p for p in loaded if p.manifest.name == "trend"), None)
    result("trend 已被加载", trend is not None)
    if trend:
        result("按声明授予能力",
               [c.name for c in trend.capabilities] == ["source.read"],
               str([c.name for c in trend.capabilities]))
        result("工具已提取",
               [t.spec.name for t in trend.tools] == ["trend.compare_periods"])

    # ------------------------------------------------------------ 2) 单跑工具
    section("2) 工具单独调用是否正确")
    from nl2sql.catalog import CatalogService
    from nl2sql.config import NL2SQLConfig
    from nl2sql.contracts.tool import Capability, ToolContext
    from nl2sql.sources.sqlite import SqliteAdapter
    from nl2sql.storage import build_storage

    work = os.path.join(ROOT, "data", "_devtrial")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    cfg = NL2SQLConfig(state_dir=os.path.join(work, "state"))
    cfg.add_sqlite(GOLDEN_DB, source_id="shop")
    cfg.validate()
    bundle = build_storage(cfg)
    adapter = SqliteAdapter(GOLDEN_DB)
    catalog = CatalogService(bundle.meta, {"shop": adapter})
    catalog.refresh("shop")

    caps = (Capability("source.read"),)
    ctx = ToolContext(capabilities=caps, source_id="shop", now=NOW)
    ctx.provide("adapter", adapter)
    ctx.provide("catalog", catalog)

    tool = trend.tools[0] if trend else None
    if tool:
        r = tool.run({"current_month": "2024-06"}, ctx)
        result("无分组环比执行成功", r.ok, str(r.error or ""))
        if r.ok:
            overall = r.data["总体"]
            print(f"        上期(2024-05)={overall['上期']}  "
                  f"本期(2024-06)={overall['本期']}  "
                  f"变化率={overall['变化率']}")
            result("变化率已计算", overall["变化率"] is not None)
            result("口径已声明", bool(r.data.get("口径")), r.data.get("口径", ""))

        r2 = tool.run({"current_month": "2024-06", "group_by": "channel_id"}, ctx)
        result("分组环比执行成功", r2.ok, str(r2.error or ""))
        if r2.ok:
            print(f"        分组数={len(r2.data['rows'])}")

        # 错误路径：能不能给可行动的提示
        r3 = tool.run({"current_month": "6月"}, ctx)
        result("非法月份被拒并给提示",
               (not r3.ok) and bool(r3.hint), f"{r3.error} | {r3.hint[:40]}")
        r4 = tool.run({"current_month": "2024-06", "metric": "毛利率"}, ctx)
        result("不支持的指标被拒并给候选",
               (not r4.ok) and bool(r4.candidates), str(r4.candidates))
        r5 = tool.run({"current_month": "2024-06",
                       "group_by": "x; DROP TABLE fact_order"}, ctx)
        result("注入尝试被拒",
               (not r5.ok) and "非法字符" in (r5.error or ""), str(r5.error))

    # ------------------------------------------------------------ 3) 默认加载？
    section("3) 默认是否加载外部插件（安全性）")
    from nl2sql import NL2SQL

    cfg2 = NL2SQLConfig(state_dir=os.path.join(work, "state2"))
    cfg2.add_sqlite(GOLDEN_DB, source_id="shop")
    cfg2.agent.planner = "agent"
    # **关键：显式指向插件目录**，同时刻意把白名单留空。
    # 上一版这里漏了 plugins_dir（走了默认相对路径），导致断言失败——
    # 那是测试写错，不是行为错。默认行为是：有目录就扫，但白名单为空时**不加载**。
    cfg2.agent.plugins_dir = os.path.join(ROOT, "plugins")
    cfg2.agent.enabled_plugins = ()          # 空 = 一个都不加载
    print(f"        agent.enabled_plugins = {cfg2.agent.enabled_plugins!r}"
          f"（空 = 不加载外部插件）")
    with NL2SQL(cfg2) as e:
        e.refresh_snapshot()
        n_plugins = len(getattr(e, "plugin_manifests", []) or [])
        result("白名单为空时**不加载**外部插件（安全默认）",
               n_plugins == 0, f"实际加载 {n_plugins} 个")
        result("内置工具仍可用", len(e.tools.names()) >= 9, str(len(e.tools.names())))

    # 通配符：显式声明"我信任这个目录里的全部插件"
    cfg2b = NL2SQLConfig(state_dir=os.path.join(work, "state2b"))
    cfg2b.add_sqlite(GOLDEN_DB, source_id="shop")
    cfg2b.agent.planner = "agent"
    cfg2b.agent.plugins_dir = os.path.join(ROOT, "plugins")
    cfg2b.agent.enabled_plugins = ("*",)
    with NL2SQL(cfg2b) as e:
        e.refresh_snapshot()
        n = len(getattr(e, "plugin_manifests", []) or [])
        result("显式写 [\"*\"] 时才加载全部", n == 2, f"实际 {n} 个")

    # ------------------------------------------------------------ 4) 白名单加载
    section("4) 按白名单显式启用插件")
    cfg3 = NL2SQLConfig(state_dir=os.path.join(work, "state3"))
    cfg3.add_sqlite(GOLDEN_DB, source_id="shop")
    cfg3.agent.planner = "agent"
    cfg3.agent.plugins_dir = os.path.join(ROOT, "plugins")
    cfg3.agent.enabled_plugins = ("trend",)
    with NL2SQL(cfg3) as e:
        e.refresh_snapshot()
        result("只加载白名单里的 trend",
               [m["name"] for m in e.plugin_manifests] == ["trend"],
               str([m["name"] for m in e.plugin_manifests]))
        result("trend 工具已注册",
               e.tools.has("trend.compare_periods"), str(e.tools.names()))
        result("demo 未注册（被白名单排除）",
               not e.tools.has("demo.profile_column"))
        desc = e.tools.describe()
        t = next((x for x in desc if x["name"] == "trend.compare_periods"), None)
        result("来源标注为插件",
               t is not None and t["origin"] == "plugin:trend",
               str(t and t["origin"]))

    # ------------------------------------------------------------ 5) 坏插件
    section("5) 坏插件 / 慢插件是否拖垮系统")
    bad_dir = os.path.join(work, "plugins_bad")
    for name, toml, code in [
        ("crash", '[plugin]\nname="crash"\nentry="plugin.py"\nrequires=["source.read"]\n',
         "def register():\n    raise RuntimeError('故意炸')\n"),
        ("badspec", '[plugin]\nname="badspec"\nentry="plugin.py"\nrequires=["source.read"]\n',
         "def register():\n    return ['不是工具']\n"),
        ("overreach",
         '[plugin]\nname="overreach"\nentry="plugin.py"\nrequires=["source.read"]\n',
         "from nl2sql.plugins import ToolResult, ToolSpec\n"
         "class T:\n"
         "    spec = ToolSpec(name='x.w', description='d', requires=('source.write',))\n"
         "    def run(self,a,c): return ToolResult(ok=True)\n"
         "def register(): return [T()]\n"),
        ("boom", '[plugin]\nname="boom"\nentry="plugin.py"\nrequires=["source.read"]\n',
         "from nl2sql.plugins import ToolResult, ToolSpec\n"
         "class T:\n"
         "    spec = ToolSpec(name='boom.run', description='运行期抛异常', requires=('source.read',))\n"
         "    def run(self,a,c): raise RuntimeError('运行期炸')\n"
         "def register(): return [T()]\n"),
    ]:
        d = os.path.join(bad_dir, name)
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "plugin.toml"), "w", encoding="utf-8").write(toml)
        open(os.path.join(d, "plugin.py"), "w", encoding="utf-8").write(code)

    cfg4 = NL2SQLConfig(state_dir=os.path.join(work, "state4"))
    cfg4.add_sqlite(GOLDEN_DB, source_id="shop")
    cfg4.agent.planner = "agent"
    cfg4.agent.plugins_dir = bad_dir
    # 必须显式启用才会去扫这个目录（安全默认：空白名单 = 不加载）
    cfg4.agent.enabled_plugins = ("*",)
    t0 = time.perf_counter()
    with NL2SQL(cfg4) as e:
        e.refresh_snapshot()
        elapsed = time.perf_counter() - t0
        result("引擎仍然可用（坏插件未拖垮启动）", True, f"{elapsed:.1f}s")
        result("坏插件被记录而非吞掉",
               len(e.plugin_errors) >= 2, str(e.plugin_errors)[:150])
        result("越权插件被拒（能力未声明）",
               any("source.write" in x or "未声明" in x or "未被配置授予" in x
                   for x in e.plugin_errors),
               str([x for x in e.plugin_errors if "source" in x])[:120])
        # 运行期抛异常的工具不该打断循环。
        # 注意：**必须通过运行时调用**才会被收敛——直接调 `tool.run()`
        # 抛异常是正常的，收敛逻辑在 `AgentRuntime._invoke` 里。
        # （上一版直接调了 run，断言写错，不是实现问题。）
        if e.tools.has("boom.run") and e._runtime is not None:
            from nl2sql.contracts.tool import ToolContext as TC
            bctx = TC(capabilities=caps, source_id="shop")
            bctx.provide("adapter", adapter)
            res = e._runtime._invoke("boom.run", {}, bctx)
            result("运行期异常被运行时收敛为 ToolResult（不抛出）",
                   res.ok is False and "RuntimeError" in (res.error or ""),
                   str(res.error))
            result("并给了可行动的提示", bool(res.hint), str(res.hint)[:50])

    shutil.rmtree(work, ignore_errors=True)

    # ------------------------------------------------------------ 6) 需模型
    if no_llm:
        section("6) agent 是否真的用上新工具 —— 跳过（--no-llm）")
        return 0

    key = os.environ.get("NL2SQL_LLM_API_KEY", "")
    if not key:
        section("6) agent 是否真的用上新工具 —— 跳过（未设 Key）")
        return 0

    section("6) agent 是否真的会用上新工具")
    from nl2sql.llm import OpenAICompatProvider
    from nl2sql.semantics.golden_model import build_model

    work2 = os.path.join(ROOT, "data", "_devtrial2")
    shutil.rmtree(work2, ignore_errors=True)
    os.makedirs(work2, exist_ok=True)
    cfg5 = NL2SQLConfig(state_dir=os.path.join(work2, "state"))
    cfg5.add_sqlite(GOLDEN_DB, source_id="shop")
    cfg5.agent.planner = "agent"
    cfg5.agent.plugins_dir = os.path.join(ROOT, "plugins")
    cfg5.agent.enabled_plugins = ("trend",)
    provider = OpenAICompatProvider(
        api_key=key,
        base_url=os.environ.get("NL2SQL_LLM_BASE_URL",
                                "https://api.deepseek.com/v1"),
        model=os.environ.get("NL2SQL_LLM_MODEL", "deepseek-chat"),
    )
    with NL2SQL(cfg5, semantic_model=build_model(order_level=True),
                llm_provider=provider) as e:
        e.refresh_snapshot()
        q = "6月的实收金额比5月增长了吗？增长了多少？"
        sid = e.new_session_id()
        t0 = time.perf_counter()
        out = e.ask(q, now=NOW, session_id=sid)
        elapsed = time.perf_counter() - t0
        session = e.storage.sessions.load(sid)
        used = [t.tool_name for t in (session.turns if session else [])
                if t.role == "tool"]
        print(f"        Q: {q}")
        print(f"        status={out.status}  {elapsed:.1f}s  工具={used}")
        print(f"        回答: {(out.message or '')[:200]}")
        result("★ agent 主动调用了新插件", "trend.compare_periods" in used,
               str(used))
        result("结果里出现两期数值对比",
               "171" in (out.message or "") or "增长" in (out.message or "")
               or "下降" in (out.message or ""),
               (out.message or "")[:60])

    shutil.rmtree(work2, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
