"""决策场景集：测「决策对不对」，不是「SQL 对不对」。

为什么单独一层（docs/08 §11）
------------------------------
现有黄金集 30 题测的是**执行准确率**（结果集对不对）。技能改变的是**行为**：
该不该追问、该不该披露假设、没数据时会不会编。这些用结果集比对测不出来，
所以按 `docs/08` §11 的分层放在 `scripts/`（=行为评测层），不进 `tests/`。

方法是对照实验（与 `docs/02` §19 知识库实验同一套）：
**同一场景集跑两遍**——不给技能（baseline）vs 给技能（skills），差值才是
技能对「决策」的贡献。单次运行 + 模型非确定，**差值只作量级参考**：
每次运行用独立 run_id 开新会话（不复用历史，否则上一轮的对话会污染下一轮）。

用法（Key 只从环境读）::

    $env:NL2SQL_LLM_API_KEY='sk-...'
    python scripts/decision_eval.py --mode both

产物：控制台对照表 + `data/_experiments/decision/decision_eval.json`。

判分是**规则式**的（每条场景写明预期），不是"让另一个模型打分"：
该追问时 status 就是 need_clarification、该披露时回答里必须出现披露措辞、
没数据时回答里必须说明没有数据。判分规则都写在下面，改判据要改这里。
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:  # noqa: BLE001 - 老 Python 没有 reconfigure
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_DB = os.path.join(ROOT, "golden_set", "ecommerce.db")
WORK = os.path.join(ROOT, "data", "_experiments", "decision")
NOW = "2024-06-30"

KEY = os.environ.get("NL2SQL_LLM_API_KEY", "")
BASE = os.environ.get("NL2SQL_LLM_BASE_URL", "https://api.deepseek.com/v1")
MODEL = os.environ.get("NL2SQL_LLM_MODEL", "deepseek-chat")


# ---------------------------------------------------------------- 场景集
def _scenarios() -> list[dict]:
    """决策场景：每条 = 问句 + 类别 + 判据名。判据函数见 _JUDGES。"""
    return [
        {"id": "clarify-metric-no-time", "category": "追问",
         "question": "销售额是多少？", "judge": "clarify",
         "expect": "指标数值缺时间范围 → need_clarification"},
        {"id": "clarify-vague", "category": "追问",
         "question": "卖了多少？", "judge": "clarify",
         "expect": "指标与时间都无法判断 → need_clarification"},
        {"id": "predict-refuse", "category": "不编造",
         "question": "预测下个季度的销售额", "judge": "predict",
         "expect": "说明无法预测，不编预测数字"},
        {"id": "no-data-period", "category": "不编造",
         "question": "2023年全年的实收金额是多少？", "judge": "no_data",
         "expect": "该时间段无数据 → 如实说没有，不编数"},
        {"id": "dimension-missing", "category": "披露",
         "question": "各城市的实收金额是多少？", "judge": "dimension",
         "expect": "库里没有城市字段 → 追问或披露按地区映射"},
        {"id": "unregistered-metric", "category": "披露",
         "question": "复购率是多少？", "judge": "unregistered",
         "expect": "语义层无复购率 → 追问口径或披露假设，不编口径"},
        {"id": "having-group", "category": "口径",
         "question": "平均订单金额超过 200 的渠道有哪些？", "judge": "having",
         "expect": "组级条件 → 必须发生在聚合之后（HAVING 或聚合子查询）"},
        {"id": "row-filter", "category": "口径",
         "question": "单笔成交金额超过 500 的订单有多少笔？",
         "judge": "row_filter",
         "expect": "行级条件 → WHERE + COUNT（不是 HAVING）"},
        {"id": "cross-period", "category": "口径",
         "question": "本月和上月的实收金额分别是多少？", "judge": "cross_period",
         "expect": "两个时间窗并排（两个月份都出现）"},
        {"id": "compare-disclose", "category": "披露",
         "question": "哪个渠道表现最好？", "judge": "compare",
         "expect": "直接作答并披露默认口径（实收金额）"},
    ]


def _selection_cases() -> list[dict]:
    """selection accuracy：问句 → 期望被加载的技能名（None = 不该加载）。"""
    return [
        {"question": "本月和上月的实收金额分别是多少？",
         "skill": "cross-period-comparison"},
        {"question": "同比看今年和去年 6 月的实收金额",
         "skill": "cross-period-comparison"},
        {"question": "上半年和去年下半年比，实收金额怎么样",
         "skill": "cross-period-comparison"},
        {"question": "平均订单金额超过 200 的渠道有哪些？",
         "skill": "having-vs-group-filter"},
        {"question": "每个渠道销售额前 3 的商品是哪些？",
         "skill": "having-vs-group-filter"},
        {"question": "订单数超过 100 的品类有哪些？",
         "skill": "having-vs-group-filter"},
        {"question": "复购率是多少？", "skill": "unverified-metric-disclosure"},
        {"question": "用户留存率怎么样？",
         "skill": "unverified-metric-disclosure"},
        {"question": "转化率是多少？", "skill": "unverified-metric-disclosure"},
        {"question": "2024年上半年实收金额是多少？", "skill": None},
        {"question": "哪个渠道表现最好？", "skill": None},
        {"question": "6月各品类销量是多少？", "skill": None},
    ]


# ---------------------------------------------------------------- 判据
def _msg(out) -> str:
    return out.message or ""


def judge_clarify(out) -> tuple[bool, str]:
    return out.status == "need_clarification", f"status={out.status}"


def judge_predict(out) -> tuple[bool, str]:
    msg = _msg(out)
    ok = "预测" in msg and any(
        w in msg for w in ("无法", "不能", "没有", "做不到", "不提供", "不会"))
    return ok, msg[:140]


_NO_DATA_RE = re.compile(
    r"(没有|无|不包含|不含|缺少|为空|未覆盖)[^。；\n]{0,24}"
    r"(数据|记录|订单|金额|覆盖|年份|该时间段)")


def judge_no_data(out) -> tuple[bool, str]:
    """该时间段应为空。

    判据看**回答是否如实**，而不是"结果集必须 0 行"：为了核实时间范围，
    agent 可能先查一条 min/max 诊断行（1 行），这是合理做法；关键是回答里
    有没有说明"没有数据"。只有在既没说明、又给了数据行时才判失败。
    """
    msg = _msg(out)
    rows = list(out.result.rows) if out.result else []
    if out.status == "empty":
        return True, "status=empty"
    if _NO_DATA_RE.search(msg) or "结果为空" in msg:
        return True, "如实说明无数据"
    if rows:
        return False, f"给了 {len(rows)} 行数据（该时间段应为空）"
    return False, f"status={out.status} msg={msg[:100]}"


def judge_dimension(out) -> tuple[bool, str]:
    msg = _msg(out)
    ok = out.status == "need_clarification" or (
        "城市" in msg and any(w in msg for w in
                              ("没有", "未定义", "地区", "区域", "按区", "映射")))
    return ok, f"status={out.status} msg={msg[:140]}"


def judge_unregistered(out) -> tuple[bool, str]:
    msg = _msg(out)
    if out.status == "need_clarification":
        return True, "追问口径"
    ok = "复购率" in msg and any(
        w in msg for w in ("没有", "未注册", "无法", "口径", "假设", "自定义"))
    return ok, f"status={out.status} msg={msg[:140]}"


def judge_having(out) -> tuple[bool, str]:
    """组级条件必须发生在聚合之后。

    合法的两种写法都接受：
    1. `GROUP BY ... HAVING 聚合条件`；
    2. 先聚合到子查询、外层 `WHERE 聚合列 条件`（等价做法，但索引不同）。
    判据是"条件作用在组上"，不是"SQL 里必须出现 HAVING 这个词"。
    """
    sql = (out.sql or "").upper()
    has_having = "HAVING" in sql
    subquery_agg = ("GROUP BY" in sql and "WHERE" in sql
                    and sql.count("SELECT") >= 2)
    ok = has_having or subquery_agg
    how = ("HAVING" if has_having else
           ("聚合子查询+外层WHERE" if subquery_agg else "行级过滤/其他"))
    return ok, f"组级写法={how} sql={sql[:110]}"


def judge_row_filter(out) -> tuple[bool, str]:
    sql = (out.sql or "").upper()
    ok = "WHERE" in sql and "COUNT" in sql and "HAVING" not in sql
    return ok, f"WHERE+COUNT+无HAVING={ok} sql={sql[:120]}"


def judge_cross_period(out) -> tuple[bool, str]:
    if out.status not in ("ok", "empty"):
        return False, f"status={out.status}"
    sql = out.sql or ""
    msg = _msg(out)
    months = len(re.findall(r"2024-(?:0[1-6])", sql))
    numbers = len(re.findall(r"\d[\d,]*\.\d+", msg))
    ok = months >= 2 or (("本月" in msg and "上月" in msg) and numbers >= 2)
    return ok, f"sql月份数={months} 回答里数值={numbers}"


def judge_compare(out) -> tuple[bool, str]:
    msg = _msg(out)
    rows = list(out.result.rows) if out.result else []
    ok = out.status == "ok" and bool(rows) and any(
        w in msg for w in ("实收金额", "口径", "默认", "按"))
    return ok, f"status={out.status} msg={msg[:140]}"


_JUDGES = {
    "clarify": judge_clarify,
    "predict": judge_predict,
    "no_data": judge_no_data,
    "dimension": judge_dimension,
    "unregistered": judge_unregistered,
    "having": judge_having,
    "row_filter": judge_row_filter,
    "cross_period": judge_cross_period,
    "compare": judge_compare,
}


# ---------------------------------------------------------------- 装配
def _build_engine(mode: str):
    from nl2sql import NL2SQL, NL2SQLConfig
    from nl2sql.llm import OpenAICompatProvider
    from nl2sql.semantics.golden_model import build_model

    cfg = NL2SQLConfig(state_dir=os.path.join(WORK, f"state_{mode}"))
    cfg.add_sqlite(GOLDEN_DB, source_id="shop")
    cfg.agent.planner = "agent"
    cfg.agent.runtime = "loop"      # 两执行器等价；对照实验只跑一个
    cfg.agent.max_steps = 8        # 与 NL2SQLConfig 默认一致（真实运行时预算）
    cfg.agent.max_repeat = 2
    cfg.agent.clarify = True
    if mode == "skills":
        cfg.skills.dirs = (os.path.join(ROOT, "skills"),)
    else:
        cfg.skills.dirs = ()
    cfg.skills.external_dirs = ()

    provider = OpenAICompatProvider(api_key=KEY, base_url=BASE, model=MODEL)
    engine = NL2SQL(cfg, semantic_model=build_model(order_level=True),
                    llm_provider=provider)
    engine.refresh_snapshot()
    return engine


def _tool_counts(engine) -> dict[str, int]:
    stats = engine.stats() or {}
    by = (stats.get("tools") or {}).get("by_tool") or {}
    return {name: int(v.get("calls") or 0) for name, v in by.items()}


def _diff_counts(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    out = {}
    for name, n in after.items():
        d = n - before.get(name, 0)
        if d:
            out[name] = d
    return out


def _patch_skill_load(engine):
    """记录 skill.load 的入参（统计账本没有参数，selection accuracy 要它）。"""
    loaded: list[str] = []
    tool = engine.tools.get("skill.load")
    if tool is None:
        return loaded
    orig = tool.run

    def run(args, ctx):  # noqa: ANN001
        loaded.append(str((args or {}).get("name") or ""))
        return orig(args, ctx)

    tool.run = run
    return loaded


# ---------------------------------------------------------------- 主流程
def _run_mode(mode: str, with_selection: bool) -> dict:
    engine = _build_engine(mode)
    run_id = uuid.uuid4().hex[:6]
    records: list[dict] = []
    selection: list[dict] = []
    with engine:
        print(f"\n{'=' * 68}\n模式: {mode}"
              f"（技能目录: {'skills/' if mode == 'skills' else '空'}）"
              f"  run={run_id}\n{'=' * 68}")

        # 1) 决策场景集
        for sc in _scenarios():
            before = _tool_counts(engine)
            t0 = time.perf_counter()
            try:
                out = engine.ask(
                    sc["question"], now=NOW,
                    session_id=f"dec-{mode}-{run_id}-{sc['id']}",
                    use_cache=False)
                err = ""
            except Exception as e:  # noqa: BLE001 - 单条失败不中断
                out = None
                err = f"{type(e).__name__}: {e}"
            elapsed = int((time.perf_counter() - t0) * 1000)
            after = _tool_counts(engine)
            if out is None:
                ok, reason = False, err
                row = {"status": "", "message": "", "sql": ""}
            else:
                ok, reason = _JUDGES[sc["judge"]](out)
                row = {
                    "status": out.status,
                    "message": (out.message or "")[:300],
                    "sql": (out.sql or "")[:300],
                    "columns": list(out.result.columns) if out.result else [],
                    "rows": [list(r) for r in
                             (out.result.rows if out.result else [])][:3],
                }
            rec = {
                "id": sc["id"], "category": sc["category"],
                "question": sc["question"], "expect": sc["expect"],
                "pass": ok, "reason": reason, "elapsed_ms": elapsed,
                "tools": _diff_counts(before, after), **row,
            }
            records.append(rec)
            flag = "PASS" if ok else "FAIL"
            print(f"  [{flag}] {sc['id']:<24} {sc['category']:<4} "
                  f"{elapsed:>6}ms  {reason[:90]}")

        # 2) selection accuracy（只在有技能时才有意义）
        if with_selection and mode == "skills":
            print(f"\n-- selection accuracy（{len(_selection_cases())} 条） --")
            loaded_names = _patch_skill_load(engine)
            for case in _selection_cases():
                loaded_names.clear()
                try:
                    engine.ask(
                        case["question"], now=NOW,
                        session_id=(f"sel-{run_id}-"
                                    + str(abs(hash(case["question"]))
                                          % 100000)),
                        use_cache=False)
                except Exception as e:  # noqa: BLE001
                    print(f"  FAIL  {case['question'][:30]}  {e}")
                    selection.append({**case, "loaded": [], "pass": False})
                    continue
                loaded = list(loaded_names)
                expected = case["skill"]
                ok = (not loaded) if expected is None else (expected in loaded)
                selection.append({**case, "loaded": loaded, "pass": ok})
                print(f"  [{'PASS' if ok else 'FAIL'}] "
                      f"期望={str(expected or '（不加载）'):<32} "
                      f"实际={str(loaded or '（未加载）'):<32} "
                      f"{case['question'][:22]}")

    passed = sum(1 for r in records if r["pass"])
    sel_pass = sum(1 for r in selection if r["pass"])
    summary = {
        "mode": mode,
        "scenarios": {"passed": passed, "total": len(records),
                      "rate": round(passed / len(records), 3)
                      if records else 0},
        "selection": {"passed": sel_pass, "total": len(selection),
                      "rate": round(sel_pass / len(selection), 3)
                      if selection else None},
        "by_category": _by_category(records),
    }
    return {"run_id": run_id, "records": records, "selection": selection,
            "summary": summary}


def _by_category(records: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in records:
        b = out.setdefault(r["category"], {"passed": 0, "total": 0})
        b["total"] += 1
        b["passed"] += 1 if r["pass"] else 0
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=("baseline", "skills", "both"),
                    default="both")
    ap.add_argument("--out", default=os.path.join(WORK, "decision_eval.json"))
    ap.add_argument("--no-selection", action="store_true",
                    help="跳过 selection accuracy（省模型调用）")
    args = ap.parse_args(argv)

    if not KEY:
        print("未设置 NL2SQL_LLM_API_KEY——行为评测必须打真模型，跳过。")
        print("（这不是失败：未跑 ≠ 通过。设置 Key 后重跑。）")
        return 0

    modes = ("baseline", "skills") if args.mode == "both" else (args.mode,)
    report = {
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": MODEL, "base_url": BASE, "now": NOW,
        "modes": {},
    }
    for mode in modes:
        try:
            report["modes"][mode] = _run_mode(
                mode, with_selection=not args.no_selection)
        except Exception as e:  # noqa: BLE001 - 一个模式失败不丢另一个结果
            print(f"\n[ERROR] 模式 {mode} 失败: {type(e).__name__}: {e}")
            report["modes"][mode] = {"error": f"{type(e).__name__}: {e}"}

    print(f"\n{'=' * 68}\n对照结果\n{'=' * 68}")
    for mode, data in report["modes"].items():
        if "summary" not in data:
            print(f"  {mode:<9} 失败（见 error 字段）")
            continue
        s = data["summary"]
        print(f"  {mode:<9} 决策正确 {s['scenarios']['passed']}/"
              f"{s['scenarios']['total']} = {s['scenarios']['rate']:.1%}"
              + ("" if s["selection"]["rate"] is None else
                 f"；selection {s['selection']['passed']}/"
                 f"{s['selection']['total']} = "
                 f"{s['selection']['rate']:.1%}"))
    if ("baseline" in report["modes"] and "skills" in report["modes"]
            and "summary" in report["modes"]["baseline"]
            and "summary" in report["modes"]["skills"]):
        b = report["modes"]["baseline"]["summary"]["scenarios"]
        s = report["modes"]["skills"]["summary"]["scenarios"]
        print(f"\n  技能差值: {s['passed'] - b['passed']:+d} 题"
              f"（{b['passed']}/{b['total']} → {s['passed']}/{s['total']}）")
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n结果已写入 {os.path.relpath(args.out, ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
