"""M1 扩展验收：同一批问题，SQLite 与 PostgreSQL **逐题一致**（第三引擎）。

为什么还要一个引擎
------------------
MySQL 证明了"适配器 + capability 驱动降级"这条路对**第二个**引擎成立；
但它只证明了"多一个引擎"不是偶然。第三引擎要回答的是另一个问题：
**核心层是不是真的没有按方言写分支？**
PostgreSQL 与 SQLite/MySQL 的差异足够大（双引号、NUMERIC 精确小数、
TO_CHAR/EXTRACT 而不是 strftime/DATE_FORMAT、pg_catalog 而不是
information_schema），如果 PG 上还得改核心层，那"M1 换引擎只改一个文件"
就只是两个引擎的巧合。

六个层次，从下往上（与 test_m1_mysql.py 对齐）：
    A 适配器契约：能力声明、连接自检、截断语义、值归一化、超时翻译
    B 元数据等价：表/列/主键/生成列/顺序与外键
    C 逐题等价：30 题的**逻辑计划**按两边能力编译、执行，结果集一致
    D 端到端：连 PostgreSQL 跑完黄金集，准确率与失败题目集合与 SQLite 相同
    E 只读与错误翻译：服务端只读事务拒写、错误码带可行动提示
    F 壳层装配：`NL2SQL_ADAPTER=postgres` 时真的装配成 PG 数据源

C 与 D 分开是有意的：
    C 失败 = 编译/方言层的问题（改这里）
    D 失败 = 装配/链路的问题（改那里）

怎么起 PostgreSQL（不需要管理员权限 / docker）
-----------------------------------------------
PyPI 包 `embedded-postgres` 自带 PostgreSQL 18 的 Windows 二进制：

    pip install embedded-postgres

本测试优先用它；找不到时按下面顺序找 bin 目录：
    1. 环境变量 `NL2SQL_PG_BIN`（直接指向含 postgres.exe 的目录）
    2. `_tmp/pylibs/embedded_postgres/pginstall/bin`（离线解包路径）
    3. site-packages 里的 `embedded_postgres` 包（`pip install` 装的）
都没有就打印安装说明并返回 0 —— **未跑 ≠ 通过**。

为什么不用 `pg_ctl start`：在受限令牌（sandbox）下它会报
`could not create restricted token: error code 87`，无法启动；
直接跑 `postgres.exe` 绕开这个问题，且不需要管理员权限。
数据目录 `data/_test_pg/pgdata` 首次 initdb 后复用，只需几秒。

运行:
    python tests/test_pg_engine.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_DB = os.path.join(ROOT, "golden_set", "ecommerce.db")
BASELINE = os.path.join(ROOT, "golden_set", "baseline.json")
NOW = "2024-06-30"

DB_NAME = "nl2sql_golden"
PG_USER = "postgres"
DATA_ROOT = os.path.join(ROOT, "data", "_test_pg")
PGDATA = os.path.join(DATA_ROOT, "pgdata")
WORK = os.path.join(DATA_ROOT, "run")

# 判分口径必须与黄金集判分**同源**。各写一份迟早漂移，
# 而"两边用不同的尺子量"正是等价性验收最不能犯的错。
sys.path.insert(0, os.path.join(ROOT, "golden_set"))
from evaluate import rows_equal  # noqa: E402

from nl2sql.semantics.golden_model import build_model  # noqa: E402
from nl2sql.semantics.golden_plans import PLANS  # noqa: E402
from nl2sql.sources.postgres import PostgresAdapter  # noqa: E402
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


# --------------------------------------------------------------- 定位 PG 二进制
def _bin_dir_ok(path: str | None) -> bool:
    if not path:
        return False
    exe = "postgres.exe" if os.name == "nt" else "postgres"
    return os.path.isfile(os.path.join(path, exe))


def find_pg_bin() -> str | None:
    """按优先级找 PostgreSQL 的 bin 目录。见模块 docstring。"""
    env_dir = os.environ.get("NL2SQL_PG_BIN")
    if _bin_dir_ok(env_dir):
        return env_dir

    # 离线解包路径（本仓库调试时用的方式）
    local = os.path.join(ROOT, "_tmp", "pylibs", "embedded_postgres", "pginstall", "bin")
    if _bin_dir_ok(local):
        return local

    # pip 装的 embedded-postgres：只查模块位置，**不 import**
    # （该包会 import psutil/platformdirs/fasteners，缺依赖时 import 会炸）
    try:
        import importlib.util
        spec = importlib.util.find_spec("embedded_postgres")
    except Exception:  # noqa: BLE001 - 包不存在/元数据坏了都当作找不到
        spec = None
    if spec is not None and spec.submodule_search_locations:
        for base in spec.submodule_search_locations:
            cand = os.path.join(base, "pginstall", "bin")
            if _bin_dir_ok(cand):
                return cand

    # 常见安装位置（非 embedded 的本地安装）
    for cand in (
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                     "PostgreSQL", "18", "bin"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                     "PostgreSQL", "17", "bin"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                     "PostgreSQL", "16", "bin"),
    ):
        if _bin_dir_ok(cand):
            return cand
    return None


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


class EmbeddedPostgres:
    """自管理一个本地 PostgreSQL 实例（不依赖 pg_ctl / 服务注册）。

    `pg_ctl start` 在受限令牌下会失败（Windows sandbox 的已知限制），
    所以直接跑 `postgres.exe`。stop 用 terminate：退出时 PG 会自己清理
    （fsync=off 只为本机测试提速，崩溃丢数据也无所谓，数据每次重灌）。
    """

    def __init__(self, bin_dir: str, data_dir: str) -> None:
        self.bin_dir = bin_dir
        self.data_dir = data_dir
        self.port = 0
        self.proc: subprocess.Popen | None = None
        self._log = None

    def _exe(self, name: str) -> str:
        return os.path.join(self.bin_dir, name)

    def start(self, timeout_s: float = 40.0) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        if not os.path.exists(os.path.join(self.data_dir, "PG_VERSION")):
            r = subprocess.run(
                [self._exe("initdb.exe"), "-D", self.data_dir,
                 "-U", PG_USER, "-A", "trust", "-E", "UTF8", "--no-locale"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            check(r.returncode == 0, "initdb 初始化数据目录",
                  (r.stderr or r.stdout or "")[-300:])
            if r.returncode != 0:
                raise RuntimeError("initdb 失败，无法启动 PostgreSQL")

        # 上次异常退出可能留下 pid 文件；进程已死时它只是脏文件
        pid_file = os.path.join(self.data_dir, "postmaster.pid")
        if os.path.exists(pid_file):
            try:
                os.remove(pid_file)
            except OSError:
                pass

        self.port = free_port()
        self._log = open(os.path.join(DATA_ROOT, "postgres.log"), "ab")
        self.proc = subprocess.Popen(
            [self._exe("postgres.exe"), "-D", self.data_dir,
             "-p", str(self.port), "-h", "127.0.0.1",
             "-c", "fsync=off"],
            stdout=self._log, stderr=subprocess.STDOUT, cwd=self.bin_dir,
        )

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"postgres.exe 启动失败（exit={self.proc.returncode}），"
                    f"日志: {os.path.join(DATA_ROOT, 'postgres.log')}"
                )
            try:
                import psycopg
                conn = psycopg.connect(
                    host="127.0.0.1", port=self.port, user=PG_USER,
                    dbname="postgres", connect_timeout=2,
                )
                conn.close()
                return
            except Exception:  # noqa: BLE001 - 服务还没就绪，继续等
                time.sleep(0.3)
        raise RuntimeError(f"PostgreSQL 未在 {timeout_s:.0f}s 内就绪")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if self._log is not None:
            try:
                self._log.close()
            finally:
                self._log = None


def load_golden(port: int) -> bool:
    """用 scripts/pg_load_golden.py 把 SQLite 黄金库原样搬进 PG。"""
    cmd = [
        sys.executable, os.path.join(ROOT, "scripts", "pg_load_golden.py"),
        "--host", "127.0.0.1", "--port", str(port),
        "--user", PG_USER, "--database", DB_NAME,
    ]
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired:
        check(False, "黄金集灌入 PostgreSQL（scripts/pg_load_golden.py）",
              "180s 超时（PG 没起来或 loader 阻塞）")
        return False
    tail = "\n".join((r.stdout or "").strip().splitlines()[-8:])
    if tail:
        print(tail)
    ok = r.returncode == 0
    check(ok, "黄金集灌入 PostgreSQL（scripts/pg_load_golden.py）",
          "" if ok else (r.stderr or r.stdout or "")[-400:])
    return ok


# --------------------------------------------------------------- 装置
def make_pg(port: int, **over) -> PostgresAdapter:
    kwargs = {
        "host": "127.0.0.1",
        "port": port,
        "user": PG_USER,
        "database": DB_NAME,
        "password": "",
    }
    kwargs.update(over)
    return PostgresAdapter(**kwargs)


# --------------------------------------------------------------- A
def section_a(adapter: PostgresAdapter) -> None:
    print("\n[A] 适配器契约")

    try:
        adapter.test_connection()
        check(True, "连接自检")
    except Exception as exc:  # noqa: BLE001
        check(False, "连接自检", f"{type(exc).__name__}: {exc}")
        return

    cap = adapter.capabilities
    check(cap.dialect == "postgres", "能力声明 dialect=postgres", cap.dialect)
    check(cap.identifier_quote == '"', "双引号包裹标识符", repr(cap.identifier_quote))
    check(cap.supports_window and cap.supports_cte, "声明支持窗口与 CTE")
    check(cap.default_schema == "public", "默认 schema=public", str(cap.default_schema))

    desc = adapter.describe()
    check(desc["database"] == DB_NAME, "describe 带库名", str(desc.get("database")))
    check(desc["schema"] == "public", "describe 带 schema", str(desc.get("schema")))
    check(desc["read_only"] is True, "默认只读事务")

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

    # 值归一化：与 SQLite 同一口径（date→字符串、NUMERIC→float）
    res3 = adapter.execute(
        "SELECT MIN(order_date) AS d, SUM(amount) AS s FROM fact_order",
        limit=10, timeout_s=10)
    d, s = res3.rows[0]
    check(isinstance(d, str) and len(d) == 10, "DATE 归一化成字符串", repr(d))
    check(isinstance(s, float), "NUMERIC 归一化成 float", type(s).__name__)
    check(abs(s - 1315071.0) < 0.01, "金额合计与灌数校验一致", f"{s}")

    # 超时下推到服务端：statement_timeout 触发 SQLSTATE 57014 → QueryTimeoutError。
    #
    # 环境限制（实测）：在 DSH 的受限沙箱里，postgres.exe 后端之间的信号传递
    # 被拦住 —— `SHOW statement_timeout` 明明显示 200ms，pg_sleep(1) 仍跑满 1s；
    # 用第二个连接 pg_cancel_backend(pid) 也返回 False。也就是说本机无法验证
    # "服务端定时器真的掐断查询"这条主路径（真实部署环境可以）。
    # 因此自适应：
    #   1) 定时器可用 → 断言真实超时被翻译（主路径）；
    #   2) 定时器不可用 → 用服务端会返回的 QueryCanceled(57014) 断言翻译逻辑，
    #      并**显式打印**"本环境不可验证"，不静默跳过（未跑 ≠ 通过）。
    from nl2sql.errors import QueryTimeoutError
    timer_works = False
    try:
        adapter.execute("SELECT pg_sleep(0.5)", limit=5, timeout_s=0.15)
    except QueryTimeoutError:
        timer_works = True
    except Exception:  # noqa: BLE001 - 其他异常说明是别的问题，走分支 2 再报
        pass
    if timer_works:
        check(True, "服务端 statement_timeout 生效且被翻译成 QueryTimeoutError")
    else:
        info("服务端超时（statement_timeout）在受限沙箱里不生效",
             "进程间信号被拦；改为验证 57014 → QueryTimeoutError 的翻译路径")
        import psycopg
        try:
            adapter._raise_translated(
                psycopg.errors.QueryCanceled(
                    "canceling statement due to statement timeout"),
                timeout_s=0.15,
            )
            check(False, "57014 被翻译成 QueryTimeoutError", "没抛异常")
        except QueryTimeoutError as exc:
            check("被服务端中断" in str(exc), "57014 被翻译成 QueryTimeoutError",
                  str(exc)[:80])
        except Exception as exc:  # noqa: BLE001
            check(False, "57014 被翻译成 QueryTimeoutError",
                  f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------- B
def section_b(pg_adapter: PostgresAdapter, sqlite_adapter: SqliteAdapter) -> None:
    print("\n[B] 元数据等价（同一份数据，两边抽取结果比）")

    pg_tables = {t.name: t for t in pg_adapter.introspect()}
    sq_tables = {t.name: t for t in sqlite_adapter.introspect()}

    check(set(pg_tables) == set(sq_tables), "表集合一致",
          f"仅PG={set(pg_tables) - set(sq_tables)} 仅SQLite={set(sq_tables) - set(pg_tables)}")

    for name in sorted(set(pg_tables) & set(sq_tables)):
        pt, st = pg_tables[name], sq_tables[name]
        pg_cols = {c.name: c for c in pt.columns}
        sq_cols = {c.name: c for c in st.columns}
        same_names = set(pg_cols) == set(sq_cols)
        check(same_names, f"[{name}] 列集合一致",
              f"仅PG={set(pg_cols) - set(sq_cols)} 仅SQLite={set(sq_cols) - set(pg_cols)}")
        if not same_names:
            continue

        diffs = []
        for col in sorted(pg_cols):
            a, b = pg_cols[col], sq_cols[col]
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

        # 外键目标带 schema 前缀（public.xxx）也是合理表述；
        # 本模型 SQLite 侧没有物理外键，两边都为空，直接相等即可。
        check(pt.foreign_keys == st.foreign_keys, f"[{name}] 外键一致",
              f"{pt.foreign_keys} vs {st.foreign_keys}")

        if pt.row_estimate is not None and st.row_estimate is not None:
            check(pt.row_estimate == st.row_estimate, f"[{name}] 行数一致",
                  f"{pt.row_estimate} vs {st.row_estimate}")

        # 类型名按引擎各自表述，不做相等断言 —— 但要把差异**摆出来**
        type_pairs = sorted({f"{c}: {pg_cols[c].data_type}/{sq_cols[c].data_type}"
                             for c in pg_cols
                             if pg_cols[c].data_type != sq_cols[c].data_type})
        if type_pairs:
            info(f"[{name}] 类型名差异（预期内）", f"{len(type_pairs)} 列: "
                 + ", ".join(type_pairs[:6]))

    # 生成列是重点：它是 SQLite(table_xinfo) 与 PG(pg_attribute.attgenerated)
    # 两套机制的交汇处
    amount_col = next(c for c in pg_tables["fact_order"].columns if c.name == "amount")
    check(amount_col.generated, "fact_order.amount 在 PG 被识别为生成列")


# --------------------------------------------------------------- C
def section_c(pg_adapter: PostgresAdapter, sqlite_adapter: SqliteAdapter,
              model: object) -> dict[str, str]:
    """30 题：同一份逻辑计划，分别按两边能力编译执行，比结果集。"""
    print("\n[C] 30 题逐题等价（逻辑计划 → 各自方言编译 → 执行 → 比结果集）")
    from nl2sql.dialect import SqlCompiler

    pg_comp = SqlCompiler(pg_adapter.capabilities)
    sq_comp = SqlCompiler(sqlite_adapter.capabilities)

    status: dict[str, str] = {}
    for qid in sorted(PLANS):
        plan = PLANS[qid]
        sql_s = sq_comp.compile(plan, model).sql
        sql_p = pg_comp.compile(plan, model).sql

        try:
            r_s = sqlite_adapter.execute(sql_s, limit=5000, timeout_s=15)
        except Exception as exc:  # noqa: BLE001
            check(False, f"[{qid}] SQLite 执行失败", f"{exc}")
            status[qid] = "sqlite_error"
            continue

        try:
            r_p = pg_adapter.execute(sql_p, limit=5000, timeout_s=15)
        except Exception as exc:  # noqa: BLE001
            check(False, f"[{qid}] PostgreSQL 执行失败", f"{str(exc)[:160]}")
            status[qid] = "postgres_error"
            continue

        ok = rows_equal(r_s.rows, r_p.rows)
        check(ok, f"[{qid}] 结果集一致",
              "" if ok else f"SQLite {len(r_s.rows)} 行 vs PG {len(r_p.rows)} 行")
        status[qid] = "equal" if ok else "row_mismatch"

    counts: dict[str, int] = {}
    for v in status.values():
        counts[v] = counts.get(v, 0) + 1
    print(f"\n  汇总: {counts}")
    return status


# --------------------------------------------------------------- D
def section_d(model: object, port: int) -> None:
    """端到端：同一个 NL2SQL 装配，换数据源，跑完 30 题。"""
    print("\n[D] 端到端：两引擎各跑一遍黄金集（规则规划器）")
    from nl2sql import NL2SQL, NL2SQLConfig

    with open(BASELINE, encoding="utf-8") as f:
        cases = json.load(f)["cases"]

    def run(engine_name: str, configure) -> tuple[str, dict[str, bool]]:
        cfg = NL2SQLConfig(state_dir=os.path.join(WORK, f"state_{engine_name}"))
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
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
                    result[c["id"]] = False
        return engine_name, result

    sq_db = os.path.join(WORK, "golden.db")
    shutil.copy2(GOLDEN_DB, sq_db)

    _, sq = run("sqlite", lambda cfg: cfg.add_sqlite(sq_db, source_id="shop"))
    _, pg = run("postgres", lambda cfg: cfg.add_postgres(
        host="127.0.0.1", port=port, user=PG_USER, database=DB_NAME,
        source_id="shop"))

    ids = sorted(sq)
    sq_pass, pg_pass = sum(sq.values()), sum(pg.values())
    check(sq_pass == pg_pass, "两引擎通过题数相同",
          f"SQLite {sq_pass}/{len(ids)} vs PostgreSQL {pg_pass}/{len(ids)}")

    diff = [i for i in ids if sq.get(i) != pg.get(i)]
    check(not diff, "逐题通过与否完全相同", f"分歧题: {diff}")

    # 一致性比对用"同源判分"，所以这里再确认一下绝对值
    print(f"\n  SQLite     {sq_pass}/{len(ids)} = {sq_pass / len(ids):.1%}")
    print(f"  PostgreSQL {pg_pass}/{len(ids)} = {pg_pass / len(ids):.1%}")
    failed = [i for i in ids if not sq.get(i)]
    print(f"  失败题（两边相同）: {failed}")


# --------------------------------------------------------------- E
def section_e(adapter: PostgresAdapter) -> None:
    """三层防线里的第二层：服务端只读事务（适配器）+ 错误翻译。"""
    print("\n[E] 只读与错误翻译")

    # 第二层防线：只读事务在服务端拒绝写
    try:
        adapter.execute("DELETE FROM dim_channel WHERE channel_id = 1",
                        limit=10, timeout_s=10)
        check(False, "只读事务拒绝 DELETE", "居然执行成功了，这是严重问题")
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        check("25006" in text or "只读" in text, "只读防线拒绝 DELETE", text[:120])

    # 错误提示必须可行动
    try:
        adapter.execute("SELECT nonexistent_col FROM fact_order", limit=5, timeout_s=10)
        check(False, "不存在的列应报错")
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        check("42703" in text, "错误码带可行动提示", text[:140])

    # 方言差异提示：SQLite 的 strftime 在 PG 上不存在（42883）
    try:
        adapter.execute(
            "SELECT strftime('%Y-%m', order_date) FROM fact_order LIMIT 1",
            limit=5, timeout_s=10)
        check(False, "SQLite 函数在 PG 上应报错")
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        check("42883" in text or "does not exist" in text,
              "函数不存在给出可行动提示", text[:140])


# --------------------------------------------------------------- F
def section_f(port: int) -> None:
    """壳层：`NL2SQL_ADAPTER=postgres` 时真的装配成 PG 数据源。

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
        "NL2SQL_ADAPTER": "postgres",
        "NL2SQL_PG_HOST": "127.0.0.1",
        "NL2SQL_PG_PORT": str(port),
        "NL2SQL_PG_USER": PG_USER,
        "NL2SQL_PG_DATABASE": DB_NAME,
        "NL2SQL_STATE_DIR": os.path.join(WORK, "state_shell"),
        "NL2SQL_PLANNER": "rule",
    }
    shell = read_env(env)
    check(shell.adapter == "postgres", "读到 NL2SQL_ADAPTER", shell.adapter)
    check(shell.pg_database == DB_NAME, "读到 NL2SQL_PG_DATABASE", shell.pg_database)
    check("password" not in " ".join(shell.raw_env), "壳层配置里不带口令")

    cfg, provider = build_shell_config(shell)
    src = cfg.resolve_source()
    check(src.adapter == "postgres", "装配成 PostgreSQL 数据源", src.adapter)
    check(src.credential_ref == "env:NL2SQL_PG_PASSWORD",
          "口令来源是环境变量引用，不是明文", str(src.credential_ref))
    check(provider is None or type(provider).__name__ == "NullLLM",
          "无 Key 时退化为规则规划器", type(provider).__name__)

    # 真的问一句：走完整链路（元数据 / 规划 / 编译 / 守门 / 执行）
    from nl2sql import NL2SQL
    with NL2SQL(cfg, semantic_model=build_model(order_level=True)) as engine:
        engine.refresh_snapshot()
        out = engine.ask("2024年上半年实收金额是多少", now=NOW, use_cache=False)
        ok = bool(out.ok and out.result is not None and out.result.rows)
        got = out.result.rows[0][0] if ok else None
        check(ok, "经壳层问一句能出结果", str(out.status))
        if ok:
            info("PostgreSQL 上算出的金额", f"{got}")


