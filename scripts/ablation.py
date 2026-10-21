"""M4 消融：去掉知识库 / 缓存 / 守门 / 窗口指标，各跑一次黄金集 30 题。

要回答的问题：这些"工程组件"到底是不是准确率的来源？
如果消融后准确率不变，说明它们是**代价与风险控制**，不是准确率的贡献者；
如果变化，说明它挡住了什么东西——那要能说清挡掉的是错误还是正确答案。

设计说明：
* 缓存：开/关各跑两遍。第一遍冷、第二遍热；准确率必须一致，
  热路径省下的是时间而不是正确性。
* 知识库：规则规划器不读知识库。这条消融在离线档预期 0 差异——
  如实记录，并注明"知识库对 hybrid / agent 档的影响需 Key 才能测"。
* 守门：没有配置开关（只读/单语句是底线，刻意不给运行时关掉的口子）。
  消融用**进程内临时替换** `Guard.check` 并统计它本来会改/会拒多少条。
* 窗口指标：把 `window=True` 的 5 个指标从模型里裁掉，看窗口类题目是否必坏。

用法:
    python scripts/ablation.py
"""
from __future__ import annotations

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
OUT_DIR = os.path.join(ROOT, "data", "_experiments", "ablation")

from nl2sql import NL2SQL, NL2SQLConfig  # noqa: E402
from nl2sql.guard import Guard, GuardDecision  # noqa: E402
from nl2sql.semantics.dbt_loader import load_with_report  # noqa: E402
from evaluate import rows_equal  # noqa: E402


class Scenario:
    """一次消融的配置。"""

    def __init__(self, label: str, *, cache: bool = True,
                 knowledge: bool = True, guard: bool = True,
                 trim_window: bool = False, passes: int = 1,
                 use_cache: bool = False):
        self.label = label
        self.cache = cache
        self.knowledge = knowledge
        self.guard = guard
        self.trim_window = trim_window
        self.passes = passes
        self.use_cache = use_cache


# 消融是单变量对照：除基线外每组都只改一项，缓存统一关掉，
# 否则"去掉知识库"的耗时差里会混进缓存开关的影响。
SCENARIOS = [
    Scenario("基线（缓存关，单遍）", cache=False),
    Scenario("缓存开（两遍）", cache=True, passes=2, use_cache=True),
    Scenario("去掉缓存（两遍）", cache=False, passes=2, use_cache=False),
    Scenario("去掉知识库", cache=False, knowledge=False),
    Scenario("去掉守门", cache=False, guard=False),
    Scenario("去掉窗口指标", cache=False, trim_window=True),
]


def run_scenario(scenario: Scenario, model, cases, work: str) -> dict:
    sub = copy.deepcopy(model)
    if scenario.trim_window:
        kept = [n for n, m in sub.metrics.items() if not m.window]
        removed = [n for n, m in sub.metrics.items() if m.window]
        sub.metrics = {n: sub.metrics[n] for n in kept}
    else:
        removed = []

    tag = scenario.label.replace("（", "_").replace("）", "").replace("，", "")
    db = os.path.join(work, f"{tag}.db")
    shutil.copy2(GOLDEN_DB, db)
    cfg = NL2SQLConfig(state_dir=os.path.join(work, f"state_{tag}"))
    cfg.add_sqlite(db, source_id="shop")
    cfg.agent.planner = "rule"
    cfg.cache.enabled = scenario.cache
    cfg.knowledge.enabled = scenario.knowledge

    guard_stats = {"checked": 0, "modified": 0, "denied": 0}
    original_check = Guard.check
    if not scenario.guard:
        def bypass(self, sql, *, limit=None):
            decision = original_check(self, sql, limit=limit)
            guard_stats["checked"] += 1
            if not decision.allowed:
                guard_stats["denied"] += 1
            elif decision.sql != sql:
                guard_stats["modified"] += 1
            return GuardDecision(True, sql=sql, rule="ablation_bypass")
        Guard.check = bypass

    passes = []
    try:
        with NL2SQL(cfg, semantic_model=sub) as engine:
            engine.refresh_snapshot()
            for _ in range(scenario.passes):
                stats = {"pass": 0, "total": len(cases), "elapsed_ms": 0,
                         "cache_hits": 0, "failures": []}
                for case in cases:
                    t0 = time.perf_counter()
                    out = engine.ask(case["question"], now=NOW,
                                     use_cache=scenario.use_cache)
                    stats["elapsed_ms"] += int((time.perf_counter() - t0) * 1000)
                    if out.cached:
                        stats["cache_hits"] += 1
                    matched = bool(out.ok and out.result is not None
                                   and rows_equal(case["rows"], out.result.rows))
                    if matched:
                        stats["pass"] += 1
                    else:
                        stats["failures"].append({
                            "id": case["id"],
                            "status": out.status,
                            "error_code": out.error_code,
                            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                        })
                stats["accuracy"] = stats["pass"] / stats["total"]
                passes.append(stats)
    finally:
        Guard.check = original_check

    return {"label": scenario.label, "passes": passes,
            "removed_metrics": removed, "guard_stats": guard_stats,
            "config": {"cache": scenario.cache, "knowledge": scenario.knowledge,
                       "guard": scenario.guard,
                       "trim_window": scenario.trim_window}}


def main() -> int:
    with open(BASELINE, encoding="utf-8") as f:
        cases = json.load(f)["cases"]
    model, report = load_with_report(GOLDEN_YML, source_id="shop")
    os.makedirs(OUT_DIR, exist_ok=True)
    work = os.path.join(OUT_DIR, "run")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)

    print(f"语义层: {GOLDEN_YML}（可移植性 {report.portability:.0%}）；"
          f"指标 {len(model.metric_names())} 个；黄金题 {len(cases)} 道")
    result = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "semantic_file": os.path.relpath(GOLDEN_YML, ROOT),
              "scenarios": []}
    for scenario in SCENARIOS:
        entry = run_scenario(scenario, model, cases, work)
        result["scenarios"].append(entry)
        print(f"\n[{entry['label']}]")
        for i, p in enumerate(entry["passes"], 1):
            print(f"  第 {i} 遍: {p['pass']}/{p['total']} = {p['accuracy']:.1%}"
                  f"  缓存命中 {p['cache_hits']}  耗时 {p['elapsed_ms']}ms"
                  f"  失败 {[f['id'] for f in p['failures']]}")
        if entry["removed_metrics"]:
            print(f"  裁掉的窗口指标: {entry['removed_metrics']}")
        if entry["guard_stats"]["checked"]:
            print(f"  守门原本的动作: 检查 {entry['guard_stats']['checked']} 条，"
                  f"补/改 LIMIT {entry['guard_stats']['modified']} 条，"
                  f"拒绝 {entry['guard_stats']['denied']} 条")

    path = os.path.join(OUT_DIR, "ablation.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 68)
    print("消融结果（第二遍为准；缓存组两遍都列）")
    print(f"{'场景':<20}{'准确率':>12}{'缓存命中':>8}{'耗时ms':>10}")
    for e in result["scenarios"]:
        p = e["passes"][-1]
        print(f"{e['label']:<20}{p['pass']}/{p['total']} {p['accuracy']:.1%}"
              f"{p['cache_hits']:>8}{p['elapsed_ms']:>10}")
    print(f"\nJSON: {path}")
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace")
    raise SystemExit(main())
