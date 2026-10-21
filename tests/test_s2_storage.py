"""S2 验收测试：适配器 / 元数据目录 / 知识库 / 缓存 / 历史。

运行:
    python tests/test_s2_storage.py
（不依赖 pytest，直接可跑；有 pytest 时也能被收集）
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_DB = os.path.join(ROOT, "golden_set", "ecommerce.db")

from nl2sql import NL2SQL, NL2SQLConfig  # noqa: E402
from nl2sql.contracts.knowledge import KnowledgeEntry  # noqa: E402

_failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        _failures.append(f"{label} {detail}")


def test_adapter(db: str) -> None:
    """db 已由 main() 复制好（副本，避免污染评测基线）。"""
    print("\n[1] SQLite 适配器")
    from nl2sql.sources.sqlite import SqliteAdapter

    a = SqliteAdapter(db)
    a.test_connection()
    check(True, "连接自检")

    caps = a.capabilities
    check(caps.dialect == "sqlite" and caps.pagination == "limit", "能力声明")

    tables = a.introspect()
    names = sorted(t.name for t in tables)
    check(names == ["dim_channel", "dim_customer", "dim_date",
                    "dim_product", "fact_order"],
          "抽取 5 张表", f"实际 {names}")

    fact = next(t for t in tables if t.name == "fact_order")
    check(fact.row_estimate == 810, "行数统计", f"实际 {fact.row_estimate}")
    check(any(c.is_primary_key for c in fact.columns), "主键识别")
    check(len(fact.columns) == 10, "列数（含生成列 amount）", f"实际 {len(fact.columns)}")
    amount_col = next((c for c in fact.columns if c.name == "amount"), None)
    check(amount_col is not None and amount_col.generated,
          "★ 生成列被识别（table_xinfo 而非 table_info）", f"实际 {amount_col}")

    # 快照库没有声明物理外键（外键只体现在索引上），
    # 因此这里断言的是"正确识别出没有物理外键"，而不是"能识别外键"。
    # 外键能力另用一个带声明的临时库验证（见 test_fk_detection）。
    check(fact.foreign_keys == [], "无物理外键时正确返回空",
          f"实际 {fact.foreign_keys}")

    # 执行 + 截断
    r = a.execute(
        "SELECT order_id FROM fact_order ORDER BY order_id", limit=10, timeout_s=10
    )
    check(r.row_count == 10 and r.truncated, "LIMIT 截断标记",
          f"rows={r.row_count} truncated={r.truncated}")

    r2 = a.execute("SELECT COUNT(*) AS n FROM fact_order", limit=100, timeout_s=10)
    check(r2.rows[0][0] == 810, "聚合执行", f"实际 {r2.rows[0][0]}")

    # EXPLAIN
    dr = a.explain("SELECT * FROM fact_order WHERE customer_id = 1")
    check(dr is not None and dr.ok, "EXPLAIN 可用")

    # 只读兜底：写操作必须失败
    write_err = ""
    try:
        a.execute("CREATE TABLE zzz_probe (a INT)", limit=10, timeout_s=5)
    except Exception as e:  # noqa: BLE001
        write_err = f"{type(e).__name__}: {e}"
    check(bool(write_err), "只读兜底（写操作被拒绝）")
    check("readonly" in write_err.lower() or "read-only" in write_err.lower(),
          "只读错误信息可识别", f"实际: {write_err}")
    # 表确实没有被创建
    exists = a.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='zzz_probe'",
        limit=10, timeout_s=5,
    ).rows[0][0]
    check(exists == 0, "写操作未产生副作用")

    a.close()


def test_fk_detection(tmp: str) -> None:
    """用临时库验证物理外键识别能力。"""
    print("\n[1b] 外键识别（临时库，含声明）")
    import sqlite3

    from nl2sql.sources.sqlite import SqliteAdapter

    db = os.path.join(tmp, "fk_probe.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE parent (id INTEGER PRIMARY KEY, name TEXT);"
        "CREATE TABLE child (id INTEGER PRIMARY KEY, pid INTEGER,"
        " FOREIGN KEY (pid) REFERENCES parent(id));"
    )
    conn.commit()
    conn.close()

    a = SqliteAdapter(db)
    child = next(t for t in a.introspect() if t.name == "child")
    check(("pid", "parent", "id") in child.foreign_keys,
          "物理外键识别", f"实际 {child.foreign_keys}")
    a.close()


def test_catalog_and_state(tmp: str) -> None:
    print("\n[2] 元数据目录 + 状态落盘")
    cfg = NL2SQLConfig(state_dir=os.path.join(tmp, "state"))
    cfg.add_sqlite(GOLDEN_DB, source_id="shop")

    with NL2SQL(cfg) as engine:
        res = engine.test_connection()
        check(res["ok"], "门面连接自检", str(res))

        meta = engine.refresh_snapshot()
        check(meta.table_count == 5, "快照表数", f"实际 {meta.table_count}")
        check(len(meta.fingerprint) == 32, "结构指纹")

        # 幂等：结构未变则指纹不变
        meta2 = engine.refresh_snapshot()
        check(meta2.fingerprint == meta.fingerprint
              and meta2.captured_at == meta.captured_at,
              "快照幂等（数据增长不触发重建）")

        engine.refresh_snapshot(force=True)
        meta3 = engine.refresh_snapshot()
        check(meta3.fingerprint == meta.fingerprint, "force 重建后指纹一致")

        tables = engine.tables()
        check(len(tables) == 5, "从快照读回表")

        ddl = engine.ddl(max_tables=2)
        check("CREATE TABLE" in ddl and "另有 3 张表未显示" in ddl,
              "DDL 限额生效", ddl[-60:])

        caps = engine.capabilities()
        check(caps["dialect"] == "sqlite", "能力声明透出")

    # 状态文件确实落盘
    db_file = os.path.join(cfg.state_dir, "app.db")
    check(os.path.exists(db_file), "状态库落盘 app.db")

    # 重开实例，配置与快照可恢复
    cfg2 = NL2SQLConfig(state_dir=os.path.join(tmp, "state"))
    cfg2.add_sqlite(GOLDEN_DB, source_id="shop")
    with NL2SQL(cfg2) as engine2:
        check(len(engine2.tables()) == 5, "重启后快照可恢复")
        recs = engine2.storage.meta.list_sources()
        check([r.source_id for r in recs] == ["shop"], "数据源配置可恢复")


def test_knowledge(tmp: str) -> None:
    print("\n[3] 知识库（版本 / 停用 / BM25）")
    cfg = NL2SQLConfig(state_dir=os.path.join(tmp, "state2"))
    cfg.add_sqlite(GOLDEN_DB, source_id="shop")

    from nl2sql.storage import build_storage

    cfg.validate()
    bundle = build_storage(cfg)
    ks = bundle.knowledge

    e = KnowledgeEntry(
        key="metric:gmv", type="business", title="GMV 口径",
        body="GMV 指成交金额，含优惠但不剔除退款。实收金额需减去 discount_amount。",
        source_id="shop", tags=["口径", "GMV"],
    )
    v1 = ks.upsert(e, note="初始")
    check(v1.version == 1, "首次写入 v1")

    # 内容未变 → 不产生新版本
    v1b = ks.upsert(v1)
    check(v1b.version == 1, "内容未变不推高版本")

    e2 = KnowledgeEntry(key="metric:gmv", type="business", title="GMV 口径",
                        body="GMV = SUM(amount)。实收金额 = SUM(amount - discount_amount)。",
                        source_id="shop", tags=["口径", "GMV"])
    v2 = ks.upsert(e2, note="修正表述")
    check(v2.version == 2, "修改产生 v2")

    hist = ks.history("metric:gmv")
    check(len(hist) == 2 and hist[0].version == 2, "历史含 2 个修订")

    # BM25 中文二元组检索（放在回滚之前：此时 v2 正文含"实收金额"）
    ks.upsert(KnowledgeEntry(
        key="schema:fact_order", type="schema", title="订单事实表",
        body="fact_order 订单事实表，一行一个订单。字段 order_status 取值 已支付/已退款/已取消。",
        source_id="shop",
    ))
    hits = ks.search_fts("实收金额怎么算", source_id="shop", top_k=5)
    check("metric:gmv" in [k for k, _ in hits],
          "BM25 命中口径条目", f"实际 {[k for k,_ in hits]}")

    hits2 = ks.search_fts("order_status 有哪些取值", source_id="shop", top_k=5)
    check("schema:fact_order" in [k for k, _ in hits2],
          "BM25 命中表结构条目", f"实际 {[k for k,_ in hits2]}")

    # 停用后不再参与检索
    ks.set_enabled("metric:gmv", False)
    hits3 = ks.search_fts("实收金额怎么算", source_id="shop", top_k=5)
    check("metric:gmv" not in [k for k, _ in hits3], "停用后不再召回")
    check(ks.get("metric:gmv").enabled is False, "停用状态持久")
    ks.set_enabled("metric:gmv", True)

    # 回滚：内容回到 v1，但版本号继续前进
    v3 = ks.rollback("metric:gmv", 1)
    check(v3.version == 3, "回滚生成 v3（不复用旧号）", f"实际 {v3.version}")
    check("含优惠但不剔除退款" in v3.body, "回滚内容正确")
    check(v3.body != v2.body, "回滚确实换回了旧内容（与 v2 不同）")
    check("SUM(amount)" not in v3.body, "回滚后不含 v2 的表述")

    # 软删仍保留历史
    ks.delete("schema:fact_order")
    check(len(ks.history("schema:fact_order")) >= 1, "软删保留历史")

    bundle.meta.close(); bundle.knowledge.close(); bundle.cache.close()


def test_cache(tmp: str) -> None:
    print("\n[4] 结果缓存（按数据源隔离 + TTL）")
    from nl2sql.contracts.storage import CachedResult
    from nl2sql.storage import build_storage

    cfg = NL2SQLConfig(state_dir=os.path.join(tmp, "state3"))
    cfg.add_sqlite(GOLDEN_DB, source_id="prod")
    cfg.add_sqlite(GOLDEN_DB, source_id="test")
    cfg.validate()
    bundle = build_storage(cfg)
    c = bundle.cache

    item = CachedResult(
        source_id="prod", plan_fingerprint="fp_abc",
        columns=["渠道", "实收金额"],
        rows=[["淘宝", 216339.57], ["官网", 203928.84]],
        row_count=2, sql="SELECT ...",
    )
    c.put(item, ttl_seconds=900)

    got = c.get("prod", "fp_abc")
    check(got is not None and got.row_count == 2, "命中")
    check(got.rows[0][1] == "216339.57",
          "浮点归一化（整值浮点与尾零处理）", f"实际 {got.rows[0][1]}")
    check(c.get("prod", "fp_abc").rows[1][1] == "203928.84", "第二位小数保留")

    check(c.get("test", "fp_abc") is None,
          "★ 数据源隔离：test 读不到 prod 的缓存")
    check(c.get("prod", "fp_other") is None, "指纹不同不命中")

    c.put(CachedResult(source_id="prod", plan_fingerprint="fp_exp",
                       columns=["n"], rows=[[1]], row_count=1,
                       sql="x", expires_at="2000-01-01T00:00:00"),
          ttl_seconds=None)
    check(c.get("prod", "fp_exp") is None, "过期即失效")

    n = c.invalidate("prod")
    check(n >= 1, "手动刷新清除条数", f"实际 {n}")
    check(c.get("prod", "fp_abc") is None, "刷新后失效")
    check(c.get("test", "fp_abc") is None, "刷新只作用于目标数据源")

    stats = c.stats()
    check("by_source" in stats, "统计可用")

    bundle.meta.close(); bundle.knowledge.close(); bundle.cache.close()


def test_history(tmp: str) -> None:
    print("\n[5] 查询历史")
    from nl2sql.contracts.storage import HistoryRecord
    from nl2sql.storage import build_storage

    cfg = NL2SQLConfig(state_dir=os.path.join(tmp, "state4"))
    cfg.add_sqlite(GOLDEN_DB, source_id="shop")
    cfg.validate()
    bundle = build_storage(cfg)

    bundle.meta.add_history(HistoryRecord(
        id="h1", source_id="shop", question="6月数码实收金额是多少",
        plan_fingerprint="fp1", sql="SELECT 1", status="ok",
        row_count=1, elapsed_ms=42, created_at="2024-06-30T10:00:00",
    ))
    recs = bundle.meta.list_history(source_id="shop")
    check(len(recs) == 1 and recs[0].row_count == 1, "历史落盘与读回")
    one = bundle.meta.get_history("h1")
    check(one is not None and one.question.startswith("6月"), "按 id 取历史")

    bundle.meta.close(); bundle.knowledge.close(); bundle.cache.close()


def main() -> int:
    """跑全部 S2 验收。

    注意：临时目录用**确定性路径 + makedirs**，不用 `tempfile.mkdtemp`。
    本项目的执行沙箱会拒绝对 mkdtemp 所创建目录的后续写入
    （`shutil.copy2` 报 PermissionError），而 makedirs 目录正常。
    用固定路径同时让失败后残留可被下次运行清理。
    """
    base = os.environ.get("NL2SQL_TEST_TMP") or os.path.join(ROOT, "data", "_test_s2")
    tmp = os.path.join(base, "run")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    print(f"临时目录: {tmp}")

    # 适配器测试用副本：只读校验会尝试写入，不应污染评测基线
    db = os.path.join(tmp, "adapter_probe.db")
    shutil.copy2(GOLDEN_DB, db)
    try:
        test_adapter(db)
        test_fk_detection(tmp)
        test_catalog_and_state(tmp)
        test_knowledge(tmp)
        test_cache(tmp)
        test_history(tmp)
    except Exception:
        traceback.print_exc()
        _failures.append("未捕获异常")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    if _failures:
        print(f"S2 验收未通过：{len(_failures)} 项失败")
        for f in _failures:
            print("  -", f)
        return 1
    print("S2 验收全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
