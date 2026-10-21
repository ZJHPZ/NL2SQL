"""S6 验收：知识库（检索 / 版本 / 停用）+ few-shot 效果 + 验证式混合。

评测设计要点（避免自欺）
------------------------
1. **留出集**：只把 A/B/C 三类的 15 题入库，只在 D/E/F 三类的 15 题上评测。
   若把 30 题全入库再测这 30 题，就是答案泄露——测出来的只是检索准不准。
2. **对照实验**：同一条链路跑两次（知识库空 vs 有种子），
   差值才是知识库的真实贡献。
3. **验证式混合单独测**：它是"规则答错"的兜底，与知识库是独立的两条改进。

运行:
    python tests/test_s6_knowledge.py            # 离线部分（无需 Key）
    $env:NL2SQL_LLM_API_KEY='sk-xxx'
    python tests/test_s6_knowledge.py            # 全部
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
GOLDEN_DIR = os.path.join(ROOT, "golden_set")
GOLDEN_DB = os.path.join(GOLDEN_DIR, "ecommerce.db")
NOW = "2024-06-30"

from nl2sql import NL2SQL, NL2SQLConfig  # noqa: E402
from nl2sql.contracts.knowledge import KnowledgeEntry, RetrievalQuery  # noqa: E402
from nl2sql.knowledge import (  # noqa: E402
    HOLDOUT_CATEGORIES, SEED_CATEGORIES, split, stable_key,
)
from nl2sql.knowledge.seed import seed_from_golden  # noqa: E402
from nl2sql.llm import OpenAICompatProvider  # noqa: E402
from nl2sql.semantics.golden_model import build_model  # noqa: E402

_failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        _failures.append(f"{label} {detail}")


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


# =============================================================== A. 知识库本身
def test_knowledge_base(work: str) -> None:
    print("\n[A] 知识库：自动生成 / 检索 / 版本 / 停用 / 测试检索")

    cfg = NL2SQLConfig(state_dir=os.path.join(work, "kb"))
    cfg.add_sqlite(GOLDEN_DB, source_id="shop")
    with NL2SQL(cfg, semantic_model=build_model(order_level=True)) as engine:
        engine.refresh_snapshot()

        # 1) 首次连接自动从库表元数据生成 schema 条目
        entries = engine.knowledge_list(source_id="shop")
        schema_entries = [e for e in entries if e.type == "schema"]
        check(len(schema_entries) == 5,
              "首次连接自动生成 5 条 schema 知识", f"实际 {len(schema_entries)}")
        fact = next((e for e in schema_entries if e.title == "fact_order"), None)
        check(fact is not None and "order_status" in fact.body,
              "schema 条目含字段清单")
        check(fact is not None and "amount" in fact.body,
              "★ schema 条目含生成列 amount（table_xinfo 的收益）")

        # 2) 幂等：再次 connect 不应重复生成或覆盖
        before = {e.key: e.version for e in engine.knowledge_list(
            source_id="shop", include_disabled=True)}
    with NL2SQL(cfg, semantic_model=build_model(order_level=True)) as engine2:
        engine2.refresh_snapshot()
        after = {e.key: e.version for e in engine2.knowledge_list(
            source_id="shop", include_disabled=True)}
        check(before == after, "★ 自动生成幂等（不覆盖已有条目、不推高版本）",
              f"{before} vs {after}")

        # 3) 测试检索：能给出命中原因
        #    用与表结构相关的问题——schema 条目的正文是表/字段清单，
        #    问"实收金额怎么算"（口径问题）本来就命中不到 schema 条目
        hits = engine2.knowledge_test_retrieve(
            "fact_order 表有哪些字段", source_id="shop", top_k=5)
        check(bool(hits), "测试检索有结果", f"命中 {len(hits)} 条")
        check(all(h.why for h in hits), "★ 每条命中都带命中原因（why）",
              str([h.why for h in hits]))
        check(any(h.entry.type == "schema" for h in hits),
              "表结构问题命中 schema 条目",
              str([(h.entry.key, h.why) for h in hits[:3]]))
        print(f"        命中: {[(h.entry.title[:14], h.why) for h in hits[:3]]}")

        # 4) 版本与回滚
        key = stable_key("business", "gmv")
        engine2.knowledge_upsert(KnowledgeEntry(
            key=key, type="business", title="GMV 口径",
            body="GMV 含优惠不剔除退款", source_id="shop"))
        engine2.knowledge_upsert(KnowledgeEntry(
            key=key, type="business", title="GMV 口径",
            body="GMV = SUM(amount)，含优惠、不剔除退款", source_id="shop"))
        hist = engine2.knowledge_history(key)
        check(len(hist) == 2, "知识条目留 2 个修订", f"实际 {len(hist)}")
        rolled = engine2.knowledge_rollback(key, 1)
        check(rolled.version == 3 and rolled.body == "GMV 含优惠不剔除退款",
              "回滚生成 v3 且内容回到 v1", f"v{rolled.version} {rolled.body[:20]}")

        # 5) 停用后不再召回
        engine2.knowledge_set_enabled(key, False)
        hits2 = engine2.knowledge_test_retrieve("GMV 口径 含优惠", source_id="shop", top_k=8)
        check(key not in [h.entry.key for h in hits2], "停用后不再被召回")
        engine2.knowledge_set_enabled(key, True)

        # 6) few-shot 只取 sql_pair，且不召回被停用的
        seed_entries = seed_from_golden(GOLDEN_DIR, only_categories=SEED_CATEGORIES)
        engine2.knowledge_upsert_many(seed_entries)
        ex = engine2.knowledge_few_shot(
            "2024年6月，数码品类的实收金额是多少？", source_id="shop", top_k=3)
        check(bool(ex), "few-shot 召回成功")
        check(all(sql.strip() for _, sql in ex), "示例都带 SQL")
        print(f"        few-shot 命中: {[q[:20] for q, _ in ex]}")

        # 停用一条 sql_pair → 不应再出现在 few-shot
        disabled_key = seed_entries[0].key
        disabled_q = seed_entries[0].extra["question"]
        engine2.knowledge_set_enabled(disabled_key, False)
        ex2 = engine2.knowledge_few_shot(disabled_q, source_id="shop", top_k=5)
        check(all(q != disabled_q for q, _ in ex2),
              "★ 停用的 few-shot 示例不再被召回",
              f"仍召回: {[q[:20] for q, _ in ex2]}")


# =============================================================== B. 留出集切分
def test_split() -> None:
    print("\n[B] 留出集切分（防止答案泄露）")
    seed, holdout = split(GOLDEN_DIR)
    # 断言实际规模而非预设数字：黄金集的类别分布是 17/13，
    # 关键是"两集互斥且合起来是全集"，而不是某个特定数字
    check(len(seed) + len(holdout) == 30, "两集合计 30 题",
          f"{len(seed)}+{len(holdout)}")
    check(len(seed) >= 10 and len(holdout) >= 10,
          "两集都有足够样本", f"入库 {len(seed)} / 留出 {len(holdout)}")
    overlap = {c["id"] for c in seed} & {c["id"] for c in holdout}
    check(not overlap, "★ 两集无交集", str(overlap))
    check(all(c["category"] in SEED_CATEGORIES for c in seed),
          "入库集只含指定类别")
    check(all(c["category"] in HOLDOUT_CATEGORIES for c in holdout),
          "留出集只含指定类别")
    print(f"        入库({len(seed)}): {[c['id'] for c in seed]}")
    print(f"        留出({len(holdout)}): {[c['id'] for c in holdout]}")


# =============================================================== C. 实测
def run_holdout(mode: str, provider, seed_kb: bool, work: str,
                plan_modes: list[str]) -> dict[str, dict[str, bool]]:
    """在留出集上评测。`seed_kb=False` 时不注入 few-shot。"""
    _, holdout = split(GOLDEN_DIR)
    results: dict[str, dict[str, bool]] = {}

    for plan_mode in plan_modes:
        db = os.path.join(work, f"{mode}_{plan_mode}.db")
        shutil.copy2(GOLDEN_DB, db)
        cfg = NL2SQLConfig(state_dir=os.path.join(work, f"st_{mode}_{plan_mode}"))
        cfg.add_sqlite(db, source_id="shop")
        cfg.agent.planner = plan_mode
        # 自动生成 schema 条目会干扰对照（它含表结构，本身就有帮助）。
        # 为了让实验只反映 few-shot 的贡献，这里关掉自动生成。
        cfg.knowledge.auto_generate_from_comments = False

        out_map: dict[str, bool] = {}
        with NL2SQL(cfg, semantic_model=build_model(order_level=True),
                    llm_provider=provider) as engine:
            engine.refresh_snapshot()
            if seed_kb:
                entries = seed_from_golden(
                    GOLDEN_DIR, only_categories=SEED_CATEGORIES)
                engine.knowledge_upsert_many(entries)
            for c in holdout:
                out = engine.ask(c["question"], now=NOW, use_cache=False)
                out_map[c["id"]] = bool(
                    out.ok and out.result is not None
                    and rows_match(c["rows"], out.result.rows)
                )
                if plan_mode != "rule":
                    time.sleep(0.25)
        results[plan_mode] = out_map
    return results


def test_effect(work: str, provider) -> None:
    key = os.environ.get("NL2SQL_LLM_API_KEY", "")
    _, holdout = split(GOLDEN_DIR)
    total = len(holdout)

    print("\n[C] 实测：few-shot 的贡献（留出集 15 题）")
    if not key:
        print("  跳过（未设置 NL2SQL_LLM_API_KEY）—— 离线部分已覆盖知识库本身")
        return

    print("  跑 llm-only（无知识库）...")
    no_kb = run_holdout("nokb", provider, False, work, ["llm"])["llm"]
    print("  跑 llm-only（有 few-shot）...")
    with_kb = run_holdout("withkb", provider, True, work, ["llm"])["llm"]

    n_no, n_with = sum(no_kb.values()), sum(with_kb.values())
    print(f"\n  llm-only 无知识库  {n_no}/{total} = {n_no/total:.1%}")
    print(f"  llm-only 有 few-shot {n_with}/{total} = {n_with/total:.1%}")
    delta = n_with - n_no
    print(f"  知识库贡献: {'+' if delta >= 0 else ''}{delta} 题")

    gained = [k for k in with_kb if with_kb[k] and not no_kb[k]]
    lost = [k for k in with_kb if no_kb[k] and not with_kb[k]]
    print(f"  因知识库答对的题: {gained}")
    print(f"  因知识库反而答错的题: {lost}")

    # 不断言"一定提升"——few-shot 可能无效甚至有害，
    # 如实报告比强行通过更有价值
    check(len(lost) <= 2, "few-shot 未造成明显退化", f"退化 {lost}")


def test_verified_hybrid(work: str, provider) -> None:
    key = os.environ.get("NL2SQL_LLM_API_KEY", "")
    print("\n[D] 验证式混合：能抓住'规则答错'吗")
    if not key:
        print("  跳过（未设置 NL2SQL_LLM_API_KEY）")
        return

    with open(os.path.join(GOLDEN_DIR, "baseline.json"), encoding="utf-8") as f:
        cases = json.load(f)["cases"]
    total = len(cases)

    db = os.path.join(work, "vh.db")
    shutil.copy2(GOLDEN_DB, db)
    cfg = NL2SQLConfig(state_dir=os.path.join(work, "st_vh"))
    cfg.add_sqlite(db, source_id="shop")
    cfg.agent.planner = "hybrid_verified"
    cfg.knowledge.auto_generate_from_comments = False

    ok = 0
    vstats: dict = {}
    with NL2SQL(cfg, semantic_model=build_model(order_level=True),
                llm_provider=provider) as engine:
        check(engine.planner_mode == "hybrid_verified",
              "auto + 有模型 → hybrid_verified", engine.planner_mode)
        engine.refresh_snapshot()
        # 用入库集之外的题（全 30 题）来观察验证器行为
        for c in cases:
            out = engine.ask(c["question"], now=NOW, use_cache=False)
            if out.ok and out.result is not None and rows_match(
                    c["rows"], out.result.rows):
                ok += 1
            time.sleep(0.25)
        planner = engine._agent._planner
        vstats = dict(getattr(planner, "stats", {}))
        vdesc = planner.describe() if hasattr(planner, "describe") else {}

    print(f"\n  hybrid_verified  {ok}/{total} = {ok/total:.1%}")
    print(f"  规划器统计: {vstats}")
    ver_stats = {}
    try:
        ver_stats = dict(planner._verifier.stats)
    except Exception:  # noqa: BLE001
        pass
    print(f"  验证器统计: {ver_stats}")
    rv = vstats.get("rule_rejected", 0)
    print(f"  → 规则计划被验证器拦下 {rv} 题，改由模型重做")
    print(f"  → 模型调用合计: 验证 {ver_stats.get('checked', 0)} 次"
          f" + 规划 {vstats.get('llm_used', 0)} 次")


def main() -> int:
    base = os.environ.get("NL2SQL_TEST_TMP") or os.path.join(ROOT, "data", "_test_s6")
    work = os.path.join(base, "run")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    print(f"工作目录: {work}")

    key = os.environ.get("NL2SQL_LLM_API_KEY", "")
    provider = None
    if key:
        provider = OpenAICompatProvider(
            api_key=key,
            base_url=os.environ.get("NL2SQL_LLM_BASE_URL",
                                    "https://api.deepseek.com/v1"),
            model=os.environ.get("NL2SQL_LLM_MODEL", "deepseek-chat"),
        )

    try:
        test_knowledge_base(work)
        test_split()
        if provider is not None:
            test_effect(work, provider)
            test_verified_hybrid(work, provider)
        else:
            print("\n[C][D] 跳过（未设置 NL2SQL_LLM_API_KEY）")
    except Exception:
        traceback.print_exc()
        _failures.append("未捕获异常")

    shutil.rmtree(work, ignore_errors=True)
    print("\n" + "=" * 64)
    if _failures:
        print(f"S6 验收未通过：{len(_failures)} 项失败")
        for x in _failures[:20]:
            print("  -", x)
        return 1
    print("S6 验收通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
