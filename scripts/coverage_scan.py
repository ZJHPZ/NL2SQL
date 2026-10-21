"""实验一：语义层覆盖率扫描（M4 核心实验）。

问题：确定性 NL2SQL 的准确率上限，是由模型强弱决定，还是由语义层覆盖决定？

做法：把 dbt 语义模型的**指标**按"被黄金集题目依赖的次数"从低到高裁掉，
得到若干档（满配 17 / 中配 11 / 低配 5，另加一条更密的扫描线），
每档用规则规划器跑黄金集 30 题，记录：

    1. 准确率（判分口径 = golden_set/evaluate.py 的 rows_equal，全仓唯一）；
    2. 语义天花板（题目所需指标是否还都在模型里——理论上限，可能高于实测）；
    3. 失败类型分布（UNRESOLVED / 编译失败 / 跑通了但答案不对）。

为什么失败类型比准确率更重要：规则规划器缺语义时**诚实认输**，
模型规划器缺语义时会绕过语义层现编 SQL——失败从"看不见"变成"看起来成功了"。
本脚本对 `--with-llm` 跑同样三档；没有 Key 时该列输出"未跑"，绝不写成通过。

用法:
    python scripts/coverage_scan.py                    # 只跑规则（离线）
    $env:NL2SQL_LLM_API_KEY='sk-xxx'; python scripts/coverage_scan.py --with-llm
    python scripts/coverage_scan.py --plot docs/assets/coverage_curve.png
"""
from __future__ import annotations

import argparse
import collections
import copy
import io
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "golden_set"))

GOLDEN_DB = os.path.join(ROOT, "golden_set", "ecommerce.db")
GOLDEN_YML = os.path.join(ROOT, "golden_set", "metrics.yml")
BASELINE = os.path.join(ROOT, "golden_set", "baseline.json")
NOW = "2024-06-30"

from nl2sql import NL2SQL, NL2SQLConfig  # noqa: E402
from nl2sql.dialect._deps import metric_refs  # noqa: E402
from nl2sql.semantics.dbt_loader import load_with_report  # noqa: E402
from nl2sql.semantics.golden_plans import PLANS  # noqa: E402
from evaluate import rows_equal  # noqa: E402

TOTAL_METRICS = 17            # 满配；中配/低配是 docs/10 定下的三档
TIERS = (("满配", 17), ("中配", 11), ("低配", 5))
SWEEP = (17, 15, 13, 11, 9, 7, 5, 3)   # 曲线用的密集扫描，比三档更说明趋势


def metric_closure(model, names) -> set[str]:
    """指标表达式的依赖闭包（比率/窗口指标引用了别的指标）。"""
    out: set[str] = set()
    stack = list(names)
    while stack:
        name = stack.pop()
        if name in out:
            continue
        out.add(name)
        metric = model.metrics.get(name)
        if metric is not None:
            stack.extend(r for r in metric_refs(metric.expr) if r in model.metrics)
    return out


def rank_metrics(model, cases) -> list[str]:
    """按"被黄金集题目依赖的次数"降序排指标；未用到的排最后。

    裁指标不能按字母序，否则"裁掉多少能力"不可解释。按被依赖次数裁，
    每裁一档都能说清"少覆盖了哪几类问题"。
    """
    usage: collections.Counter[str] = collections.Counter()
    for case in cases:
        plan = PLANS.get(case["id"])
        if plan is None:
            continue
        for name in metric_closure(model, [m.metric for m in plan.measures]):
            usage[name] += 1
    ranked = [n for n, _ in usage.most_common()]
    ranked += [n for n in model.metric_names() if n not in ranked]
    return ranked


def requirements(model, cases) -> dict[str, set[str]]:
    """每道黄金题"最少需要哪些指标"（含依赖闭包）。"""
    out: dict[str, set[str]] = {}
    for case in cases:
        plan = PLANS.get(case["id"])
        if plan is None:
            continue
        out[case["id"]] = metric_closure(model, [m.metric for m in plan.measures])
    return out