# --------------------------------------------------------------- main
def main() -> int:
    os.makedirs(DATA_ROOT, exist_ok=True)

    bin_dir = find_pg_bin()
    if bin_dir is None:
        print("跳过 PostgreSQL 验收：找不到 PostgreSQL 二进制。")
        print("安装方式（不需要管理员权限 / docker）：")
        print("    pip install embedded-postgres")
        print("    # 或把 bin 目录指给 NL2SQL_PG_BIN（含 postgres.exe 的目录）")
        print("（这不是通过 —— 未跑 ≠ 通过。请安装后重跑）")
        return 0

    print(f"PostgreSQL bin: {bin_dir}")
    server = EmbeddedPostgres(bin_dir, PGDATA)
    try:
        server.start()
        print(f"PostgreSQL 已在 127.0.0.1:{server.port} 启动")
        if not load_golden(server.port):
            return 1

        model = build_model(order_level=True)
        sqlite_adapter = SqliteAdapter(GOLDEN_DB)
        pg_adapter = make_pg(server.port)
        try:
            section_a(pg_adapter)
            section_b(pg_adapter, sqlite_adapter)
            section_c(pg_adapter, sqlite_adapter, model)
            section_e(pg_adapter)
            section_f(server.port)
            section_d(model, server.port)
        finally:
            pg_adapter.close()
            sqlite_adapter.close()
    except Exception:  # noqa: BLE001 - 启动失败时给出完整栈，不要只留一行
        traceback.print_exc()
        _failures.append("PostgreSQL 实例启动/加载失败")
    finally:
        server.stop()

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
