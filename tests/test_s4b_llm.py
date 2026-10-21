"""S4b 验收：真实模型（DeepSeek）对黄金集的准确率。

与 `tests/test_s4_agent.py` 的 [A] 段（规则规划器）形成对照：
    规则规划器 → 语义层覆盖率（与模型能力解耦的基准线）
    模型规划器 → 模型能力

两个数字一起看才有意义：模型的增益、成本、延迟都能被量化。

运行（Key 通过环境变量传入，不要写在文件或命令行里）:
    $env:NL2SQL_LLM_API_KEY='sk-xxx'
    python tests/test_s4b_llm.py            # 全部 30 题
    python tests/test_s4b_llm.py 8          # 只跑前 8 题（快速验证）
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

from nl2sql import NL2SQL, NL2SQLConfig  # noqa: E402
from nl2sql.llm import LLMPlanner, OpenAICompatProvider  # noqa: E402
from nl2sql.semantics.golden_model import build_model  # noqa: E402

NOW = "2024-06-30"
# 每问一次会话，避免跨题上下文干扰；DeepSeek 单次规划约 500~3000ms
PAUSE_S = 0.3


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
        return ("num", float(int(fv)) if fv == int(fv) and abs(fv) < 1e15
                else round(fv, 4))
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
    """严格比对：行数、每行字符串集合、数值集合都必须一致。"""
    a, b = canon_rows(expected), canon_rows(got)
    if len(a) != len(b):
        return False
    for (na, sa), (nb, sb) in zip(a, b):
        if sa != sb or len(na) != len(nb):
            return False
        if any(abs(x - y) > tol for x, y in zip(na, nb)):
            return False
    return True


def _sig_multiset(rows) -> list[tuple]:
    """把每行归一化成"字符串集合 + 数值多重集"，忽略列顺序。"""
    out = []
    for r in rows:
        cells = [canon_cell(v) for v in r]
        nums = tuple(sorted(round(c[1], 4) for c in cells if c[0] == "num"))
        strs = tuple(sorted(c[1] for c in cells if c[0] == "str"))
        out.append((nums, strs))
    return out


def rows_overlap(expected, got, tol: float = 1e-6) -> bool:
    """重叠比对：行数一致，且**预期结果的值都出现在实际行里**。

    用途：把"用户要的列少给了"与"数值算错"分开。
    实际结果多带一列（例如同时给了中间指标）不应被判为完全错误——
    用户仍然能拿到要的东西，只是多了一列。
    """
    if len(expected) != len(got):
        return False
    exp, act = _sig_multiset(expected), _sig_multiset(got)
    # 通过排序后的多重集逐项比较（行内部已忽略列顺序）
    for (en, es), (an, as_) in zip(sorted(exp), sorted(act)):
        if not set(es).issubset(set(as_)):
            return False
        # 预期的数值必须都能在实际数值里找到（多重集包含）
        pool = list(an)
        for x in en:
            hit = next((i for i, y in enumerate(pool) if abs(x - y) <= tol), None)
            if hit is None:
                return False
            pool.pop(hit)
    return True


def count_match(expected, got) -> bool:
    return len(expected) == len(got)


def _pct(n: int, d: int) -> str:
    return f"{n / max(1, d):.1%}"


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    key = os.environ.get("NL2SQL_LLM_API_KEY", "")
    if not key:
        print("未设置 NL2SQL_LLM_API_KEY，无法运行真实模型测试。")
        return 1
    base_url = os.environ.get("NL2SQL_LLM_BASE_URL",
                              "https://api.deepseek.com/v1")
    model_name = os.environ.get("NL2SQL_LLM_MODEL", "deepseek-chat")

    base = os.environ.get("NL2SQL_TEST_TMP") or os.path.join(ROOT, "data", "_test_s4b")
    work = os.path.join(base, "run")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    db = os.path.join(work, "shop.db")
    shutil.copy2(GOLDEN_DB, db)

    with open(BASELINE, encoding="utf-8") as f:
        cases = json.load(f)["cases"]
    if limit:
        cases = cases[:limit]

    print(f"模型     : {model_name} @ {base_url}")
    print(f"题量     : {len(cases)}")
    print("=" * 68)

    semantic_model = build_model(order_level=True)
    provider = OpenAICompatProvider(
        api_key=key, base_url=base_url, model=model_name,
    )

    cfg = NL2SQLConfig(state_dir=os.path.join(work, "state"))
    cfg.add_sqlite(db, source_id="shop")
    cfg.agent.planner = "llm"

    counters = {"ok": 0, "partial": 0, "count_ok": 0,
                "plan_fail": 0, "exec_fail": 0, "mismatch": 0}
    tokens = {"prompt": 0, "completion": 0}
    latencies: list[int] = []
    details: list[tuple[str, str, str]] = []

    with NL2SQL(cfg, semantic_model=semantic_model,
                llm_provider=provider) as engine:
        engine.refresh_snapshot()
        for c in cases:
            qid, question, expected = c["id"], c["question"], c["rows"]
            t0 = time.perf_counter()
            # 走完整链路（规划 → 编译 → 缓存 → 守门 → 执行 → 结果组装），
            # 因此测到的是端到端表现，而不是某一段的孤立能力
            out = engine.ask(question, now=NOW, use_cache=False)
            plan_ms = int((time.perf_counter() - t0) * 1000)
            latencies.append(plan_ms)

            if out.status == "unresolved":
                counters["plan_fail"] += 1
                details.append((qid, "规划失败", (out.message or "")[:90]))
            elif not out.ok or out.result is None:
                counters["exec_fail"] += 1
                details.append((qid, "执行失败",
                                f"{out.error_code}: {(out.message or '')[:70]}"))
            else:
                got = out.result.rows
                strict = rows_match(expected, got)
                overlap = rows_overlap(expected, got)
                if strict:
                    counters["ok"] += 1
                    details.append((qid, "正确", f"{out.result.row_count} 行 {plan_ms}ms"))
                elif overlap:
                    counters["partial"] += 1
                    counters["count_ok"] += 1
                    details.append((
                        qid, "部分符合",
                        f"行数对({len(expected)}) 值都在，但列集不同："
                        f"期望 {len(expected[0])} 列 {expected[0]} / "
                        f"实际 {len(got[0])} 列 {[list(r) for r in got[:1]][0]}",
                    ))
                elif count_match(expected, got):
                    counters["count_ok"] += 1
                    counters["mismatch"] += 1
                    details.append((
                        qid, "数值不符",
                        f"行数对({len(expected)}) 但值不同："
                        f"期望 {expected[:1]} / 实际 {[list(r) for r in got[:1]]}",
                    ))
                else:
                    counters["mismatch"] += 1
                    details.append((
                        qid, "结果不符",
                        f"期望 {len(expected)} 行 {expected[:1]} / "
                        f"实际 {len(got)} 行 {[list(r) for r in got[:1]]}",
                    ))
            time.sleep(PAUSE_S)

    total = len(cases)
    print("\n逐题：")
    for qid, status, note in details:
        print(f"  {qid:3s} {status:5s} {note}")

    planned = total - counters["plan_fail"]
    executed = counters["ok"] + counters["partial"] + counters["mismatch"]
    print("\n" + "=" * 68)
    print("三种判分口径（越来越宽）：")
    print(f"  [严格] 行数与列值完全一致      {counters['ok']}/{total}"
          f" = {_pct(counters['ok'], total)}")
    print(f"  [重叠] 行数一致 + 预期值都命中  "
          f"{counters['ok'] + counters['partial']}/{total}"
          f" = {_pct(counters['ok'] + counters['partial'], total)}")
    print(f"  [行数] 仅行数一致              {counters['count_ok'] + counters['ok']}/{total}"
          f" = {_pct(counters['count_ok'] + counters['ok'], total)}")
    print(f"\n链路统计：规划成功 {planned}/{total}　执行成功 {executed}/{total}")
    print(f"失败分类：规划失败 {counters['plan_fail']}　执行失败 {counters['exec_fail']}"
          f"　数值不符 {counters['mismatch']}　列集差异 {counters['partial']}")
    if latencies:
        latencies.sort()
        p50 = latencies[len(latencies) // 2]
        print(f"端到端延迟：p50 {p50}ms　max {max(latencies)}ms")
    print(f"token：{tokens['prompt']}+{tokens['completion']}")

    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
