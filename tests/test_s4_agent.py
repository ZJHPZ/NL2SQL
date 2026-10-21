"""S4 验收：规划器 + 编排链路端到端。

验收标准（docs/02 第 15 节）：
    **`evaluate.py` 出第一份准确率** —— 让规划器从语义模型出发自己产出逻辑计划。

本测试给出两条数字，且刻意分开：
    [A] 规则规划器   —— 衡量**语义层覆盖率**（与模型能力解耦的基准线）
    [B] LLM 规划器（Mock）—— 验证模型驱动的链路能跑通、能校验、能降级

分开报告的意义：有了基准线，"模型带来多少增益"才是可量化的，而不是感觉。

运行:
    python tests/test_s4_agent.py
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
BASELINE = os.path.join(ROOT, "golden_set", "baseline.json")

from nl2sql import NL2SQL, NL2SQLConfig  # noqa: E402
from nl2sql.contracts.llm import PlannerContext  # noqa: E402
from nl2sql.contracts.semantic import SemanticResolutionError  # noqa: E402
from nl2sql.llm import LLMPlanner, MockLLM, NullLLM, RulePlanner, build_provider  # noqa: E402
from nl2sql.llm.planners import parse_time_range  # noqa: E402
from nl2sql.semantics.golden_model import build_model  # noqa: E402

_failures: list[str] = []
NOW = "2024-06-30"


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        _failures.append(f"{label} {detail}")


# --------------------------------------------------------------- 结果比对
def is_num(s: object) -> bool:
    try:
        float(str(s))
        return True
    except (TypeError, ValueError):
        return False


def canon_cell(v: object) -> tuple:
    if v is None:
        return ("null",)
    s = str(v).strip()
    if is_num(s):
        fv = float(s)
        return ("num", float(int(fv)) if fv == int(fv) and abs(fv) < 1e15 else round(fv, 4))
    return ("str", s)


def canon_rows(rows):
    out = []
    for r in rows:
        cells = [canon_cell(v) for v in r]
        nums = tuple(sorted(c[1] for c in cells if c[0] == "num"))
        strs = tuple(sorted(c[1] for c in cells if c[0] == "str"))
        out.append((nums, strs))
    return sorted(out)


def rows_match(expected, got, tol: float = 1e-6) -> bool:
    a, b = canon_rows(expected), canon_rows(got)
    if len(a) != len(b):
        return False
    for (na, sa), (nb, sb) in zip(a, b):
        if sa != sb or len(na) != len(nb):
            return False
        if any(abs(x - y) > tol for x, y in zip(na, nb)):
            return False
    return True


def load_cases() -> list[dict]:
    with open(BASELINE, encoding="utf-8") as f:
        return json.load(f)["cases"]


# --------------------------------------------------------------- A. 规则规划器
def test_rule_planner(work: str) -> dict:
    print("\n[A] 规则规划器：语义层覆盖率（无模型）")
    cases = load_cases()
    model = build_model(order_level=True)
    planner = RulePlanner(model)
    ctx = PlannerContext(source_id="shop", now=NOW)

    parsed = 0
    correct = 0
    details: list[tuple[str, str, str]] = []

    db = os.path.join(work, "rule.db")
    shutil.copy2(GOLDEN_DB, db)

    cfg = NL2SQLConfig(state_dir=os.path.join(work, "state_rule"))
    cfg.add_sqlite(db, source_id="shop")
    cfg.agent.planner = "rule"

    with NL2SQL(cfg, semantic_model=model) as engine:
        engine.refresh_snapshot()
        for c in cases:
            qid, question = c["id"], c["question"]
            out = engine.ask(question, now=NOW, use_cache=False)
            if out.error_code == "UNRESOLVED":
                details.append((qid, "未解析", (out.notes[-1] if out.notes else "")[:56]))
                continue
            parsed += 1
            if out.ok and out.result is not None and rows_match(c["rows"], out.result.rows):
                correct += 1
                details.append((qid, "正确", f"{out.result.row_count} 行"))
            else:
                reason = out.message or out.error_code or "结果不符"
                got = ([list(r) for r in out.result.rows[:1]]
                       if out.result and out.result.rows else [])
                details.append((qid, "错误",
                                f"{reason[:40]} 实际={got}"))

    total = len(cases)
    print(f"\n  解析成功 {parsed}/{total}　结果正确 {correct}/{total}"
          f"　语义层覆盖率 {parsed/total:.1%}　端到端准确率 {correct/total:.1%}")
    print("\n  逐题：")
    for qid, status, note in details:
        print(f"    {qid:3s} {status:4s} {note}")

    unresolved = [d for d in details if d[1] == "未解析"]
    wrong = [d for d in details if d[1] == "错误"]
    print(f"\n  未解析 {len(unresolved)} 题: {[d[0] for d in unresolved]}")
    print(f"  解析但结果错 {len(wrong)} 题: {[d[0] for d in wrong]}")

    return {"total": total, "parsed": parsed, "correct": correct,
            "unresolved": [d[0] for d in unresolved],
            "wrong": [d[0] for d in wrong]}


# --------------------------------------------------------------- 时间解析
def test_time_parsing() -> None:
    print("\n[B] 相对时间锚定（必须由 now 决定，不读系统时钟）")
    cases = [
        ("2024年6月，数码品类的实收金额是多少？", "2024-06-01", "2024-06-30"),
        ("本月各渠道实收金额", "2024-06-01", "2024-06-30"),
        ("上月各渠道实收金额", "2024-05-01", "2024-05-31"),
        ("2024年上半年各地区销售额", "2024-01-01", "2024-06-30"),
        ("2024年下半年各地区销售额", "2024-07-01", "2024-12-31"),
        ("去年全年销售额", "2023-01-01", "2023-12-31"),
        ("2024年6月1日的订单数", "2024-06-01", "2024-06-01"),
    ]
    for q, want_start, want_end in cases:
        tr = parse_time_range(q, NOW)
        got = (tr.start, tr.end) if tr else (None, None)
        check(got == (want_start, want_end), f"{q[:26]}", f"期望 {(want_start, want_end)} 实际 {got}")

    # 换锚点必须换结果（否则说明读了系统时间）
    tr2 = parse_time_range("本月各渠道实收金额", "2024-03-15")
    check(tr2 is not None and tr2.start == "2024-03-01" and tr2.end == "2024-03-31",
          "锚点改为 2024-03-15 后本月变为 3 月", f"实际 {tr2}")

    tr3 = parse_time_range("本月各渠道实收金额", None)
    check(tr3 is None or tr3.start is None,
          "无锚点时不做相对时间猜测（宁可不解析也不猜）", f"实际 {tr3}")


# --------------------------------------------------------------- C. LLM 规划器
def test_llm_planner() -> None:
    print("\n[C] LLM 规划器（Mock 模型，验证链路而非模型能力）")
    model = build_model(order_level=True)
    ctx = PlannerContext(source_id="shop", now=NOW)

    good_json = json.dumps({
        "measures": [{"metric": "net_revenue", "alias": "实收金额"}],
        "dimensions": [{"dimension": "channel", "alias": "渠道"}],
        "filters": [],
        "time_range": {"start": "2024-01-01", "end": "2024-06-30", "grain": "month"},
        "order_by": [{"field": "实收金额", "desc": True}, {"field": "渠道", "desc": False}],
        "limit": None,
        "notes": [],
    }, ensure_ascii=False)

    # 1) 正常路径（含 markdown 围栏，模拟真实模型输出）
    mock = MockLLM(["```json\n" + good_json + "\n```"])
    planner = LLMPlanner(mock, model)
    plan = planner.resolve("上半年各渠道实收金额", ctx)
    check(len(plan.measures) == 1 and plan.measures[0].metric == "net_revenue",
          "解析指标")
    check(plan.dimensions[0].dimension == "channel", "解析维度")
    check(plan.time_range is not None and plan.time_range.start == "2024-01-01",
          "解析时间范围")
    check(plan.fingerprint() and len(plan.fingerprint()) == 32, "产出可指纹计划")

    # 2) 编造指标必须被拦下，而不是让下游编译时才炸
    bad = json.dumps({"measures": [{"metric": "不存在的指标"}], "dimensions": []},
                     ensure_ascii=False)
    try:
        LLMPlanner(MockLLM([bad]), model).resolve("乱问", ctx)
        check(False, "编造指标应被拒绝")
    except SemanticResolutionError as e:
        check("不存在的指标" in str(e), "编造指标被拒绝", str(e))

    # 3) 输出不是 JSON
    try:
        LLMPlanner(MockLLM(["我觉得应该查一下销售数据"]), model).resolve("x", ctx)
        check(False, "非 JSON 输出应被拒绝")
    except SemanticResolutionError as e:
        check("无法解析" in str(e) or "JSON" in str(e), "非 JSON 输出被拒绝", str(e))

    # 4) 模型不可用时明确报错（由上层降级）
    try:
        LLMPlanner(NullLLM(), model).resolve("x", ctx)
        check(False, "NullLLM 应报不可用")
    except SemanticResolutionError as e:
        check("不可用" in str(e), "模型不可用时明确报错", str(e))

    # 5) Prompt 里不得出现物理表名（语义层应屏蔽物理细节）
    mock2 = MockLLM([good_json])
    LLMPlanner(mock2, model).resolve("上半年各渠道实收金额", ctx)
    sent = "\n".join(m.content for m in mock2.calls[0])
    check("fact_order" not in sent and "dim_channel" not in sent,
          "★ Prompt 中不含物理表名（模型面对业务概念而非 JOIN 路径）")
    check("net_revenue" in sent and "channel" in sent, "Prompt 含指标与维度清单")


# --------------------------------------------------------------- D. 端到端与降级
def test_pipeline_and_fallback(work: str) -> None:
    print("\n[D] 编排链路：降级、缓存、历史、错误反馈")
    db = os.path.join(work, "pipe.db")
    shutil.copy2(GOLDEN_DB, db)

    cfg = NL2SQLConfig(state_dir=os.path.join(work, "state_pipe"))
    cfg.add_sqlite(db, source_id="shop")
    cfg.agent.planner = "rule"

    with NL2SQL(cfg) as engine:
        engine.refresh_snapshot()

        # 1) 正常问答
        out = engine.ask("2024年6月，数码品类的实收金额是多少？", now=NOW)
        check(out.status == "ok" and out.result is not None, "正常问答返回结果",
              f"status={out.status} {out.message}")
        check(out.sql is not None and "SELECT" in (out.sql or ""), "返回 SQL")
        check(out.plan_fingerprint is not None, "返回计划指纹")
        check("plan_ms" in out.trace, "返回各阶段耗时（trace）")

        # 2) 缓存命中
        out2 = engine.ask("2024年6月，数码品类的实收金额是多少？", now=NOW)
        check(out2.cached, "二次相同问题命中缓存")
        check(any("缓存" in n for n in out2.notes), "命中缓存有提示")

        # 3) 换说法但计划相同 → 仍应命中（指纹缓存的价值）
        out3 = engine.ask("2024年6月 数码品类 实收金额", now=NOW)
        check(out3.cached, "★ 换说法但逻辑计划相同仍命中缓存")

        # 4) 无法解析的问题 → 明确 unresolved，不编造
        out4 = engine.ask("帮我看看最近怎么样", now=NOW)
        check(out4.status == "unresolved", "无法解析时返回 unresolved",
              f"实际 {out4.status}")
        check(bool(out4.suggestions), "给出可操作建议")
        check(out4.sql is None, "不返回 SQL（不编造查询）")

        # 5) 结果为空时回显过滤条件
        out5 = engine.ask("2024年1月，华南地区的图书实收金额是多少？", now=NOW)
        if out5.status == "empty":
            check(bool(out5.applied_filters), "空结果回显生效的过滤条件",
                  str(out5.applied_filters))
        else:
            check(out5.ok, "该查询可执行", f"status={out5.status} {out5.message}")

        # 6) 历史落盘
        hist = engine.history(limit=20)
        check(len(hist) >= 4, "查询历史已落盘", f"实际 {len(hist)}")
        check(all(h.plan_fingerprint is not None for h in hist),
              "历史含计划指纹（可回放）")
        check(any(h.meta.get("planner") == "rule" for h in hist),
              "历史记录了所用规划器")

        # 7) 缓存按数据源隔离
        stats = engine._cache.stats()
        check("by_source" in stats and "shop" in stats.get("by_source", {}),
              "缓存统计按数据源区分", str(stats))


# --------------------------------------------------------------- E. 契约
def test_contracts() -> None:
    print("\n[E] 模型提供者装配（不读环境变量）")
    p = build_provider(api_key=None)
    check(not p.available(), "无 key 时返回不可用提供者（NullLLM）")
    check(type(p).__name__ == "NullLLM", "返回 NullLLM 而非抛异常")

    p2 = build_provider(api_key="sk-test", vendor="deepseek")
    check(type(p2).__name__ == "OpenAICompatProvider", "有 key 时返回兼容提供者")
    check(p2.base_url == "https://api.deepseek.com/v1", "DeepSeek 默认端点正确",
          p2.base_url)
    check(p2.model == "deepseek-chat", "DeepSeek 默认模型正确", p2.model)
    check(p2.available(), "配置齐全时可调用")

    p3 = build_provider(api_key="sk-test", vendor="dashscope")
    check("dashscope" in p3.base_url, "换厂商只改 base_url", p3.base_url)

    d = p2.describe()
    check(d["has_key"] and "sk-test" not in json.dumps(d),
          "★ describe() 不回显密钥")


def main() -> int:
    base = os.environ.get("NL2SQL_TEST_TMP") or os.path.join(ROOT, "data", "_test_s4")
    work = os.path.join(base, "run")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    print(f"工作目录: {work}")

    try:
        stats = test_rule_planner(work)
        test_time_parsing()
        test_llm_planner()
        test_pipeline_and_fallback(work)
        test_contracts()
    except Exception:
        traceback.print_exc()
        _failures.append("未捕获异常")
        stats = {}

    shutil.rmtree(work, ignore_errors=True)

    print("\n" + "=" * 64)
    if stats:
        print(f"规则规划器（语义层覆盖率基准线）: "
              f"解析 {stats['parsed']}/{stats['total']}　"
              f"正确 {stats['correct']}/{stats['total']}"
              f" = {stats['correct']/max(1,stats['total']):.1%}")
    if _failures:
        print(f"\nS4 验收未通过：{len(_failures)} 项失败")
        for x in _failures[:25]:
            print("  -", x)
        return 1
    print("S4 验收全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
