"""混合策略验收：规则优先 + 模型兜底。

两部分，前者不需 Key，后者需要：
    [A] 离线行为：无模型时自动降级、有模型时兜底触发、来源标注、指纹不受影响
    [B] 实测对比：hybrid vs rule-only vs llm-only 的严格准确率

运行:
    python tests/test_s4c_hybrid.py          # 只跑 [A]
    $env:NL2SQL_LLM_API_KEY='sk-xxx'
    python tests/test_s4c_hybrid.py          # 跑 [A]+[B]
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_DB = os.path.join(ROOT, "golden_set", "ecommerce.db")
BASELINE = os.path.join(ROOT, "golden_set", "baseline.json")
NOW = "2024-06-30"

from nl2sql import NL2SQL, NL2SQLConfig  # noqa: E402
from nl2sql.contracts.llm import PlannerContext  # noqa: E402
from nl2sql.contracts.semantic import SemanticResolutionError  # noqa: E402
from nl2sql.llm import (  # noqa: E402
    HybridPlanner, LLMPlanner, MockLLM, NullLLM, OpenAICompatProvider,
    RulePlanner,
)
from nl2sql.semantics.golden_model import build_model  # noqa: E402

_failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        _failures.append(f"{label} {detail}")


# --------------------------------------------------------------- 结果比对
def _canon_cell(v):
    if v is None:
        return ("null",)
    s = str(v).strip()
    try:
        f = float(s)
    except (TypeError, ValueError):
        return ("str", s)
    return ("num", float(int(f)) if f == int(f) and abs(f) < 1e15 else round(f, 4))


def rows_match(expected, got, tol: float = 1e-6) -> bool:
    def sig(rows):
        out = []
        for r in rows:
            cells = [_canon_cell(v) for v in r]
            out.append((
                tuple(sorted(c[1] for c in cells if c[0] == "num")),
                tuple(sorted(c[1] for c in cells if c[0] == "str")),
            ))
        return sorted(out)

    a, b = sig(expected), sig(got)
    if len(a) != len(b):
        return False
    for (na, sa), (nb, sb) in zip(a, b):
        if sa != sb or len(na) != len(nb):
            return False
        if any(abs(x - y) > tol for x, y in zip(na, nb)):
            return False
    return True


# =============================================================== A. 离线行为
def test_offline(work: str) -> None:
    print("\n[A] 离线行为（无需 Key）")
    model = build_model(order_level=True)
    ctx = PlannerContext(source_id="shop", now=NOW)
    rule = RulePlanner(model)

    # 1) 无模型 → 自动降级为 rule
    cfg = NL2SQLConfig(state_dir=os.path.join(work, "s1"))
    cfg.add_sqlite(GOLDEN_DB, source_id="shop")
    cfg.agent.planner = "auto"
    with NL2SQL(cfg, semantic_model=build_model(order_level=True)) as e:
        check(e.planner_mode == "rule", "auto 且无模型 → 降级为 rule",
              e.planner_mode)

    # 2) hybrid 但无模型 → 仍可工作（退化为 rule）
    cfg2 = NL2SQLConfig(state_dir=os.path.join(work, "s2"))
    cfg2.add_sqlite(GOLDEN_DB, source_id="shop")
    cfg2.agent.planner = "hybrid"
    with NL2SQL(cfg2, semantic_model=build_model(order_level=True),
                llm_provider=NullLLM()) as e2:
        check(e2.planner_mode == "rule(hybrid 降级)", "hybrid 无模型时明确降级",
              e2.planner_mode)
        out = e2.ask("2024年6月，数码品类的实收金额是多少？", now=NOW)
        check(out.ok, "hybrid 无模型仍能答出语义层已覆盖的问题", out.message or "")

    # 3) 规则能答 → 不调用模型（关键：省钱、保持确定性）
    mock = MockLLM(["这条不该被用到"])
    hybrid = HybridPlanner(rule, LLMPlanner(mock, model))
    plan = hybrid.resolve("2024年6月，数码品类的实收金额是多少？", ctx)
    check(len(mock.calls) == 0, "★ 规则命中时不调用模型（零成本）",
          f"实际调用 {len(mock.calls)} 次")
    check(any("规则" in n for n in plan.notes), "来源标注为规则",
          str(plan.notes))
    check(hybrid.stats["rule"] == 1 and hybrid.stats["llm"] == 0, "统计计入规则")

    # 4) 规则答不出 → 模型兜底；来源标注为模型
    good = json.dumps({
        "measures": [{"metric": "net_revenue", "alias": "实收金额"}],
        "dimensions": [{"dimension": "category", "alias": "品类"}],
        "filters": [], "time_range": None, "order_by": [], "limit": None,
    }, ensure_ascii=False)
    mock2 = MockLLM([good])
    hybrid2 = HybridPlanner(rule, LLMPlanner(mock2, model))
    # 这里必须用一个**规则规划器现在仍然答不出**的形态。
    # 原来用的是"实收金额超过20000元的品类"（HAVING）——S3c 把它做进语义层之后
    # 规则规划器能自己答了，于是模型不再被调用。那是能力提升，不是回归；
    # 测试要改的是"用哪个问题来触发兜底"，而不是把能力退回去。
    # 现在选 F1 的形态：与全局平均值的标量子查询比较（qualify 只支持字面量）。
    plan2 = hybrid2.resolve(
        "2024年上半年，消费金额高于全部客户平均消费金额的客户有哪些？", ctx)
    check(len(mock2.calls) == 1, "★ 规则拒绝时才调用模型（兜底）",
          f"实际调用 {len(mock2.calls)} 次")
    check(any("模型兜底" in n for n in plan2.notes), "来源标注为模型兜底",
          str(plan2.notes))
    check(hybrid2.stats["llm"] == 1, "统计计入模型")

    # 4b) 已被语义层覆盖的形态**不再**调用模型（这条断言防止能力悄悄退化）
    mock2b = MockLLM([good])
    hybrid2b = HybridPlanner(rule, LLMPlanner(mock2b, model))
    for q in ("2024年上半年，实收金额超过20000元的商品品类有哪些？",
              "本月（2024年6月）和上月（2024年5月）的实收金额分别是多少？",
              "2024年上半年，每月实收金额的累计值是多少？",
              "2024年上半年，各商品品类内部销量前2的商品是哪些？"):
        try:
            hybrid2b.resolve(q, ctx)
        except SemanticResolutionError:
            pass
    check(len(mock2b.calls) == 0,
          "★ HAVING / 跨期对比 / 累计 / 组内前N 都由规则规划器直接答，不花钱问模型",
          f"实际调用 {len(mock2b.calls)} 次")

    # 5) 两条路都失败 → 报规则的原因（更贴近语义层缺口）
    hybrid3 = HybridPlanner(rule, LLMPlanner(MockLLM(["这不是 JSON"]), model))
    try:
        hybrid3.resolve("帮我看看最近怎么样", ctx)
        check(False, "两条路都失败应抛错")
    except SemanticResolutionError as e:
        check("识别出任何指标或维度" in str(e),
              "两条都失败时上报规则规划器的原因", str(e)[:60])
        check(hybrid3.stats["failed"] == 1, "统计计入失败")

    # 6) 来源标注不进指纹（缓存按语义命中，不按产出路径）
    p_rule = rule.resolve("2024年6月，数码品类的实收金额是多少？", ctx)
    p_tagged = hybrid.resolve("2024年6月，数码品类的实收金额是多少？", ctx)
    check(p_rule.fingerprint() == p_tagged.fingerprint(),
          "★ 加来源标注不改变计划指纹（缓存仍可命中）",
          f"{p_rule.fingerprint()[:12]} vs {p_tagged.fingerprint()[:12]}")

    # 8) 严格模式：列无明文依据时拒绝作答（安全护栏）
    strict = RulePlanner(model, strict=True)

    # (a) 正常问题（有分组词"各"）不应被拒
    try:
        p_ok = strict.resolve("2024年上半年，各渠道的实收金额是多少？", ctx)
        check(bool(p_ok.measures), "严格模式放行有明文依据的问题")
    except SemanticResolutionError as e:
        check(False, "严格模式误拒了有依据的问题", str(e)[:70])

    # (b) 直接验证判据：分组维度 + 无分组词 → 判为无依据
    bad = strict._unjustified_columns(
        "华东地区的实收金额是多少？", ["net_revenue"], ["region"])
    check("维度:region" in bad, "★ 判据：无分组词时维度被判无依据", str(bad))

    # (c) 判据对照：有分组词时同一维度有依据
    good = strict._unjustified_columns(
        "各地区的实收金额是多少？", ["net_revenue"], ["region"])
    check("维度:region" not in good, "★ 判据对照：有分组词时维度有依据", str(good))

    # (d) 判据：未被点名的派生指标 → 无依据
    bad2 = strict._unjustified_columns(
        "上半年的退款情况如何？", ["refund_rate"], [])
    check("指标:refund_rate" in bad2,
          "★ 判据：未被点名的派生指标被判无依据", str(bad2))

    # (e) 基础指标用同义词命中时不应被误杀
    ok2 = strict._unjustified_columns(
        "客户有多少人？", ["customer_count"], [])
    check(not ok2, "★ 判据：基础指标不被误杀（用户可能用同义词）", str(ok2))

    # (f) 宽松模式对同一问题照常作答（strict 是附加约束，不替代默认行为）
    p_loose = rule.resolve("华东地区的实收金额是多少？", ctx)
    check(bool(p_loose.measures), "宽松模式下同一问题可作答（strict 为附加约束）")

    # 9) describe 不泄露密钥
    prov = OpenAICompatProvider(api_key="sk-secret-xyz", model="deepseek-chat")
    d = HybridPlanner(rule, LLMPlanner(prov, model)).describe()
    check("sk-secret-xyz" not in json.dumps(d), "★ describe() 不回显密钥",
          json.dumps(d, ensure_ascii=False))
    check(d["llm_available"] is True, "describe() 报告模型可用性")


# =============================================================== B. 实测对比
def run_mode(mode: str, provider, cases, work: str) -> tuple[dict[str, bool], dict]:
    model = build_model(order_level=True)
    db = os.path.join(work, f"{mode}.db")
    shutil.copy2(GOLDEN_DB, db)
    cfg = NL2SQLConfig(state_dir=os.path.join(work, f"st_{mode}"))
    cfg.add_sqlite(db, source_id="shop")
    cfg.agent.planner = mode

    result: dict[str, bool] = {}
    with NL2SQL(cfg, semantic_model=model, llm_provider=provider) as engine:
        engine.refresh_snapshot()
        for c in cases:
            out = engine.ask(c["question"], now=NOW, use_cache=False)
            result[c["id"]] = bool(
                out.ok and out.result is not None
                and rows_match(c["rows"], out.result.rows)
            )
            if mode != "rule":
                time.sleep(0.3)
        stats = {}
        planner = engine._agent._planner
        if isinstance(planner, HybridPlanner):
            stats = dict(planner.stats)
    return result, stats


def test_live(work: str) -> None:
    key = os.environ.get("NL2SQL_LLM_API_KEY", "")
    if not key:
        print("\n[B] 实测对比：跳过（未设置 NL2SQL_LLM_API_KEY）")
        return

    print("\n[B] 实测对比（真实模型）")
    with open(BASELINE, encoding="utf-8") as f:
        cases = json.load(f)["cases"]
    provider = OpenAICompatProvider(
        api_key=key,
        base_url=os.environ.get("NL2SQL_LLM_BASE_URL",
                                "https://api.deepseek.com/v1"),
        model=os.environ.get("NL2SQL_LLM_MODEL", "deepseek-chat"),
    )
    total = len(cases)

    print("  跑 rule-only ...")
    rule_res, _ = run_mode("rule", provider, cases, work)
    print("  跑 llm-only ...")
    llm_res, _ = run_mode("llm", provider, cases, work)
    print("  跑 hybrid ...")
    hyb_res, hyb_stats = run_mode("hybrid", provider, cases, work)

    def n(d):
        return sum(d.values())

    print(f"\n  rule-only   {n(rule_res)}/{total} = {n(rule_res)/total:.1%}")
    print(f"  llm-only    {n(llm_res)}/{total} = {n(llm_res)/total:.1%}")
    print(f"  hybrid      {n(hyb_res)}/{total} = {n(hyb_res)/total:.1%}")
    print(f"  hybrid 兜底统计: {hyb_stats}")
    if hyb_stats:
        r, l = hyb_stats.get("rule", 0), hyb_stats.get("llm", 0)
        print(f"  → 规则处理 {r}/{total}（零延迟零成本），模型兜底 {l}/{total}")

    # ---- 验收标准（按实测事实设定，不按期望设定）----
    # 实测结论：hybrid 是**速度/成本优化**，不是准确率提升。
    #   rule-only 17/30、llm-only 18/30、hybrid 17/30
    #   但 hybrid 把 25/30 的问题留在零延迟零成本的规则路径上。
    # 因此断言"调用次数大幅下降"，而不是断言"准确率更高"——
    # 后者在当前实现下不成立，硬写会变成自欺。
    r, l = hyb_stats.get("rule", 0), hyb_stats.get("llm", 0)
    llm_calls_saved = total - l
    check(l <= max(5, total // 4),
          f"★ 模型调用大幅下降（规则处理 {r}/{total}）",
          f"模型兜底 {l} 次")
    check(n(hyb_res) >= n(rule_res),
          "hybrid 不劣于 rule-only",
          f"hybrid={n(hyb_res)} rule={n(rule_res)}")
    print(f"  → 相比 llm-only 的 {total} 次调用，hybrid 只用了 {l} 次"
          f"（省 {llm_calls_saved} 次，{llm_calls_saved/total:.0%}）")
    if n(hyb_res) < n(llm_res):
        print(f"  ! 注意：hybrid 准确率低于 llm-only（{n(hyb_res)} vs {n(llm_res)}），"
              f"原因是规则规划器会'错误地自信'——")
        print( "    它对某些问题给出看似正常但答非所问的结果，不抛 UNRESOLVED，")
        print( "    因此永远不会触发兜底。这是当前方案的已知缺口。")


def main() -> int:
    base = os.environ.get("NL2SQL_TEST_TMP") or os.path.join(ROOT, "data", "_test_s4c")
    work = os.path.join(base, "run")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    print(f"工作目录: {work}")

    try:
        test_offline(work)
        test_live(work)
    except Exception:
        traceback.print_exc()
        _failures.append("未捕获异常")

    shutil.rmtree(work, ignore_errors=True)
    print("\n" + "=" * 64)
    if _failures:
        print(f"混合策略验收未通过：{len(_failures)} 项失败")
        for x in _failures[:20]:
            print("  -", x)
        return 1
    print("混合策略验收全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
