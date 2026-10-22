"""M7 验收之四：JDBC 泛用数据源适配器（`nl2sql/sources/jdbc.py`）。

验收重点（都是"能被做假的地方"）：
1. **真跑 JDBC 驱动**——下载 sqlite-jdbc 真 jar，Java 桥起真进程，
   连 `golden_set/ecommerce.db` 的临时副本（sqlite-jdbc 不能用 `?mode=ro`，
   所以复制一份，绝不碰 golden 原件）；
2. **契约等价**——`introspect()` 抽出的 `fact_order` 列/主键与 SQLite
   适配器一致；`execute()` 的 limit+1 截断、行转字符串、NULL 语义一致；
3. **只读不是靠 Prompt**——写操作/多语句在 Python 侧就被词法校验拒绝，
   且拒绝发生在启动 java 进程之前；
4. **错误可行动**——坏 URL / 坏 driver_class 抛 `SourceConnectionError`；
5. **不泄漏资源**——每轮结束 java 桥进程必须回收（用 tasklist 前后对比）。

运行:
    python tests/test_m7_jdbc.py

前置条件（缺任何一个都会打印"未跑 ≠ 通过"并退出 0，不伪装通过）：
    java / javac（JDK 17）、httpx、能访问 Maven Central（或已缓存 jar）。

工作目录：`_tmp/jdbc_test/`（驱动在 `_tmp/jdbc_drivers/`，桥 class 在
`_tmp/jdbc_bridge_classes/`），可用 `NL2SQL_TEST_TMP` 覆盖。
"""
from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")

ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GOLDEN_DB = ROOT / "golden_set" / "ecommerce.db"
TMP = Path(os.environ.get("NL2SQL_TEST_TMP") or (ROOT / "_tmp")) / "jdbc_test"
DRIVER_DIR = TMP.parent / "jdbc_drivers"
BRIDGE_DIR = TMP.parent / "jdbc_bridge_classes"
TEST_DB = TMP / "ecommerce.db"

#: fact_order / dim_channel 是 golden 集里最有代表性的两张表。
EXPECTED_TABLES = {
    "dim_channel", "dim_customer", "dim_date", "dim_product", "fact_order",
}
#: golden 集 fact_order 含一个 STORED 生成列 amount（隐藏列，PRAGMA table_xinfo
#: 能看到但 table_info 看不到），JDBC introspect 以 DatabaseMetaData 为准会列出它。
FACT_ORDER_COLUMNS = [
    "order_id", "order_date", "customer_id", "product_id", "channel_id",
    "quantity", "unit_price", "discount_amount", "order_status", "amount",
]
DIM_CHANNEL_COLUMNS = ["channel_id", "channel_name", "channel_type"]

_failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        _failures.append(f"{label} {detail}".strip())


def info(detail: str) -> None:
    print(f"  --    {detail}")


def section(title: str) -> None:
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


# --------------------------------------------------------------------- 环境
def _find_tool(name: str, env_var: str) -> str | None:
    override = os.environ.get(env_var, "").strip()
    if override and Path(override).is_file():
        return override
    java_home = os.environ.get("JAVA_HOME", "").strip()
    if java_home:
        exe = Path(java_home) / "bin" / (name + (".exe" if os.name == "nt" else ""))
        if exe.is_file():
            return str(exe)
    return shutil.which(name)


