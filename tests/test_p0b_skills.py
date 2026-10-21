"""P0b 验收：技能机制（发现 / 解析 / 报告 / `skill.load` / 界面第四块）。

这一批的验收重点不是"技能能不能用"，而是**两件容易被做假的事**：

1. **不改任何现有行为**。P0 的定位是"机制通了，行为不变"。
   所以最硬的一条断言是：**零技能配置下，工具清单与加技能机制之前逐字相同**
   （`skill.load` 根本不注册）。做法与既有原则一致——
   "能力未授予的工具不注册，模型不会去调一个它看不见的工具"。
2. **失败必须可见**。技能文件写错、外置 Prompt 路径写错，
   都要出现在报告与管理界面上。静默回退是本项目反复踩的坑
   （`_load_decision_prompt` 原来的 `return ""` 就是反面教材）。

运行:
    python tests/test_p0b_skills.py
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


def _write(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _skill_md(name: str, description: str, body: str = "## 步骤\n1. 做一件事",
              ns: str = "") -> str:
    ns_block = f"nl2sql:\n{ns}\n" if ns else ""
    return (f"---\nname: {name}\n"
            f"description: {description}\n{ns_block}---\n\n# {name}\n\n{body}\n")


def _loader(**kw):
    from nl2sql.skills import SkillLoader

    return SkillLoader(**kw)


# --------------------------------------------------------------------- A
def test_discovery(work: str) -> None:
    section("[A] 发现与解析：两种放置方式 / 必填校验 / 容忍未知键")
    from nl2sql.skills import SkillLoader

    root = os.path.join(work, "skills")
    _write(os.path.join(root, "bundle", "SKILL.md"),
           _skill_md("bundle-skill", "用户做 A 事时使用"))
    _write(os.path.join(root, "flat.md"),
           _skill_md("flat-skill", "用户做 B 事时使用"))
    # 附属文件不该被当成技能
    _write(os.path.join(root, "bundle", "references", "detail.md"), "# 参考\n内容")
    # 普通文档：不该报错
    _write(os.path.join(root, "README.md"), "# 说明\n这不是技能\n")

    loader = SkillLoader()
    cat = loader.load(project_dirs=[root])
    check(sorted(cat.names()) == ["bundle-skill", "flat-skill"],
          "★ 目录式与平铺两种都能发现",
          str(cat.names()))
    check(any("README" in n or "readme" in n.lower()
              for n, _r in loader.report.skipped),
          "★ 普通 README.md 被跳过而不是报失败（否则报告很快没人看）",
          str(loader.report.skipped))
    check(not loader.report.rejected, "没有加载失败",
          str(loader.report.rejected))

    # ---- 必填校验 ----
    bad = os.path.join(work, "bad")
    _write(os.path.join(bad, "no-desc", "SKILL.md"),
           "---\nname: no-desc\n---\n\n# x\n正文\n")
    _write(os.path.join(bad, "no-name", "SKILL.md"),
           "---\ndescription: 缺 name\n---\n\n# x\n正文\n")
    _write(os.path.join(bad, "badname", "SKILL.md"),
           "---\nname: 有中文名字\ndescription: 非法名\n---\n\n# x\n正文\n")
    _write(os.path.join(bad, "nobody", "SKILL.md"),
           "---\nname: nobody\ndescription: 没正文\n---\n\n")
    _write(os.path.join(bad, "nofm", "SKILL.md"), "# 没有 frontmatter\n")
    _write(os.path.join(bad, "badkind", "SKILL.md"),
           _skill_md("badkind", "kind 写错", ns="  kind: notebook"))
    _write(os.path.join(bad, "badcap", "SKILL.md"),
           _skill_md("badcap", "能力名写错", ns="  requires: [source.readd]"))
    _write(os.path.join(bad, "unknownkey", "SKILL.md"),
           _skill_md("unknownkey", "未知键应容忍", ns="  whatever: 1"))
    _write(os.path.join(bad, "toobig", "SKILL.md"),
           _skill_md("toobig", "正文超限", body="x" * 200))

    loader2 = SkillLoader(max_body_chars=100)
    cat2 = loader2.load(project_dirs=[bad])
    reasons = {n: r for n, r in loader2.report.rejected}
    check("no-desc" in reasons and "description" in reasons["no-desc"],
          "★ 缺 description 被拒（它是唯一的选择依据）", str(reasons))
    check("no-name" in reasons, "缺 name 被拒", str(reasons))
    check("badname" in reasons or "有中文名字" in reasons,
          "非法技能名被拒", str(reasons))
    check("有中文名字" in reasons,
          "★ 报告用**声明的名字**而不是目录名（用户认的是 frontmatter 里的 name）",
          str(sorted(reasons)))
    check("nobody" in reasons and "正文" in reasons["nobody"],
          "空正文被拒", str(reasons))
    check("nofm" in reasons and "frontmatter" in reasons["nofm"],
          "★ SKILL.md 缺 frontmatter 被拒（文件名表明了意图）", str(reasons))
    check("badkind" in reasons, "非法 kind 被拒", str(reasons))
    check("badcap" in reasons and "未知能力" in reasons["badcap"],
          "未声明的能力名被拒（复用能力白名单）", str(reasons))
    check("toobig" in reasons and "截断" in reasons["toobig"],
          "★ 正文超限被**拒绝**而不是截断（截断的流程不成立）",
          str(reasons.get("toobig"))[:90])
    check("unknownkey" in cat2.names(),
          "nl2sql 下的未知键被容忍（只发警告）", str(cat2.names()))
    check(any("unknownkey" in w for w in loader2.report.warnings),
          "且发出了警告", str(loader2.report.warnings))


# --------------------------------------------------------------------- B
def test_trust(work: str) -> None:
    section("[B] 信任模型：外部技能默认不加载，需点名")
    proj = os.path.join(work, "proj")
    ext = os.path.join(work, "ext")
    _write(os.path.join(proj, "mine", "SKILL.md"),
           _skill_md("mine", "项目自己的技能"))
    _write(os.path.join(ext, "theirs", "SKILL.md"),
           _skill_md("theirs", "外部技能"))
    _write(os.path.join(ext, "off", "SKILL.md"),
           _skill_md("off", "外部技能但自己关了", ns="  enabled: false"))
    _write(os.path.join(proj, "scoped", "SKILL.md"),
           _skill_md("scoped", "只对另一个数据源生效", ns="  scope: other"))

    loader = _loader()
    cat = loader.load(project_dirs=[proj], external_dirs=[ext],
                      source_id="shop")
    check("mine" in cat.names(), "★ 项目技能默认加载", str(cat.names()))
    check("theirs" not in cat.names(),
          "★ 外部技能默认**不**加载（与插件同一条原则）", str(cat.names()))
    check("scoped" not in cat.names(),
          "scope 不符的技能不加载", str(cat.names()))
    skipped = {n: r for n, r in loader.report.skipped}
    check("theirs" in skipped and "点名" in skipped["theirs"],
          "★ 并说明为什么没加载（可照着改）", str(skipped))
    check("scoped" in skipped and "scope" in skipped["scoped"],
          "scope 不符也给出理由", str(skipped))

    # 点名后加载
    loader2 = _loader()
    cat2 = loader2.load(project_dirs=[proj], external_dirs=[ext],
                        enabled_external=["theirs"], source_id="shop")
    check("theirs" in cat2.names(), "★ 白名单点名后加载", str(cat2.names()))
    check("off" not in cat2.names(), "文件里 enabled=false 仍不加载")

    # 通配
    cat3 = _loader().load(project_dirs=[proj], external_dirs=[ext],
                          enabled_external=["*"], source_id="shop")
    check("theirs" in cat3.names() and "off" not in cat3.names(),
          '"*" 加载全部（但仍尊重文件里的 enabled=false）', str(cat3.names()))

    # 外部同名不得覆盖项目技能
    _write(os.path.join(ext, "mine", "SKILL.md"),
           _skill_md("mine", "外部想冒充项目技能"))
    loader4 = _loader()
    cat4 = loader4.load(project_dirs=[proj], external_dirs=[ext],
                        enabled_external=["*"], source_id="shop")
    got = cat4.get("mine")
    check(got is not None and got.origin == "project",
          "★ 外部同名技能不得覆盖项目技能（最坏的一种静默替换）",
          f"{got.origin if got else None} @ {got.path if got else None}")

    # scope 匹配时加载
    cat5 = _loader().load(project_dirs=[proj], external_dirs=[ext],
                          source_id="other")
    check("scoped" in cat5.names(), "scope 匹配时正常加载", str(cat5.names()))


# --------------------------------------------------------------------- C
def test_report_and_prompt(work: str) -> None:
    section("[C] 报告：加载失败与外置 Prompt 问题都必须可见")
    from nl2sql import NL2SQL, NL2SQLConfig

    state = os.path.join(work, "state_c")
    cfg = NL2SQLConfig(state_dir=state)
    cfg.add_sqlite(GOLDEN_DB, source_id="shop")
    cfg.agent.planner = "rule"
    # 项目技能目录：一好一坏
    sk = os.path.join(work, "skills_c")
    _write(os.path.join(sk, "good", "SKILL.md"),
           _skill_md("good-skill", "用户做某事时使用"))
    _write(os.path.join(sk, "broken", "SKILL.md"),
           "---\nname: broken\n---\n\n# x\n正文\n")
    cfg.skills.dirs = (sk,)
    cfg.skills.external_dirs = ()          # 测项目技能，不扫外部
    # P0.5：外置 Prompt 指向不存在的文件
    cfg.agent.prompt_files = {"decision": os.path.join(work, "nope.md")}

    with NL2SQL(cfg) as e:
        e.refresh_snapshot()
        rep = e.skill_report
        check(rep is not None, "装配后拿到技能报告")
        check(rep.loaded == ["good-skill"], "好技能加载", str(rep.loaded))
        check(any(n == "broken" for n, _r in rep.rejected),
              "★ 坏技能出现在报告里（不阻断启动，但也不吞掉）",
              str(rep.rejected))
        check("skill.load" in e.tools.names(),
              "有技能时 skill.load 注册", str(e.tools.names()))
        d = rep.describe()
        check(isinstance(d.get("rejected"), list) and d["rejected"],
              "describe() 里带 rejected", str(d)[:120])

        # ---- P0.5：外置 Prompt 静默回退被修掉 ----
        issues = " ".join(rep.prompt_issues)
        check("不存在" in issues,
              "★ 外置 Prompt 路径写错 → 出现在报告里（原先静默回退）",
              issues[:140])
        check("没有生效" in issues,
              "★ 并且明说「你的 Prompt 没有生效」", issues[:160])

    # ---- HTTP 面：管理界面第四块 ----
    from fastapi.testclient import TestClient

    from apps.web.backend.main import create_app

    os.environ["NL2SQL_DB"] = GOLDEN_DB
    os.environ["NL2SQL_STATE_DIR"] = os.path.join(work, "state_http")
    os.environ["NL2SQL_SOURCE_ID"] = "shop"
    os.environ["NL2SQL_CONFIG"] = os.path.join(work, "http.toml")
    os.environ["NL2SQL_PLANNER"] = "rule"
    os.environ["NL2SQL_METRICS"] = ""
    os.environ["NL2SQL_PLUGINS"] = ""
    os.environ["NL2SQL_SKILLS"] = os.path.join(work, "skills_http")
    os.environ["NL2SQL_SKILLS_ENABLED"] = ""
    os.environ.pop("NL2SQL_LLM_API_KEY", None)

    with TestClient(create_app()) as client:
        r = client.get("/api/admin/tools")
        check(r.status_code == 200, "GET /api/admin/tools 200", str(r.status_code))
        d = r.json()
        check("skills" in d and "skill_report" in d,
              "★ 返回第四块：技能 + 报告", str([k for k in d if "skill" in k]))
        rep = d.get("skill_report") or {}
        check(rep.get("loaded") == ["demo-skill"],
              "界面能看到已加载技能", str(rep.get("loaded")))
        check(any(s["name"] == "demo-skill" for s in d["skills"]),
              "技能清单带 name/description/origin",
              str(d["skills"][:1]))
        s0 = next(s for s in d["skills"] if s["name"] == "demo-skill")
        check(s0.get("description") and s0.get("origin") == "project",
              "★ 描述与来源都在（描述是选择依据，必须可见）", str(s0))
        check(any(x["name"] == "bad-skill" for x in rep.get("rejected", [])),
              "★ 加载失败也出现在界面上",
              str(rep.get("rejected")))


# --------------------------------------------------------------------- D
def test_skill_load_tool(work: str) -> None:
    section("[D] skill.load 工具：条件注册 / 正文完整 / 名字错给候选")
    from nl2sql import NL2SQL, NL2SQLConfig

    long_body = "\n".join(f"{i}. 第 {i} 步" for i in range(1, 60))
    sk = os.path.join(work, "skills_d")
    _write(os.path.join(sk, "demo", "SKILL.md"),
           _skill_md("demo", "用户做演示任务时使用", body=long_body))
    _write(os.path.join(sk, "other", "SKILL.md"),
           _skill_md("other", "用户做另一件事时使用"))
    _write(os.path.join(sk, "scoped", "SKILL.md"),
           _skill_md("scoped", "只对别的数据源", ns="  scope: elsewhere"))

    cfg = NL2SQLConfig(state_dir=os.path.join(work, "state_d"))
    cfg.add_sqlite(GOLDEN_DB, source_id="shop")
    cfg.agent.planner = "rule"
    cfg.skills.dirs = (sk,)
    cfg.skills.external_dirs = ()

    with NL2SQL(cfg) as e:
        e.refresh_snapshot()
        # 模型看到的是**注册进运行时**的那份规格（带目录）
        registered = next(t.spec for t in e.tools.all()
                          if t.spec.name == "skill.load")
        check("demo" in registered.description
              and "other" in registered.description,
              "★ 技能目录渲染进了**注册工具**的说明（模型才知道有得加载）",
              registered.description[-140:])
        check("scoped" not in registered.description,
              "★ scope 不符的技能不进目录（否则模型会去加载用不了的技能）",
              registered.description[-140:])
        # 管理界面的"工具全集"用的是类的静态规格（目录信息在技能块里单列）
        names = [s.name for s in e.all_tool_specs()]
        check("skill.load" in names,
              "工具全集里也能看到 skill.load（用户得知道有这么一个工具）",
              str(names))

        tool = next(t for t in e.tools.all() if t.spec.name == "skill.load")
        from nl2sql.contracts.tool import Capability, ToolContext

        caps = tuple(Capability(name=n, granted_by="test")
                     for n in ("knowledge.read",))
        ctx = ToolContext(capabilities=caps, source_id="shop")
        ctx.provide("skill_catalog", e.skill_catalog)

        res = tool.run({"name": "demo"}, ctx)
        body = res.data if res.ok else ""
        check(res.ok and long_body in body,
              "★ 正文**完整**回灌，不被截断",
              f"got {len(body)} chars，含全文={long_body in body}")
        # 关键：走的是 observation 的**原样输出**分支，不过 _compact 的 1200 上限
        check("1. 第 1 步" in res.to_observation()
              and "59. 第 59 步" in res.to_observation(),
              "★ 回灌给模型的观察文本里首尾步骤都在（没被 _compact 截断）",
              f"obs {len(res.to_observation())} chars")

        res2 = tool.run({"name": "dmo"}, ctx)
        check(not res2.ok and "demo" in res2.candidates,
              "★ 名字写错时给出候选", f"{res2.error} | {res2.candidates}")

        res3 = tool.run({}, ctx)
        check(not res3.ok and res3.candidates,
              "缺参数时也给候选", str(res3.hint)[:60])

        # scope 不符的技能在**加载期**就被挡在目录外了（所以这里说"没有这个技能"）
        res4 = tool.run({"name": "scoped"}, ctx)
        check(not res4.ok and "没有这个技能" in str(res4.error),
              "★ 目录外的技能加载不到（加载期已按 scope 过滤）",
              str(res4.error)[:80])

        # 防御性的第二层：目录里有但当前数据源不符时，运行期也要拒
        from nl2sql.skills import LoadedSkillCatalog
        from nl2sql.contracts.skill import SkillSpec

        forced = LoadedSkillCatalog(_items=(SkillSpec(
            name="elsewhere-only", description="只对别的源",
            scope="other", body="x"),))
        ctx2 = ToolContext(capabilities=caps, source_id="shop")
        ctx2.provide("skill_catalog", forced)
        res5 = tool.run({"name": "elsewhere-only"}, ctx2)
        check(not res5.ok and "不适用" in str(res5.error),
              "★ 运行期二次校验 scope（防止目录被跨源复用）",
              str(res5.error)[:90])

        # 能力不足时工具不注册（复用既有机制：未授予的能力对应工具不注册）
        from nl2sql.plugins import ToolRegistry

        reg = ToolRegistry(granted=(), origin="core")
        try:
            reg.register(tool)
            check(False, "未授予 knowledge.read 时注册应被拒")
        except Exception as e:  # noqa: BLE001
            check("skill.load" not in reg.names(),
                  "★ 未授予 knowledge.read 时 skill.load 不注册",
                  f"{type(e).__name__}: {str(e)[:70]}")


# --------------------------------------------------------------------- E
def test_no_behavior_change(work: str) -> None:
    section("[E] 零技能配置下行为必须逐字不变（P0 的定位）")
    from nl2sql import NL2SQL, NL2SQLConfig

    cfg = NL2SQLConfig(state_dir=os.path.join(work, "state_e"))
    cfg.add_sqlite(GOLDEN_DB, source_id="shop")
    cfg.agent.planner = "rule"
    cfg.skills.dirs = (os.path.join(work, "empty_skills"),)
    cfg.skills.external_dirs = ()
    os.makedirs(cfg.skills.dirs[0], exist_ok=True)

    with NL2SQL(cfg) as e:
        e.refresh_snapshot()
        names = e.tools.names()
        check(len(names) == 9, "★ 零技能时仍是 9 个内置工具", str(len(names)))
        check("skill.load" not in names,
              "★ skill.load 不注册（没有技能时留在清单里只会白花一步）",
              str(names))
        # 端到端仍然可用
        out = e.ask("2024年6月，数码品类的实收金额是多少？", now="2024-06-30")
        check(out.ok, "端到端问答照常", f"{out.status} {out.message}")

    # 项目中真实存在的 skills/README.md 不该变成一条加载失败
    loader = _loader()
    loader.load(project_dirs=[os.path.join(ROOT, "skills")],
                external_dirs=[])
    check(not loader.report.rejected,
          "★ 仓库自带的 skills/README.md 不产生加载失败",
          str(loader.report.rejected))

    # 外部根里有 find-skills（本机已装）时，默认也不加载
    loader2 = _loader()
    cat2 = loader2.load(
        project_dirs=[os.path.join(ROOT, "skills")],
        external_dirs=[os.path.join(ROOT, ".agents", "skills")],
        source_id="shop",
    )
    check("find-skills" not in cat2.names(),
          "★ 真实存在于 .agents/skills 的 find-skills 默认不加载",
          str(cat2.names()))
    ext_skipped = {n: r for n, r in loader2.report.skipped}
    if os.path.isdir(os.path.join(ROOT, ".agents", "skills")):
        check("find-skills" in ext_skipped,
              "★ 但它出现在报告里（看得见、可点名启用）", str(ext_skipped))


def main() -> int:
    base = os.environ.get("NL2SQL_TEST_TMP") or os.path.join(
        ROOT, "data", "_test_p0b")
    work = os.path.join(base, "run")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    print(f"工作目录: {work}")

    # 供 HTTP 段使用的一份技能
    _write(os.path.join(work, "skills_http", "demo", "SKILL.md"),
           _skill_md("demo-skill", "HTTP 段用", ns="  scope: shop"))
    _write(os.path.join(work, "skills_http", "bad", "SKILL.md"),
           "---\nname: bad-skill\n---\n\n# x\n正文\n")

    try:
        test_discovery(work)
        test_trust(work)
        test_report_and_prompt(work)
        test_skill_load_tool(work)
        test_no_behavior_change(work)
    except Exception:
        traceback.print_exc()
        _failures.append("未捕获异常")

    shutil.rmtree(work, ignore_errors=True)
    print("\n" + "=" * 68)
    if _failures:
        print(f"P0b 验收未通过：{len(_failures)} 项失败")
        for x in _failures[:25]:
            print("  -", x)
        return 1
    print("P0b 验收全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
