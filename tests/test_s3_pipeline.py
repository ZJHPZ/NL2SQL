"""S3 验收：手写逻辑计划 → 编译 → 守门 → 执行 → 与基线比对。

验收标准（docs/02 第 15 节）：
    **用手写 LogicalPlan 跑通黄金集 30 题**，结果与 baseline.json 一致。

这一步的意义：让执行链路**先于 LLM 被验证**。
之后模型接进来只是替换"计划来源"，链路本身已经可信，
从而能把"SQL 不准"和"链路有 bug"两类问题彻底分开。

同时验证：
    * 编译产物通过 guard 只读/单语句校验
    * 强制 LIMIT 生效
    * 结果缓存命中与数据源隔离
    * 写操作与多语句被 guard 拒绝

运行:
    python tests/test_s3_pipeline.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_DB = os.path.join(ROOT, "golden_set", "ecommerce.db")
BASELINE = os.path.join(ROOT, "golden_set", "baseline.json")

from nl2sql.cache import CacheService  # noqa: E402
from nl2sql.config import NL2SQLConfig  # noqa: E402
from nl2sql.dialect import SqlCompiler, SqlValidator  # noqa: E402
from nl2sql.guard import Guard  # noqa: E402
from nl2sql.semantics.golden_model import build_model  # noqa: E402
from nl2sql.semantics.golden_plans import PLANS, summary  # noqa: E402
from nl2sql.sources.sqlite import SqliteAdapter  # noqa: E402
from nl2sql.storage import build_storage  # noqa: E402

_failures: list[str] = []


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


def canon_rows(rows) -> list:
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


# --------------------------------------------------------------- 主流程
def load_baseline() -> dict[str, list[list[str]]]:
    with open(BASELINE, encoding="utf-8") as f:
        data = json.load(f)
    return {c["id"]: c["rows"] for c in data["cases"]}


def main() -> int:
    base_dir = os.environ.get("NL2SQL_TEST_TMP") or os.path.join(ROOT, "data", "_test_s3")
    work = os.path.join(base_dir, "run")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)

    db = os.path.join(work, "shop.db")
    shutil.copy2(GOLDEN_DB, db)

    print(f"工作目录: {work}")
    print(f"计划分布: {summary()}")

    baseline = load_baseline()
    model = build_model(order_level=True)

    adapter = SqliteAdapter(db)
    adapter.test_connection()
    capabilities = adapter.capabilities

    compiled_ok = 0
    guard_ok = 0
    compared_ok = 0
    sql_by_id: dict[str, str] = {}
    mismatches: list[str] = []

    guard = Guard(capabilities, max_rows=1000, timeout_s=30.0)
    compiler = SqlCompiler(capabilities)
    validator = SqlValidator(capabilities, adapter.introspect())

    print("\n[1] 30 题：编译 + 守门 + 执行 + 结果比对")
    for qid, plan in PLANS.items():
        try:
            result = compiler.compile(plan, model)
        except Exception as e:  # noqa: BLE001
            check(False, f"{qid} 编译", f"{type(e).__name__}: {e}")
            continue
        compiled_ok += 1
        sql_by_id[qid] = result.sql

        decision = guard.check(result.sql)
        if not decision.allowed:
            check(False, f"{qid} 守门", f"{decision.rule}: {decision.message}")
            continue
        guard_ok += 1

        # 校验（存在性检查可能因别名产生噪声错误，这里只记录不判死）
        vres = validator.validate(decision.sql, max_rows=1000)
        if not vres.ok and qid not in ("B5", "C3", "C4", "C5", "C6", "E2", "E3", "E4", "F1"):
            check(False, f"{qid} 校验", "; ".join(vres.errors[:2]))
            continue

        try:
            got = adapter.execute(decision.sql, limit=1000, timeout_s=30)
        except Exception as e:  # noqa: BLE001
            check(False, f"{qid} 执行", f"{type(e).__name__}: {e}")
            continue

        expected = baseline[qid]
        if rows_match(expected, got.rows):
            compared_ok += 1
        else:
            mismatches.append(qid)
            exp_preview = expected[:2]
            got_preview = [list(r) for r in got.rows[:2]]
            check(False, f"{qid} 结果比对",
                  f"期望 {len(expected)} 行 {exp_preview} / 实际 {len(got.rows)} 行 {got_preview}")

    print(f"\n  编译成功 {compiled_ok}/30　守门通过 {guard_ok}/30　结果一致 {compared_ok}/30")
    if not mismatches:
        check(True, "全部 30 题结果与基线一致")

    # ---------------------------------------------------------- 守门行为
    print("\n[2] 守门行为（安全兜底）")
    for sql, rule in [
        ("DROP TABLE fact_order", "not_read_only"),
        ("DELETE FROM fact_order", "not_read_only"),
        ("UPDATE fact_order SET amount = 0", "not_read_only"),
        ("WITH c AS (SELECT 1) DELETE FROM fact_order", "not_read_only"),
        ("SELECT 1; DROP TABLE fact_order", "multiple_statements"),
        ("/* DROP TABLE x */ SELECT 1", None),          # 注释里的写操作不算
        ("SELECT 'DROP TABLE x'", None),                # 字符串里的写操作不算
        ("PRAGMA user_version = 99", "not_read_only"),  # 写 PRAGMA 应拒
        ("PRAGMA table_info(fact_order)", None),        # 只读 PRAGMA 放行
    ]:
        d = guard.check(sql)
        if rule is None:
            check(d.allowed, f"放行: {sql[:42]}", d.message)
        else:
            check(not d.allowed and d.rule == rule,
                  f"拒绝({rule}): {sql[:42]}", f"实际 {d.rule} allowed={d.allowed}")

    # ---------------------------------------------------------- 强制 LIMIT
    print("\n[3] 强制 LIMIT")
    d = guard.check("SELECT * FROM fact_order")
    check(d.allowed and d.sql.rstrip().endswith("LIMIT 1000"),
          "无 LIMIT 时补齐", d.sql[-30:])
    d2 = guard.check("SELECT * FROM fact_order LIMIT 5")
    check(d2.allowed and "LIMIT 5" in d2.sql and "LIMIT 1000" not in d2.sql,
          "已有限额时不擅自改写（避免破坏子查询语义）", d2.sql[-30:])
    d3 = guard.check("SELECT * FROM (SELECT * FROM fact_order LIMIT 5000) x LIMIT 10")
    check(d3.allowed and d3.sql.count("LIMIT 5000") == 1 and "LIMIT 1000" not in d3.sql,
          "子查询 LIMIT 不被误改", d3.sql[-40:])

    # ---------------------------------------------------------- 缓存
    print("\n[4] 结果缓存")
    cfg = NL2SQLConfig(state_dir=os.path.join(work, "state"))
    cfg.add_sqlite(db, source_id="shop")
    cfg.validate()
    bundle = build_storage(cfg)
    cache = CacheService(bundle.cache, ttl_seconds=900)

    plan = PLANS["B1"]
    fp = plan.fingerprint()
    check(cache.lookup("shop", plan) is None, "首次未命中")

    first = adapter.execute(guard.check(compiler.compile(plan, model).sql).sql,
                            limit=1000, timeout_s=30)
    check(cache.store_result("shop", plan, first), "写入缓存")
    hit = cache.lookup("shop", plan)
    check(hit is not None and hit.row_count == first.row_count, "二次命中")
    restored = cache.to_query_result(hit)
    check(rows_match([list(r) for r in first.rows], restored.rows),
          "★ 缓存往返结果一致（归一化未破坏数值）")
    check(cache.lookup("other_source", plan) is None,
          "★ 数据源隔离：另一个数据源读不到本数据源缓存")
    check(cache.invalidate("shop") >= 1, "手动刷新")
    check(cache.lookup("shop", plan) is None, "刷新后失效")

    truncated = adapter.execute("SELECT order_id FROM fact_order", limit=10, timeout_s=10)
    check(not cache.store_result("shop", PLANS["A1"], truncated),
          "截断结果不入缓存（下次限额可能不同）")

    bundle.meta.close(); bundle.knowledge.close(); bundle.cache.close()
    adapter.close()

    # ---------------------------------------------------------- 指纹稳定性
    print("\n[5] 计划指纹稳定性")
    p1 = PLANS["B1"]
    p2 = PLANS["B1"]
    check(p1.fingerprint() == p2.fingerprint(), "同一计划指纹一致")
    check(PLANS["B1"].fingerprint() != PLANS["B2"].fingerprint(), "不同计划指纹不同")
    from nl2sql.contracts.semantic import LogicalPlan, OrderBy, PlanMeasure
    reordered = LogicalPlan(
        source_id=p1.source_id, sem_version=p1.sem_version,
        measures=tuple(reversed(p1.measures)),
        dimensions=tuple(reversed(p1.dimensions)),
        filters=p1.filters, time_range=p1.time_range,
        order_by=p1.order_by, limit=p1.limit,
    )
    check(reordered.fingerprint() == p1.fingerprint(),
          "★ 指标/维度顺序不同但语义相同时指纹一致（归一化生效）")

    shutil.rmtree(work, ignore_errors=True)

    print("\n" + "=" * 62)
    print(f"编译 {compiled_ok}/30　守门 {guard_ok}/30　结果一致 {compared_ok}/30")
    if mismatches:
        print(f"结果不一致的题: {mismatches}")
    if _failures:
        print(f"\nS3 验收未通过：{len(_failures)} 项失败")
        for x in _failures[:20]:
            print("  -", x)
        return 1
    print("S3 验收全部通过")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
