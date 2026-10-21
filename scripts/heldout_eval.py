"""实验三：留出集一次性评估。

留出集（`golden_set/heldout.json`，20 题，H1–H20）在开发期**一次都没有跑过**：
题目是手写的，答案由手写 SQL 生成，语义层/规划器没有按它们调整过。
现在跑一次，结果就是结果——掉到多少都如实写进 docs/14。

这条纪律比分数重要：如果留出集也参与调参，"留出"两个字就是假的。

用法:
    python scripts/heldout_eval.py            # 规则规划器（离线，可复现）
    $env:NL2SQL_LLM_API_KEY='sk-xxx'; python scripts/heldout_eval.py --with-llm
"""
from __future__ import annotations

import argparse
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
HELDOUT = os.path.join(ROOT, "golden_set", "heldout.json")
NOW = "2024-06-30"
OUT_DIR = os.path.join(ROOT, "data", "_experiments", "heldout")

from nl2sql import NL2SQL, NL2SQLConfig  # noqa: E402
from nl2sql.semantics.dbt_loader import load_with_report  # noqa: E402
from evaluate import rows_equal  # noqa: E402


def run(planner: str, model, cases, work: str, provider=None) -> dict:
    db = os.path.join(work, f"{planner}.db")
    shutil.copy2(GOLDEN_DB, db)
    cfg = NL2SQLConfig(state_dir=os.path.join(work, f"state_{planner}"))
    cfg.add_sqlite(db, source_id="shop")
    cfg.agent.planner = planner

    stats = {"planner": planner, "pass": 0, "total": len(cases),
             "elapsed_ms": 0, "failures": [], "categories": {}}
    with NL2SQL(cfg, semantic_model=model, llm_provider=provider) as engine:
        engine.refresh_snapshot()
        for case in cases:
            t0 = time.perf_counter()
            out = engine.ask(case["question"], now=NOW, use_cache=False)
            elapsed = int((time.perf_counter() - t0) * 1000)
            stats["elapsed_ms"] += elapsed
            matched = bool(out.ok and out.result is not None
                           and rows_equal(case["rows"], out.result.rows))
            cat = case.get("category", "未分类")
            bucket = stats["categories"].setdefault(cat, [0, 0])
            bucket[1] += 1
            if matched:
                stats["pass"] += 1
                bucket[0] += 1
            else:
                stats["failures"].append({
                    "id": case["id"], "question": case["question"],
                    "status": out.status, "error_code": out.error_code,
                    "expected_rows": case.get("row_count"),
                    "actual_rows": (None if out.result is None
                                    else len(out.result.rows)),
                    "elapsed_ms": elapsed,
                })
            print(f"  {case['id']:<4}{'✓' if matched else '✗'} {case['question']}")
    stats["accuracy"] = stats["pass"] / stats["total"]
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="留出集一次性评估（M4 实验三）")
    ap.add_argument("--with-llm", action="store_true",
                    help="同时跑模型规划器（需要 Key；留出集只该跑一次，慎用）")
    args = ap.parse_args()

    with open(HELDOUT, encoding="utf-8") as f:
        data = json.load(f)
    cases = data["cases"]
    model, report = load_with_report(GOLDEN_YML, source_id="shop")
    print(f"留出集 {len(cases)} 题（{os.path.relpath(HELDOUT, ROOT)}）；"
          f"语义层可移植性 {report.portability:.0%}")
    print("注意：这是开发期唯一一次运行，结果直接进 docs/14，不据此调参。\n")

    os.makedirs(OUT_DIR, exist_ok=True)
    work = os.path.join(OUT_DIR, "run")
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

    result = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "heldout_file": os.path.relpath(HELDOUT, ROOT),
              "note": "开发期唯一一次运行；未用于调参",
              "rule": run("rule", model, cases, work),
              "llm": run("llm", model, cases, work, provider)
              if provider is not None else {"skipped": "未跑（无 Key）"}}
    path = os.path.join(OUT_DIR, "heldout_eval.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 68)
    for name in ("rule", "llm"):
        s = result[name]
        if "skipped" in s:
            print(f"{name}: {s['skipped']}（未跑 ≠ 通过）")
            continue
        print(f"{name}: {s['pass']}/{s['total']} = {s['accuracy']:.1%}"
              f"  耗时 {s['elapsed_ms']}ms")
        for cat, (hit, total) in s["categories"].items():
            print(f"  {cat:<14}{hit}/{total}")
        if s["failures"]:
            print(f"  失败: {[f['id'] for f in s['failures']]}")
    print(f"\nJSON: {path}")
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace")
    raise SystemExit(main())
