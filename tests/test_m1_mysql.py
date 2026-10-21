"""M1 验收：同一批问题，SQLite 与 MySQL **逐题一致**。

为什么这条验收是 M1 的全部
--------------------------
适配器能连上、能查出数，只说明"没坏"。真正的契约是：
**换引擎不该改变答案**。这条一旦不成立，后面所有结论（准确率、口径、
缓存、论文里的实验数字）都只对 SQLite 成立，而 SQLite 不是数仓。

三个层次，从下往上：
    A 适配器契约：能力声明、连接自检、截断语义
    B 元数据等价：同一份数据，两边抽取出的表/列/主键/生成列/外键一致
    C 逐题等价：30 题的**逻辑计划**分别按两边能力编译、执行，结果集一致
    D 端到端：连 MySQL 跑完黄金集，准确率与失败题目集合与 SQLite 相同

C 与 D 分开是有意的：
    C 失败 = 编译/方言层的问题（改这里）
    D 失败 = 装配/链路的问题（改那里）
混在一起就分不清是谁的错。

已知的方向性差异（M3 已闭合）
------------------------------
C3 曾经用 `strftime('%Y-%W', order_date)` 按周分桶 —— 这是**写死在计划里**
的 SQLite 函数，MySQL 上没有。M1 当时把它作为已知失败显式打印，正确的修法
写在了待办里：时间粒度作为一等公民，计划说"按周"，由方言层各自展开。

M3 已完成这件事（`DimensionDefinition.grain`）。本节从
"29 一致 + 1 题已知方言失败"变成 **30/30 逐题一致**：C3 现在在两个引擎上
都返回 26 个周桶，且用的是各自方言的截断函数。

运行:
    set NL2SQL_MYSQL_PASSWORD=...   # 或由 .env 提供
    python tests/test_m1_mysql.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_DB = os.path.join(ROOT, "golden_set", "ecommerce.db")
BASELINE = os.path.join(ROOT, "golden_set", "baseline.json")
NOW = "2024-06-30"

# 判分口径必须与黄金集判分**同源**。各写一份迟早漂移，
# 而"两边用不同的尺子量"正是等价性验收最不能犯的错。
sys.path.insert(0, os.path.join(ROOT, "golden_set"))
from evaluate import rows_equal  # noqa: E402

from nl2sql.semantics.golden_model import build_model  # noqa: E402
from nl2sql.semantics.golden_plans import PLANS  # noqa: E402
from nl2sql.sources.mysql import MysqlAdapter  # noqa: E402
from nl2sql.sources.sqlite import SqliteAdapter  # noqa: E402

_failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        _failures.append(f"{label} {detail}")


def info(label: str, detail: str = "") -> None:
    print(f"  --    {label}  {detail}")


# --------------------------------------------------------------- 环境
def load_env() -> dict[str, str]:
    """读 `.env`（已 gitignore）。密码只从环境来，从不写进仓库。"""
    path = os.path.join(ROOT, ".env")
    values: dict[str, str] = {}
    if not os.path.exists(path):
        return values
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip('"').strip("'")
    return values


ENV = load_env()
for _k, _v in ENV.items():
    os.environ.setdefault(_k, _v)

MYSQL = {
    "host": os.environ.get("NL2SQL_MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("NL2SQL_MYSQL_PORT", "3306")),
    "user": os.environ.get("NL2SQL_MYSQL_USER", ""),
    "password": os.environ.get("NL2SQL_MYSQL_PASSWORD", ""),
    "database": os.environ.get("NL2SQL_MYSQL_DATABASE", ""),
}


def mysql_ready() -> tuple[bool, str]:
    missing = [k for k in ("user", "password", "database") if not MYSQL[k]]
    if missing:
        return False, f"缺少 {', '.join('NL2SQL_MYSQL_' + m.upper() for m in missing)}"
    return True, ""


def make_mysql(**over) -> MysqlAdapter:
    cfg = dict(MYSQL)
    cfg.update(over)
    return MysqlAdapter(
        host=cfg["host"], port=cfg["port"], user=cfg["user"],
        database=cfg["database"], password=cfg["password"],
    )


# --------------------------------------------------------------- A
def section_a(adapter: MysqlAdapter) -> None:
    print("\n[A] 适配器契约")

    try:
        adapter.test_connection()
        check(True, "连接自检")
    except Exception as exc:  # noqa: BLE001
        check(False, "连接自检", f"{type(exc).__name__}: {exc}")
        return

    cap = adapter.capabilities
    check(cap.dialect == "mysql", "能力声明 dialect=mysql", cap.dialect)
    check(cap.identifier_quote == "`", "反引号包裹标识符", repr(cap.identifier_quote))
    check(cap.supports_window and cap.supports_cte, "声明支持窗口与 CTE")

    desc = adapter.describe()
    check(desc["database"] == MYSQL["database"], "describe 带库名", str(desc.get("database")))
    check(desc["read_only"] is True, "默认只读事务")
    info("超时下推", "max_execution_time" if not desc["timeout_fallback"]
         else "降级到 read_timeout（老版本）")

    # 截断语义：给 N 行上限，内部读 N+1
    res = adapter.execute(
        "SELECT order_id FROM fact_order ORDER BY order_id", limit=5, timeout_s=10)
    check(len(res.rows) == 5 and res.truncated, "截断：5 行上限判出还有更多",
          f"rows={len(res.rows)} truncated={res.truncated}")
    check(res.row_count == 5 and res.columns == ["order_id"], "列名与行数",
          f"{res.columns}")

    res2 = adapter.execute("SELECT COUNT(*) AS n FROM fact_order",
                           limit=10, timeout_s=10)
    check(res2.rows == [(810,)] and not res2.truncated, "全量小结果不误判截断",
          str(res2.rows))

    # 值归一化：与 SQLite 同一口径（date→字符串、Decimal→float）
    res3 = adapter.execute(
        "SELECT MIN(order_date) AS d, SUM(amount) AS s FROM fact_order",
        limit=10, timeout_s=10)
    d, s = res3.rows[0]
    check(isinstance(d, str) and len(d) == 10, "DATE 归一化成字符串", repr(d))
    check(isinstance(s, float), "DECIMAL 归一化成 float", type(s).__name__)
    check(abs(s - 1315071.0) < 0.01, "金额合计与灌数校验一致", f"{s}")


# --------------------------------------------------------------- B
def section_b(mysql_adapter: MysqlAdapter, sqlite_adapter: SqliteAdapter) -> None:
    print("\n[B] 元数据等价（同一份数据，两边抽取结果比）")

    my_tables = {t.name: t for t in mysql_adapter.introspect()}
    sq_tables = {t.name: t for t in sqlite_adapter.introspect()}

    check(set(my_tables) == set(sq_tables), "表集合一致",
          f"仅MySQL={set(my_tables) - set(sq_tables)} 仅SQLite={set(sq_tables) - set(my_tables)}")

    for name in sorted(set(my_tables) & set(sq_tables)):
        mt, st = my_tables[name], sq_tables[name]
        my_cols = {c.name: c for c in mt.columns}
        sq_cols = {c.name: c for c in st.columns}
        same_names = set(my_cols) == set(sq_cols)
        check(same_names, f"[{name}] 列集合一致",
              f"仅MySQL={set(my_cols) - set(sq_cols)} 仅SQLite={set(sq_cols) - set(my_cols)}")
        if not same_names:
            continue

        diffs = []
        for col in sorted(my_cols):
            a, b = my_cols[col], sq_cols[col]
            if a.nullable != b.nullable:
                diffs.append(f"{col}.nullable {a.nullable}!={b.nullable}")
            if a.is_primary_key != b.is_primary_key:
                diffs.append(f"{col}.pk {a.is_primary_key}!={b.is_primary_key}")
            if a.generated != b.generated:
                diffs.append(f"{col}.generated {a.generated}!={b.generated}")
            if a.ordinal != b.ordinal:
                diffs.append(f"{col}.ordinal {a.ordinal}!={b.ordinal}")
        check(not diffs, f"[{name}] 列属性一致（可空/主键/生成列/顺序）",
              "; ".join(diffs[:4]))

        check(mt.foreign_keys == st.foreign_keys, f"[{name}] 外键一致", 
              f"{mt.foreign_keys} vs {st.foreign_keys}")

        if mt.row_estimate is not None and st.row_estimate is not None:
            check(mt.row_estimate == st.row_estimate, f"[{name}] 行数一致",
                  f"{mt.row_estimate} vs {st.row_estimate}")

        # 类型名按引擎各自表述，不做相等断言 —— 但要把差异**摆出来**
        type_pairs = sorted({f"{c}: {my_cols[c].data_type}/{sq_cols[c].data_type}"
                             for c in my_cols
                             if my_cols[c].data_type != sq_cols[c].data_type})
        if type_pairs:
            info(f"[{name}] 类型名差异（预期内）", f"{len(type_pairs)} 列: "
                 + ", ".join(type_pairs[:6]))

    # 生成列是重点：它是 SQLite(table_xinfo) 与 MySQL(EXTRA) 两套机制的交汇处
    amount_col = next(c for c in my_tables["fact_order"].columns if c.name == "amount")
    check(amount_col.generated, "fact_order.amount 在 MySQL 被识别为生成列")


# --------------------------------------------------------------- C
def section_c(mysql_adapter: MysqlAdapter, sqlite_adapter: SqliteAdapter,
              model: object) -> dict[str, str]:
    """30 题：同一份逻辑计划，分别按两边能力编译执行，比结果集。"""
    print("\n[C] 30 题逐题等价（逻辑计划 → 各自方言编译 → 执行 → 比结果集）")
    from nl2sql.dialect import SqlCompiler

    my_comp = SqlCompiler(mysql_adapter.capabilities)
    sq_comp = SqlCompiler(sqlite_adapter.capabilities)

    status: dict[str, str] = {}
    for qid in sorted(PLANS):
        plan = PLANS[qid]
        sql_s = sq_comp.compile(plan, model).sql
        sql_m = my_comp.compile(plan, model).sql

        try:
            r_s = sqlite_adapter.execute(sql_s, limit=5000, timeout_s=15)
        except Exception as exc:  # noqa: BLE001
            check(False, f"[{qid}] SQLite 执行失败", f"{exc}")
            status[qid] = "sqlite_error"
            continue

        try:
            r_m = mysql_adapter.execute(sql_m, limit=5000, timeout_s=15)
        except Exception as exc:  # noqa: BLE001
            check(False, f"[{qid}] MySQL 执行失败", f"{str(exc)[:160]}")
            status[qid] = "mysql_error"
            continue

        ok = rows_equal(r_s.rows, r_m.rows)
        check(ok, f"[{qid}] 结果集一致",
              "" if ok else f"SQLite {len(r_s.rows)} 行 vs MySQL {len(r_m.rows)} 行")
        status[qid] = "equal" if ok else "row_mismatch"

    counts: dict[str, int] = {}
    for v in status.values():
        counts[v] = counts.get(v, 0) + 1
    print(f"\n  汇总: {counts}")
    return status


# --------------------------------------------------------------- D
def section_d(model: object, work: str) -> None:
    """端到端：同一个 NL2SQL 装配，换数据源，跑完 30 题。"""
    print("\n[D] 端到端：两引擎各跑一遍黄金集（规则规划器）")
    from nl2sql import NL2SQL, NL2SQLConfig

    with open(BASELINE, encoding="utf-8") as f:
        cases = json.load(f)["cases"]

    def run(engine_name: str, configure) -> tuple[str, dict[str, bool]]:
        cfg = NL2SQLConfig(state_dir=os.path.join(work, f"state_{engine_name}"))
        configure(cfg)
        cfg.agent.planner = "rule"
        result: dict[str, bool] = {}
        with NL2SQL(cfg, semantic_model=model) as engine:
            engine.refresh_snapshot()
            for c in cases:
                try:
                    out = engine.ask(c["question"], now=NOW, use_cache=False)
                    result[c["id"]] = bool(
                        out.ok and out.result is not None
                        and rows_equal(c["rows"], out.result.rows))
                except Exception as exc:  # noqa: BLE001
                    traceback.print_exc()
                    result[c["id"]] = False
        return engine_name, result

    import shutil
    sq_db = os.path.join(work, "golden.db")
    shutil.copy2(GOLDEN_DB, sq_db)

    _, sq = run("sqlite", lambda cfg: cfg.add_sqlite(sq_db, source_id="shop"))
    _, my = run("mysql", lambda cfg: cfg.add_mysql(
        host=MYSQL["host"], port=MYSQL["port"], user=MYSQL["user"],
        database=MYSQL["database"], source_id="shop"))

    ids = sorted(sq)
    sq_pass, my_pass = sum(sq.values()), sum(my.values())
    check(sq_pass == my_pass, "两引擎通过题数相同",
          f"SQLite {sq_pass}/{len(ids)} vs MySQL {my_pass}/{len(ids)}")

    diff = [i for i in ids if sq.get(i) != my.get(i)]
    check(not diff, "逐题通过与否完全相同", f"分歧题: {diff}")

    # 一致性比对用"同源判分"，所以这里再确认一下绝对值
    print(f"\n  SQLite {sq_pass}/{len(ids)} = {sq_pass / len(ids):.1%}")
    print(f"  MySQL  {my_pass}/{len(ids)} = {my_pass / len(ids):.1%}")
    failed = [i for i in ids if not sq.get(i)]
    print(f"  失败题（两边相同）: {failed}")


# --------------------------------------------------------------- E
def section_e(adapter: MysqlAdapter) -> None:
    """三层防线里的后两层：只读事务（适配器）+ 只读账号（库）。"""
    print("\n[E] 只读与错误翻译")

    admin_user = os.environ.get("NL2SQL_MYSQL_ADMIN_USER", "root")
    is_ro = MYSQL["user"] != admin_user
    info("当前连接账号", f"{MYSQL['user']}"
         + ("（只读账号 —— 主链路一直跑在最小权限下）" if is_ro else "（不是只读账号）"))

    # 第二层防线：只读事务在服务端拒绝写
    try:
        adapter.execute("DELETE FROM dim_channel WHERE channel_id = 'C1'",
                        limit=10, timeout_s=10)
        check(False, "只读事务拒绝 DELETE", "居然执行成功了，这是严重问题")
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        check("1792" in text or "1142" in text or "只读" in text,
              "只读防线拒绝 DELETE", text[:120])

    # 错误提示必须可行动
    try:
        adapter.execute("SELECT nonexistent_col FROM fact_order", limit=5, timeout_s=10)
        check(False, "不存在的列应报错")
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        check("1054" in text, "错误码带可行动提示", text[:140])

    # 第三层防线：只读账号连 DDL 都做不了 —— 这一层与适配器无关，
    # 是**库自己**不让写，绕不过文本解析
    if not is_ro:
        info("只读账号专项", "跳过：当前连接账号不是 .env 里登记的只读账号")
        return
    try:
        adapter.execute("CREATE TABLE _probe_ro (x INT)", limit=5, timeout_s=10)
        check(False, "只读账号拒绝 CREATE TABLE", "居然建表成功了")
    except Exception as exc:  # noqa: BLE001
        check("1142" in str(exc), "只读账号拒绝 CREATE TABLE", str(exc)[:140])

    try:
        adapter.execute("UPDATE dim_channel SET channel_name = 'x'", limit=5,
                        timeout_s=10)
        check(False, "只读账号拒绝 UPDATE", "居然更新成功了")
    except Exception as exc:  # noqa: BLE001
        check("1142" in str(exc), "只读账号拒绝 UPDATE", str(exc)[:140])


# --------------------------------------------------------------- F
def section_f() -> None:
    """壳层：`NL2SQL_ADAPTER=mysql` 时真的装配成 MySQL 数据源。

    这一节测的是**接口**而不是适配器：换引擎对上层应该是"改一个环境变量"。
    若这里要改代码，那"可替换"就只是说法。
    """
    print("\n[F] 壳层装配（改一个环境变量就换引擎）")
    backend = os.path.join(ROOT, "apps", "web", "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)

    try:
        from shell_config import build_shell_config, read_env
    except Exception as exc:  # noqa: BLE001
        check(False, "导入壳层配置", f"{type(exc).__name__}: {exc}")
        return

    env = {
        "NL2SQL_ADAPTER": "mysql",
        "NL2SQL_MYSQL_HOST": MYSQL["host"],
        "NL2SQL_MYSQL_PORT": str(MYSQL["port"]),
        "NL2SQL_MYSQL_USER": MYSQL["user"],
        "NL2SQL_MYSQL_DATABASE": MYSQL["database"],
        "NL2SQL_STATE_DIR": os.path.join(ROOT, "data", "_m1", "state_shell"),
        "NL2SQL_PLANNER": "rule",
    }
    shell = read_env(env)
    check(shell.adapter == "mysql", "读到 NL2SQL_ADAPTER", shell.adapter)
    check("password" not in " ".join(shell.raw_env), "壳层配置里不带口令")

    cfg, provider = build_shell_config(shell)
    src = cfg.resolve_source()
    check(src.adapter == "mysql", "装配成 MySQL 数据源", src.adapter)
    check(src.credential_ref == "env:NL2SQL_MYSQL_PASSWORD",
          "口令来源是环境变量引用，不是明文", str(src.credential_ref))
    check(provider is None or type(provider).__name__ == "NullLLM",
          "无 Key 时退化为规则规划器", type(provider).__name__)

    # 真的问一句：走完整链路（元数据 / 规划 / 编译 / 守门 / 执行）
    from nl2sql import NL2SQL
    from nl2sql.semantics.golden_model import build_model
    with NL2SQL(cfg, semantic_model=build_model(order_level=True)) as engine:
        engine.refresh_snapshot()
        out = engine.ask("2024年上半年实收金额是多少", now=NOW, use_cache=False)
        ok = bool(out.ok and out.result is not None and out.result.rows)
        got = out.result.rows[0][0] if ok else None
        check(ok, "经壳层问一句能出结果", str(out.status))
        if ok:
            # 数值对不对由 [D] 逐题比对保证；这里只验证"装配通了、算得出数"。
            # 不在这里钉死一个数字：同一句话换个口径含义就变，
            # 钉死会让这条检查变成"改口径就报错"的假警。
            info("MySQL 上算出的金额", f"{got}")


# --------------------------------------------------------------- main
def main() -> int:
    ok, why = mysql_ready()
    if not ok:
        print(f"跳过 MySQL 验收：{why}")
        print("（这不是通过 —— 请提供 .env 或环境变量后重跑）")
        return 0

    work = os.path.join(ROOT, "data", "_m1")
    os.makedirs(work, exist_ok=True)

    model = build_model(order_level=True)
    sqlite_adapter = SqliteAdapter(GOLDEN_DB)
    mysql_adapter = make_mysql()

    try:
        section_a(mysql_adapter)
        section_b(mysql_adapter, sqlite_adapter)
        section_c(mysql_adapter, sqlite_adapter, model)
        section_e(mysql_adapter)
        section_f()
        section_d(model, work)
    finally:
        mysql_adapter.close()
        sqlite_adapter.close()

    print("\n" + "=" * 60)
    if _failures:
        print(f"{len(_failures)} 项失败：")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