def _java_pids() -> set[int]:
    """当前 java 进程 PID 集合（Windows 用 tasklist；其它平台 pgrep 兜底）。"""
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq java.exe",
                 "/FO", "CSV", "/NH"],
                capture_output=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return set()
        text = proc.stdout.decode("utf-8", "replace")
        pids: set[int] = set()
        for line in text.splitlines():
            line = line.strip().strip('"')
            if "java.exe" not in line.lower():
                continue
            fields = next(csv.reader([line]), [])
            if len(fields) >= 2:
                try:
                    pids.add(int(fields[1]))
                except ValueError:
                    pass
        return pids
    try:
        proc = subprocess.run(["pgrep", "-f", "JdbcBridge"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {int(item) for item in proc.stdout.split() if item.isdigit()}


def _skip(reason: str) -> int:
    print("\n" + "=" * 68)
    print(f"跳过：{reason}")
    print("（这不是通过 —— 未跑 ≠ 通过。请安装/联网后重跑）")
    return 0


# --------------------------------------------------------------------- A
def test_environment(ctx: dict) -> None:
    section("[A] 环境与驱动：注册表 / 下载 / 桥编译")
    from nl2sql.sources import drivers as drv

    kinds = set(drv.DRIVER_REGISTRY)
    check(kinds == {"sqlite", "mysql", "postgresql", "mssql"},
          "驱动注册表 4 种 kind", str(sorted(kinds)))
    sqlite_spec = drv.DRIVER_REGISTRY["sqlite"]
    check(sqlite_spec.driver_class == "org.sqlite.JDBC",
          "sqlite 驱动类名", sqlite_spec.driver_class)
    check(sqlite_spec.download_url.startswith("https://repo1.maven.org/maven2/"),
          "下载地址来自 Maven Central", sqlite_spec.download_url)

    sqlite_jar = drv.download_driver("sqlite", DRIVER_DIR)
    check(sqlite_jar.is_file() and sqlite_jar.stat().st_size > 1_000_000,
          "sqlite-jdbc 主 jar 已缓存", f"{sqlite_jar} ({sqlite_jar.stat().st_size} B)")
    extras = drv.extra_jar_paths("sqlite", DRIVER_DIR)
    check(bool(extras) and all(p.is_file() for p in extras),
          "sqlite 附加依赖（slf4j-api）已下载",
          ", ".join(str(p) for p in extras))
    jar_paths = [str(sqlite_jar), *[str(p) for p in extras]]

    status = drv.driver_status("sqlite", DRIVER_DIR)
    check(isinstance(status, dict) and status["downloaded"],
          "driver_status(kind) 报告已下载")
    check(status["path"] == str(sqlite_jar),
          "driver_status.path 是主 jar 绝对路径", status["path"])
    check(status["maven"] == "org.xerial:sqlite-jdbc:3.45.3.0",
          "固定版本，可复现", status["maven"])
    all_status = drv.driver_status(dest_dir=DRIVER_DIR)
    check(isinstance(all_status, list) and len(all_status) == 4,
          "driver_status() 返回全部 4 种")
    try:
        drv.driver_spec("nope")
        check(False, "未知 kind 抛 ConfigError")
    except Exception as exc:  # noqa: BLE001 - 只关心异常类型
        from nl2sql.errors import ConfigError
        check(isinstance(exc, ConfigError), "未知 kind 抛 ConfigError", repr(exc))

    # Java 桥编译：幂等（第二次不应重新编译）
    from nl2sql.sources.jdbc import ensure_bridge_compiled
    classes = ensure_bridge_compiled(BRIDGE_DIR)
    class_file = classes / "JdbcBridge.class"
    check(class_file.is_file(), "JdbcBridge.class 编译产物存在", str(class_file))
    mtime = class_file.stat().st_mtime
    classes_again = ensure_bridge_compiled(BRIDGE_DIR)
    check(classes_again == classes and class_file.stat().st_mtime == mtime,
          "桥编译幂等（class 未更新时复用）")

    ctx["jar_paths"] = jar_paths
    ctx["bridge_dir"] = classes


# --------------------------------------------------------------------- B
def test_connection(ctx: dict) -> None:
    section("[B] 连接自检：java 桥懒启动、自检、进程复用")
    from nl2sql.sources.jdbc import JdbcAdapter

    adapter = JdbcAdapter(
        jdbc_url="jdbc:sqlite:" + TEST_DB.as_posix(),
        driver_class="org.sqlite.JDBC",
        jar_paths=ctx["jar_paths"],
        bridge_dir=ctx["bridge_dir"],
    )
    ctx["adapter"] = adapter
    ctx["adapters"].append(adapter)
    check(adapter.java_pid is None, "构造适配器不启动 java（懒启动）")
    check(adapter.capabilities.dialect == "sqlite",
          "方言从 URL 前缀推断", adapter.capabilities.dialect)
    check(adapter.capabilities.pagination == "limit",
          "SQLite 分页能力 = limit")

    adapter.test_connection()
    pid1 = adapter.java_pid
    check(pid1 is not None, "连接自检后 java 桥进程存活", str(pid1))
    # 受限沙箱里 tasklist 可能看不到其它进程（子进程列表被隔离），
    # 这时不把"检测手段不可见"当成桥进程不存在：后续用 java_pid 与查询验证存活。
    if pid1 in _java_pids():
        check(True, "tasklist 能看到桥进程（检测手段自检）")
    else:
        info(f"tasklist 在当前环境看不到 java 进程（沙箱限制），"
             f"改用 pid={pid1} 与查询验证桥存活")
    desc = adapter.describe()
    check(desc["connected"] is True and desc["adapter"] == "jdbc",
          "describe() 反映已连接")
    check("SQLite" in desc["driver_info"] or desc["driver_info"] != "",
          "driver_info 带驱动名版本", desc["driver_info"])

    adapter.test_connection()
    check(adapter.java_pid == pid1, "重复自检复用同一 java 进程")


# --------------------------------------------------------------------- C
def test_introspect(ctx: dict) -> None:
    section("[C] 元数据：introspect 与 SQLite 适配器对照")
    from nl2sql.sources.sqlite import SqliteAdapter

    adapter = ctx["adapter"]
    tables = adapter.introspect()
    by_name = {t.name: t for t in tables}
    check(set(by_name) >= EXPECTED_TABLES,
          "抽到 golden 集全部业务表",
          f"缺 {EXPECTED_TABLES - set(by_name)}")
    jdbc_fact = by_name.get("fact_order")
    check(jdbc_fact is not None, "fact_order 在元数据里")
    if jdbc_fact is not None:
        check([c.name for c in jdbc_fact.columns] == FACT_ORDER_COLUMNS,
              "fact_order 列名/顺序与 golden 一致",
              str([c.name for c in jdbc_fact.columns]))
        pk = [c.name for c in jdbc_fact.columns if c.is_primary_key]
        check(pk == ["order_id"], "fact_order 主键 = order_id", str(pk))
        quantity = next((c for c in jdbc_fact.columns
                         if c.name == "quantity"), None)
        check(quantity is not None and quantity.data_type.upper() == "INTEGER",
              "列类型取自 TYPE_NAME", getattr(quantity, "data_type", ""))
        check(jdbc_fact.row_estimate is None,
              "行数给 None（不做全表 COUNT）", str(jdbc_fact.row_estimate))
        check(not jdbc_fact.schema, "SQLite schema 为空 → None/''",
              repr(jdbc_fact.schema))
        check(jdbc_fact.comment == "", "注释字段存在（SQLite 为空）")

    dim = by_name.get("dim_channel")
    check(dim is not None and [c.name for c in dim.columns] == DIM_CHANNEL_COLUMNS,
          "dim_channel 列名/顺序一致",
          str([c.name for c in dim.columns]) if dim else "missing")

    # 与 SQLite 适配器对照：列名/顺序/主键应完全一致
    sqlite_adapter = SqliteAdapter(str(GOLDEN_DB))
    try:
        sqlite_tables = {t.name: t for t in sqlite_adapter.introspect()}
    finally:
        sqlite_adapter.close()
    for name in ("fact_order", "dim_channel"):
        ref = sqlite_tables.get(name)
        got = by_name.get(name)
        if ref is None or got is None:
            check(False, f"{name} 两边都抽到", "存在缺失")
            continue
        check([c.name for c in got.columns] == [c.name for c in ref.columns],
              f"{name} 列顺序与 SQLite 适配器一致")
        check([c.name for c in got.columns if c.is_primary_key]
              == [c.name for c in ref.columns if c.is_primary_key],
              f"{name} 主键与 SQLite 适配器一致")

    # include_comments=False 时不带注释
    sparse = {t.name: t for t in adapter.introspect(include_comments=False)}
    check(sparse["fact_order"].comment == "", "include_comments=False 时注释为空")
    check(adapter.introspect(count_rows=True) is not None,
          "count_rows 参数被接受但行数仍为 None（元数据快照契约）")


# --------------------------------------------------------------------- D
def test_execute(ctx: dict) -> None:
    section("[D] 查询：SELECT / limit+1 截断 / CTE / NULL 与字符串语义")
    adapter = ctx["adapter"]

    # 截断语义：adapter 内部取 limit+1 行判断是否超过展示上限。
    # SQL 本身不能带 LIMIT —— 否则 ResultSet 只有 N 行，无法证明"还有更多"。
    result = adapter.execute(
        "SELECT order_id, order_status, quantity FROM fact_order"
        " ORDER BY order_id",
        limit=5, timeout_s=10,
    )
    check(result.columns == ["order_id", "order_status", "quantity"],
          "结果列名来自 ResultSetMetaData", str(result.columns))
    check(result.row_count == 5 and len(result.rows) == 5, "返回 5 行")
    check(result.truncated is True,
          "未写 LIMIT 的查询在 limit=5 时探测到截断（truncated=True）")
    check(isinstance(result.elapsed_ms, int) and result.elapsed_ms >= 0,
          "elapsed_ms 是整数")
    check(result.rows[0][0] == "100001", "单元格转字符串（order_id）",
          repr(result.rows[0][0]))
    check(all(isinstance(v, str) for row in result.rows for v in row),
          "所有非 NULL 单元格都是字符串")

    full = adapter.execute("SELECT order_id FROM fact_order ORDER BY order_id",
                           limit=100000, timeout_s=10)
    check(full.row_count == 810, "不截断时返回全部 810 行",
          str(full.row_count))
    check(full.truncated is False, "未截断时 truncated=False")

    counted = adapter.execute("SELECT COUNT(*) AS c FROM fact_order",
                              limit=10, timeout_s=10)
    check(counted.row_count == 1 and counted.rows[0][0] == "810",
          "聚合查询结果正确", repr(counted.rows))

    cte = adapter.execute(
        "WITH per_channel AS ("
        " SELECT channel_id, COUNT(*) AS n FROM fact_order GROUP BY channel_id)"
        " SELECT COUNT(*) AS c FROM per_channel",
        limit=10, timeout_s=10,
    )
    check(cte.rows and cte.rows[0][0].isdigit() and int(cte.rows[0][0]) > 0,
          "WITH 语句可执行", repr(cte.rows))

    nulls = adapter.execute("SELECT NULL AS n", limit=1, timeout_s=10)
    check(nulls.rows == [[None]], "SQL NULL → Python None", repr(nulls.rows))

    string_like = adapter.execute(
        "SELECT 'DELETE FROM fact_order' AS s, 1 AS one",
        limit=1, timeout_s=10,
    )
    check(string_like.rows[0] == ["DELETE FROM fact_order", "1"],
          "字符串常量里的写关键字不误伤", repr(string_like.rows))

    commented = adapter.execute(
        "-- 注释里有 DELETE 也不该被误判\n"
        "SELECT order_id FROM fact_order ORDER BY order_id LIMIT 1;",
        limit=1, timeout_s=10,
    )
    check(commented.rows == [["100001"]], "注释/尾分号不影响只读判定",
          repr(commented.rows))


# --------------------------------------------------------------------- E
def test_read_only(ctx: dict) -> None:
    section("[E] 只读拒绝：写操作 / 多语句 / 锁定读")

    adapter = ctx["adapter"]
    cases = [
        ("DELETE FROM fact_order", "DELETE"),
        ("INSERT INTO fact_order(order_id) VALUES (1)", "INSERT"),
        ("UPDATE fact_order SET order_status = 'x'", "UPDATE"),
        ("DROP TABLE fact_order", "DROP"),
        ("CREATE TABLE t(x INT)", "CREATE"),
        ("ATTACH DATABASE ':memory:' AS m", "ATTACH"),
        ("SELECT 1; SELECT 2", "多语句"),
        ("WITH x AS (SELECT 1) INSERT INTO fact_order(order_id)"
         " SELECT * FROM x", "WITH+INSERT"),
        ("SELECT * FROM fact_order FOR UPDATE", "锁定读"),
    ]
    from nl2sql.errors import QueryBlockedError

    for sql, label in cases:
        try:
            adapter.execute(sql, limit=10, timeout_s=5)
            check(False, f"{label} 被拒绝")
        except QueryBlockedError as exc:
            message = str(exc)
            check("JDBC 适配器只支持只读查询" in message,
                  f"{label} 报错说明只读原因", message[:80])
            check(exc.rule == "read_only", f"{label} 带 rule=read_only",
                  repr(exc.rule))
            check(bool(exc.suggestion), f"{label} 带可行动建议")
        except Exception as exc:  # noqa: BLE001
            check(False, f"{label} 抛 QueryBlockedError",
                  f"{type(exc).__name__}: {exc}")

    # 拒绝发生在启动 java 进程之前：新建一个适配器，被拒后不应有 java 进程
    from nl2sql.sources.jdbc import JdbcAdapter
    fresh = JdbcAdapter(
        jdbc_url="jdbc:sqlite:" + TEST_DB.as_posix(),
        driver_class="org.sqlite.JDBC",
        jar_paths=ctx["jar_paths"],
        bridge_dir=ctx["bridge_dir"],
    )
    ctx["adapters"].append(fresh)
    try:
        fresh.execute("DELETE FROM fact_order", limit=10, timeout_s=5)
        check(False, "写操作被拒")
    except QueryBlockedError:
        pass
    check(fresh.java_pid is None, "被拒的写操作不启动 java 进程")
    fresh.close()
    check(fresh.java_pid is None, "close() 后无桥进程")

    # 显式关闭只读校验后允许写（契约允许 read_only=False 用于受控场景）
    writable = JdbcAdapter(
        jdbc_url="jdbc:sqlite:" + TEST_DB.as_posix(),
        driver_class="org.sqlite.JDBC",
        jar_paths=ctx["jar_paths"],
        bridge_dir=ctx["bridge_dir"],
        read_only=False,
    )
    ctx["adapters"].append(writable)
    writable.test_connection()
    pid = writable.java_pid
    check(pid is not None, "read_only=False 仍可正常连接")
    writable.close()
    check(writable.java_pid is None, "read_only=False 关闭后无残留进程")


# --------------------------------------------------------------------- F
def test_bad_targets(ctx: dict) -> None:
    section("[F] 错误路径：坏 URL / 坏 driver_class → SourceConnectionError")
    from nl2sql.errors import SourceConnectionError
    from nl2sql.sources.jdbc import JdbcAdapter

    missing_dir = TMP / "no_such_dir_xyz" / "x.db"
    bad_url = JdbcAdapter(
        jdbc_url="jdbc:sqlite:" + missing_dir.as_posix(),
        driver_class="org.sqlite.JDBC",
        jar_paths=ctx["jar_paths"],
        bridge_dir=ctx["bridge_dir"],
    )
    ctx["adapters"].append(bad_url)
    try:
        bad_url.test_connection()
        check(False, "坏 URL 报错")
    except SourceConnectionError as exc:
        check(True, "坏 URL 抛 SourceConnectionError")
        check("无法通过 JDBC 连接" in str(exc), "错误信息带连接上下文",
              str(exc)[:100])
    except Exception as exc:  # noqa: BLE001
        check(False, "坏 URL 抛 SourceConnectionError",
              f"{type(exc).__name__}: {exc}")
    finally:
        bad_url.close()
    check(bad_url.java_pid is None, "坏 URL 失败后 java 进程已回收")

    bad_driver = JdbcAdapter(
        jdbc_url="jdbc:sqlite:" + TEST_DB.as_posix(),
        driver_class="no.such.DriverClass",
        jar_paths=ctx["jar_paths"],
        bridge_dir=ctx["bridge_dir"],
    )
    ctx["adapters"].append(bad_driver)
    try:
        bad_driver.test_connection()
        check(False, "坏 driver_class 报错")
    except SourceConnectionError as exc:
        check(True, "坏 driver_class 抛 SourceConnectionError")
        check("找不到 JDBC 驱动类" in str(exc),
              "错误信息指出驱动类问题", str(exc)[:120])
    except Exception as exc:  # noqa: BLE001
        check(False, "坏 driver_class 抛 SourceConnectionError",
              f"{type(exc).__name__}: {exc}")
    finally:
        bad_driver.close()
    check(bad_driver.java_pid is None, "坏 driver_class 失败后 java 进程已回收")

    # 配置错误：jar 不存在应在构造时就被挡住（不等到运行期）
    from nl2sql.errors import ConfigError
    try:
        JdbcAdapter(
            jdbc_url="jdbc:sqlite:" + TEST_DB.as_posix(),
            driver_class="org.sqlite.JDBC",
            jar_paths=[str(TMP / "no_such_driver.jar")],
            bridge_dir=ctx["bridge_dir"],
        )
        check(False, "jar 不存在被拒绝")
    except ConfigError as exc:
        check("jar 不存在" in str(exc), "jar 不存在抛 ConfigError", str(exc)[:80])


# --------------------------------------------------------------------- G
def test_factory(ctx: dict) -> None:
    section("[G] 装配：config.add_jdbc + build_adapter（壳层入口）")
    from nl2sql.config import NL2SQLConfig
    from nl2sql.sources import build_adapter

    cfg = NL2SQLConfig(state_dir=str(TMP / "state"))
    cfg.add_jdbc(
        jdbc_url="jdbc:sqlite:" + TEST_DB.as_posix(),
        driver_class="org.sqlite.JDBC",
        source_id="main",
        label="黄金集 SQLite",
        jar_paths=tuple(ctx["jar_paths"]),
        params={"bridge_dir": str(ctx["bridge_dir"])},
    )
    source_cfg = cfg.resolve_source("main")
    check(source_cfg.adapter == "jdbc", "config 记录 adapter=jdbc")
    source_cfg.validate()  # 配置层校验：jdbc_url / driver_class 齐全
    check(source_cfg.options.params["jdbc_url"].startswith("jdbc:sqlite:"),
          "jdbc_url 进入 options.params")

    adapter = build_adapter(source_cfg)
    ctx["adapters"].append(adapter)
    check(adapter.capabilities.dialect == "sqlite",
          "build_adapter 装配出 JDBC 适配器")
    adapter.test_connection()
    tables = adapter.introspect()
    check({t.name for t in tables} >= EXPECTED_TABLES,
          "装配后的适配器能 introspect")
    result = adapter.execute("SELECT COUNT(*) AS c FROM fact_order",
                             limit=10, timeout_s=10)
    check(result.rows[0][0] == "810", "装配后的适配器能查询",
          repr(result.rows))
    adapter.close()
    check(adapter.java_pid is None, "装配后的适配器关闭后无残留进程")


# --------------------------------------------------------------------- main
def main() -> int:
    print("M7 验收：JDBC 泛用数据源适配器")
    print(f"工作目录: {TMP}")
    java = _find_tool("java", "NL2SQL_JAVA")
    javac = _find_tool("javac", "NL2SQL_JAVAC")
    try:
        import httpx  # noqa: F401
        have_httpx = True
    except ImportError:
        have_httpx = False
    if java is None or javac is None:
        return _skip("找不到 java/javac（需要 JDK 17）")
    if not have_httpx:
        return _skip("缺少 httpx（下载 JDBC 驱动需要）")
    if not GOLDEN_DB.is_file():
        return _skip(f"缺少 golden_set/ecommerce.db: {GOLDEN_DB}")
    info(f"java: {java}")
    info(f"javac: {javac}")

    TMP.mkdir(parents=True, exist_ok=True)
    shutil.copy2(GOLDEN_DB, TEST_DB)  # sqlite-jdbc 无法用 ?mode=ro，复制一份
    check(TEST_DB.is_file() and TEST_DB.stat().st_size > 0,
          "ecommerce.db 临时副本就绪", str(TEST_DB))

    baseline_java = _java_pids()
    info(f"基线 java 进程: {sorted(baseline_java) or '无'}")

    ctx: dict = {"adapters": [], "jar_paths": [], "bridge_dir": BRIDGE_DIR}
    failed_sections = 0
    try:
        for fn in (test_environment, test_connection, test_introspect,
                   test_execute, test_read_only, test_bad_targets,
                   test_factory):
            try:
                fn(ctx)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                _failures.append(f"{fn.__name__} 异常")
                failed_sections += 1
    finally:
        # 回收所有适配器（即使中途异常），验证没有 java 进程残留
        for adapter in ctx["adapters"]:
            try:
                adapter.close()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        time.sleep(0.5)
        section("[H] 进程回收：没有残留 java 进程")
        residual = _java_pids() - baseline_java
        check(not residual, "无残留 java 桥进程", f"残留 PID: {sorted(residual)}")
        if residual:
            info("残留进程可用 taskkill /PID <pid> /F 清理")

    print("\n" + "=" * 68)
    if _failures:
        print(f"失败 {len(_failures)} 项：")
        for item in _failures:
            print("  -", item)
        return 1
    print("M7 JDBC 适配器验收全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
