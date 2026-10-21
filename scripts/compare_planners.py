"""对比两种规划器在黄金集上的逐题表现，并测试混合策略。

目的：判断"规则 + 模型"是否互补。
若互补，混合策略就能同时拿到两者的正确题——这是不花钱的准确率提升。

用法:
    $env:NL2SQL_LLM_API_KEY='sk-xxx'
    python scripts/compare_planners.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "golden_set"))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_DB = os.path.join(ROOT, "golden_set", "ecommerce.db")
BASELINE = os.path.join(ROOT, "golden_set", "baseline.json")
NOW = "2024-06-30"

from nl2sql import NL2SQL, NL2SQLConfig  # noqa: E402
from nl2sql.llm import OpenAICompatProvider  # noqa: E402
from nl2sql.semantics.golden_model import build_model  # noqa: E402

# 判分口径**只有一份**，就是 golden_set/evaluate.py 的那份。
# 这里曾经自带一个"最小实现"，它把 NULL 从签名里整个丢掉了 ——
# 于是"多返回一列 NULL"被判成正确，准确率被高估 5 题（25/30 → 20/30）。
# 判分器不能各写一份：这一处漂移直接改了论文里最重要的那个数字。
from evaluate import rows_equal  # noqa: E402

rows_match = rows_equal


def run(planner: str, provider, cases, work: str,
        metrics_path: str | None = None) -> tuple[dict[str, bool], list[str]]:
    """跑一遍全部题目。

    `metrics_path` 给定时用 dbt 指标文件装配语义模型，否则用内置参考模型。
    这条路径是**端到端**的（含守门 / 缓存 / 校验 / 结果组装），
    因此它能验证"换成语义文件后准确率不变"——而不只是加载器本身对不对。
    """
    if metrics_path:
        from nl2sql.semantics.dbt_loader import load_with_report

        model, srep = load_with_report(metrics_path, source_id="shop")
        print(f"  语义层: {metrics_path}（可移植性 {srep.portability:.0%}）")
    else:
        model = build_model(order_level=True)
    db = os.path.join(work, f"{planner}.db")
    shutil.copy2(GOLDEN_DB, db)
    cfg = NL2SQLConfig(state_dir=os.path.join(work, f"state_{planner}"))
    cfg.add_sqlite(db, source_id="shop")
    cfg.agent.planner = planner

    result: dict[str, bool] = {}
    errors: list[str] = []
    with NL2SQL(cfg, semantic_model=model, llm_provider=provider) as engine:
        engine.refresh_snapshot()
        for c in cases:
            t0 = time.perf_counter()
            out = engine.ask(c["question"], now=NOW, use_cache=False)
            elapsed = int((time.perf_counter() - t0) * 1000)
            ok = bool(out.ok and out.result is not None
                      and rows_match(c["rows"], out.result.rows))
            result[c["id"]] = ok
            if not ok:
                errors.append(f"{c['id']}({out.error_code or out.status},{elapsed}ms)")
            if planner == "llm":
                time.sleep(0.3)
    return result, errors


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="对比规则规划器与模型规划器的准确率")
    ap.add_argument("--planners", default="rule,llm",
                    help="要跑哪些规划器，逗号分隔（rule,llm）")
    ap.add_argument("--metrics", default=None,
                    help="用 dbt metrics.yml 装配语义模型（默认用内置参考模型）")
    args = ap.parse_args()

    want = [p.strip() for p in args.planners.split(",") if p.strip()]
    key = os.environ.get("NL2SQL_LLM_API_KEY", "")
    with open(BASELINE, encoding="utf-8") as f:
        cases = json.load(f)["cases"]

    base = os.environ.get("NL2SQL_TEST_TMP") or os.path.join(ROOT, "data", "_cmp")
    work = os.path.join(base, "run")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)

    provider = OpenAICompatProvider(
        api_key=key, base_url=os.environ.get("NL2SQL_LLM_BASE_URL",
                                             "https://api.deepseek.com/v1"),
        model=os.environ.get("NL2SQL_LLM_MODEL", "deepseek-chat"),
    )

    results: dict[str, dict[str, bool]] = {}
    errors: dict[str, list[str]] = {}
    for p in want:
        print(f"跑 {p} 规划器 ...")
        results[p], errors[p] = run(p, provider, cases, work,
                                   metrics_path=args.metrics)

    ids = [c["id"] for c in cases]
    total = len(ids)
    print("\n" + "=" * 68)
    for p in want:
        n = sum(results[p].values())
        print(f"{p:<12} {n}/{total} = {n / total:.1%}")

    # 分类别准确率：总分相同不代表能力相同（语义层改动常常一类涨、一类跌）。
    cats: dict[str, list[str]] = {}
    for c in cases:
        cats.setdefault(c.get("category", "未分类"), []).append(c["id"])
    print("\n分类别：")
    header = f"{'类别':<14}" + "".join(f"{p:>12}" for p in want)
    print(header)
    print("-" * len(header))
    for cat, cat_ids in cats.items():
        line = f"{cat:<14}"
        for p in want:
            hit = sum(1 for i in cat_ids if results[p].get(i))
            line += f"{hit:>6}/{len(cat_ids):<5}"
        print(line)

    if "rule" in results and "llm" in results:
        rule, llm = results["rule"], results["llm"]
        union = [i for i in ids if rule[i] or llm[i]]
        print(f"\n两者都对     {[i for i in ids if rule[i] and llm[i]]}")
        print(f"仅规则对     {[i for i in ids if rule[i] and not llm[i]]}")
        print(f"仅模型对     {[i for i in ids if llm[i] and not rule[i]]}")
        print(f"两者都错     {[i for i in ids if not rule[i] and not llm[i]]}")
        print(f"\n★ 合并上限（规则优先 + 模型兜底）: {len(union)}/{total}"
              f" = {len(union) / total:.1%}")

    for p in want:
        print(f"\n{p} 失败题: {errors[p]}")

    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace")
    raise SystemExit(main())