def trimmed(model, keep: set[str]):
    """复制一份只保留给定指标的语义模型（维度、连接原样保留）。"""
    clone = copy.deepcopy(model)
    clone.metrics = {k: v for k, v in clone.metrics.items() if k in keep}
    return clone


def classify(out, matched: bool) -> str:
    """把一次结果归类。判分只有一种口径，分类只是给人看的。"""
    if matched:
        return "通过"
    status = (out.status or "").lower()
    code = (out.error_code or "").upper()
    if "UNRESOLVED" in code or status == "unresolved":
        return "规划失败(UNRESOLVED)"
    if out.result is None:
        return "未产出结果"
    return "跑通但答案不对"


def run_tier(model, cases, tag: str, work: str, *, planner: str,
             provider=None, max_rows: int = 1000) -> dict:
    db = os.path.join(work, f"{tag}.db")
    shutil.copy2(GOLDEN_DB, db)
    cfg = NL2SQLConfig(state_dir=os.path.join(work, f"state_{tag}"))
    cfg.add_sqlite(db, source_id="shop")
    cfg.agent.planner = planner
    cfg.safety.max_rows = max_rows

    stats = {"pass": 0, "total": len(cases), "failures": [],
             "kinds": collections.Counter(), "categories": {},
             "elapsed_ms": 0}
    with NL2SQL(cfg, semantic_model=model, llm_provider=provider) as engine:
        engine.refresh_snapshot()
        for case in cases:
            t0 = time.perf_counter()
            out = engine.ask(case["question"], now=NOW, use_cache=False)
            elapsed = int((time.perf_counter() - t0) * 1000)
            stats["elapsed_ms"] += elapsed
            matched = bool(out.ok and out.result is not None
                           and rows_equal(case["rows"], out.result.rows))
            kind = classify(out, matched)
            stats["kinds"][kind] += 1
            cat = case.get("category", "未分类")
            bucket = stats["categories"].setdefault(cat, [0, 0])
            bucket[1] += 1
            if matched:
                stats["pass"] += 1
                bucket[0] += 1
            else:
                stats["failures"].append({
                    "id": case["id"], "kind": kind,
                    "status": out.status, "error_code": out.error_code,
                    "elapsed_ms": elapsed,
                })
            if planner in ("llm", "hybrid", "hybrid_verified"):
                time.sleep(0.3)
    stats["accuracy"] = stats["pass"] / stats["total"]
    stats["kinds"] = dict(stats["kinds"])
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="语义层覆盖率扫描（M4 实验一）")
    ap.add_argument("--with-llm", action="store_true",
                    help="同时跑模型规划器（需要 NL2SQL_LLM_API_KEY）")
    ap.add_argument("--plot", default=None,
                    help="把覆盖率曲线写成 PNG（需要 Pillow）")
    ap.add_argument("--work", default=os.path.join(ROOT, "data", "_experiments",
                                                   "coverage"))
    args = ap.parse_args()

    with open(BASELINE, encoding="utf-8") as f:
        cases = json.load(f)["cases"]
    model, report = load_with_report(GOLDEN_YML, source_id="shop")
    print(f"语义层: {GOLDEN_YML}（可移植性 {report.portability:.0%}）")
    print(f"指标 {len(model.metric_names())} 个 / 维度 {len(model.dimension_names())} 个 / "
          f"黄金题 {len(cases)} 道")

    ranked = rank_metrics(model, cases)
    need = requirements(model, cases)
    work = os.path.join(args.work, "run")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)

    key = os.environ.get("NL2SQL_LLM_API_KEY", "")
    provider = None
    if args.with_llm and key:
        from nl2sql.llm import OpenAICompatProvider
        provider = OpenAICompatProvider(
            api_key=key,
            base_url=os.environ.get("NL2SQL_LLM_BASE_URL", "https://api.deepseek.com/v1"),
            model=os.environ.get("NL2SQL_LLM_MODEL", "deepseek-chat"),
        )
    llm_skipped = "未跑（无 NL2SQL_LLM_API_KEY）" if (args.with_llm and not key) else None

    result = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "semantic_file": os.path.relpath(GOLDEN_YML, ROOT),
              "portability": report.portability,
              "total_metrics": len(model.metric_names()),
              "total_cases": len(cases), "tiers": [], "sweep": []}

    def evaluate_tier(label: str, target: int, planners: list[str]) -> dict:
        keep = set(ranked[:target])
        keep = metric_closure(model, keep)          # 保持依赖闭包，避免"缺依赖"噪音
        sub = trimmed(model, keep)
        covered = [cid for cid, ids in need.items() if ids <= keep]
        covered_cases = [c for c in cases if c["id"] in set(covered)]
        entry = {
            "label": label, "target": target,
            "kept_metrics": sorted(keep), "kept_count": len(keep),
            "coverage_ceiling": len(covered),
            "covered_ids": covered,
            "uncovered_ids": [c["id"] for c in cases if c["id"] not in set(covered)],
            "rule": None, "llm": None,
        }
        print(f"\n[{label}] 保留 {target} 个指标（闭包后 {len(keep)}），"
              f"语义天花板 {len(covered)}/{len(cases)} 题")
        for planner in planners:
            if planner == "llm" and provider is None:
                entry["llm"] = {"skipped": llm_skipped or "未跑"}
                print("  llm: 未跑（无 Key），不写成通过")
                continue
            # 天花板之外的题也要跑：规划器可能给出错误答案，这正是要说的事
            stats = run_tier(sub, cases, f"{label}_{planner}", work,
                             planner=planner, provider=provider)
            entry[planner] = stats
            print(f"  {planner}: {stats['pass']}/{stats['total']} = "
                  f"{stats['accuracy']:.1%}  失败类型 {stats['kinds']}")
        return entry

    for label, target in TIERS:
        pl = ["rule"] + (["llm"] if args.with_llm else [])
        result["tiers"].append(evaluate_tier(label, target, pl))

    print("\n[密集扫描] 规则规划器（一条曲线）")
    for target in SWEEP:
        keep = metric_closure(model, set(ranked[:target]))
        sub = trimmed(model, keep)
        covered = sum(1 for ids in need.values() if ids <= keep)
        stats = run_tier(sub, cases, f"sweep_{target}", work, planner="rule")
        point = {"target": target, "kept_count": len(keep),
                 "coverage_ceiling": covered,
                 "pass": stats["pass"], "accuracy": stats["accuracy"],
                 "kinds": stats["kinds"]}
        result["sweep"].append(point)
        print(f"  {target:>2} 个指标（天花板 {covered:>2}/30）→ "
              f"{stats['pass']:>2}/30 = {stats['accuracy']:.1%}")

    out_json = os.path.join(args.work, "coverage.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 72)
    print("实验一结果（准确率 / 语义天花板）")
    print(f"{'档位':<6}{'指标':>4}{'天花板':>8}{'rule':>10}{'llm':>12}")
    for e in result["tiers"]:
        rule = e["rule"]
        rule_txt = f"{rule['pass']}/{rule['total']} {rule['accuracy']:.1%}" if rule else "未跑"
        llm = e["llm"]
        llm_txt = ("未跑" if not llm or "skipped" in llm
                   else f"{llm['pass']}/{llm['total']} {llm['accuracy']:.1%}")
        print(f"{e['label']:<6}{e['kept_count']:>4}{e['coverage_ceiling']:>6}/30"
              f"{rule_txt:>14}{llm_txt:>14}")
    print(f"\nJSON: {out_json}")

    if args.plot:
        try:
            from scripts.plot_coverage import plot_coverage
        except ImportError:
            sys.path.insert(0, ROOT)
            from scripts.plot_coverage import plot_coverage
        plot_coverage(out_json, os.path.join(ROOT, args.plot)
                      if not os.path.isabs(args.plot) else args.plot)

    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace")
    raise SystemExit(main())
