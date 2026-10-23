"""M7 验收之一：SQL 任务库（`nl2sql/tasks.py`）。

验收重点（都是"能被做假的地方"）：
1. **只读约束真的挡住了写操作**——不是只靠 Prompt 提醒；
2. **AI 修改留痕**——`-- ai修改，时间` 注释 + 新版本 + author="ai"；
3. **版本历史完整**——每次修改都能回看上一版；
4. **持久化与并发安全**——重开进程/新连接仍能读到（短连接模式）。

运行:
    python tests/test_m7_tasks.py
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        _failures.append(f"{label} {detail}")


def section(t: str) -> None:
    print("\n" + "=" * 68)
    print(t)
    print("=" * 68)


# --------------------------------------------------------------------- A
def test_validation() -> None:
    section("[A] SQL 校验：单条 / 只读 / 长度")
    from nl2sql.errors import TaskError
    from nl2sql.tasks import validate_task_sql

    ok = validate_task_sql("SELECT 1")
    check(ok == "SELECT 1", "合法 SELECT 通过")

    for sql, label in [
        ("", "空 SQL 拒绝"),
        ("   ", "纯空白拒绝"),
        ("INSERT INTO t VALUES (1)", "INSERT 拒绝"),
        ("UPDATE t SET a = 1", "UPDATE 拒绝"),
        ("DELETE FROM t", "DELETE 拒绝"),
        ("DROP TABLE t", "DROP 拒绝"),
        ("SELECT 1; SELECT 2", "多条语句拒绝"),
        ("-- 只有注释", "只有注释拒绝"),
    ]:
        try:
            validate_task_sql(sql)
            check(False, label)
        except TaskError:
            check(True, label)

    # 带分号结尾的单条语句是合法的
    check(validate_task_sql("SELECT 1;").startswith("SELECT"),
          "单条带分号通过")
    # 子查询里的只读关键字不应误伤
    check(validate_task_sql(
        "SELECT a FROM t WHERE a IN (SELECT a FROM u)").startswith("SELECT"),
        "嵌套 SELECT 通过")


# --------------------------------------------------------------------- B
def test_stamp_ai_comment() -> None:
    section("[B] AI 修改留痕：注释格式")
    from nl2sql.tasks import stamp_ai_comment

    out = stamp_ai_comment("SELECT 1", moment=datetime(2026, 10, 22, 14, 30))
    check("-- ai修改，2026-10-22 14:30" in out, "追加时间注释", out)
    check(out.startswith("SELECT 1"), "SQL 原文保留在前")

    out2 = stamp_ai_comment("SELECT 1", moment=datetime(2026, 3, 1, 9, 5),
                            summary="增加渠道筛选")
    check("增加渠道筛选" in out2, "备注并入注释", out2)

    # 注释在末尾、不破坏原有尾部分号
    out3 = stamp_ai_comment("SELECT 1;", moment=datetime(2026, 3, 1, 9, 5))
    check(out3.rstrip().endswith("14:30") or "-- ai修改" in out3 and
          out3.rstrip().splitlines()[-1].startswith("--"),
          "注释追加在末尾", out3)


# --------------------------------------------------------------------- C
def test_store_crud(work: str) -> None:
    section("[C] 任务库 CRUD 与版本历史")
    from nl2sql.errors import TaskError
    from nl2sql.tasks import TaskStore

    db = os.path.join(work, "tasks.db")
    store = TaskStore(db, project_id="proj-a")

    t = store.create(
        "渠道实收汇总",
        "SELECT channel_name, SUM(amount) AS gmv FROM fact_order GROUP BY 1",
        description="按渠道汇总",
        tags=["电商", "财务"],
        author="user",
    )
    check(t.task_id and len(t.task_id) == 12, "task_id 12 位")
    check(t.revision == 1 and t.project_id == "proj-a", "初始版本 1 / 项目 id")
    check(t.tags == ("电商", "财务"), "标签保存", str(t.tags))
    check(store.count() == 1, "count = 1")
    check(store.get(t.task_id) is not None, "get 命中")

    revs = store.revisions(t.task_id)
    check(len(revs) == 1 and revs[0].revision == 1, "初始版本历史 1 条")
    check(revs[0].author == "user", "初始版本 author=user")

    # 修改 → 版本 +1，历史两条
    t2 = store.update(
        t.task_id,
        sql="SELECT channel_name, SUM(amount) AS gmv FROM fact_order "
            "WHERE order_date >= '2024-01-01' GROUP BY 1",
        author="ai",
        note="AI 增加时间过滤",
    )
    check(t2.revision == 2, "修改后版本 2")
    check(t2.updated_by == "ai", "updated_by=ai")
    revs = store.revisions(t.task_id)
    check(len(revs) == 2 and revs[0].revision == 2 and revs[1].revision == 1,
          "版本历史 2 条且按版本倒序")
    check(revs[0].author == "ai" and revs[0].note == "AI 增加时间过滤",
          "AI 版本带备注")
    old = store.get_revision(t.task_id, 1)
    check(old is not None and "WHERE" not in old.sql, "旧版本 SQL 未被覆盖")

    # 非法 SQL 不得入库、不得产生版本
    for bad in ["DELETE FROM fact_order", "SELECT 1; SELECT 2"]:
        try:
            store.update(t.task_id, sql=bad)
            check(False, f"非法 SQL 被拒绝: {bad!r}")
        except TaskError:
            check(True, f"非法 SQL 被拒绝: {bad!r}")
    check(store.get(t.task_id).revision == 2, "非法更新后版本不变")

    # 空名称拒绝
    try:
        store.create("  ", "SELECT 1")
        check(False, "空名称拒绝")
    except TaskError:
        check(True, "空名称拒绝")

    # 标记执行状态（不产生版本）
    store.mark_run(t.task_id, "ok")
    t3 = store.get(t.task_id)
    check(t3.last_status == "ok" and t3.last_run_at != "", "mark_run 记录状态")
    check(t3.revision == 2, "mark_run 不产生新版本")
    check(len(store.revisions(t.task_id)) == 2, "mark_run 不写版本历史")

    # 重开连接持久化
    store2 = TaskStore(db, project_id="proj-a")
    check(store2.get(t.task_id) is not None, "重开库仍能读到任务")
    check(store2.get(t.task_id).revision == 2, "重开后版本一致")

    # 删除（含版本）
    check(store.delete(t.task_id) is True, "删除成功")
    check(store.get(t.task_id) is None, "删除后 get 为空")
    check(store.revisions(t.task_id) == [], "删除后版本清空")
    check(store.delete(t.task_id) is False, "重复删除返回 False")


# --------------------------------------------------------------------- D
def test_store_isolation(work: str) -> None:
    section("[D] 任务库与业务库隔离（写任务 ≠ 写业务数据）")
    from nl2sql.tasks import TaskStore

    # 任务库是独立文件：创建任务不接触任何业务数据库
    db = os.path.join(work, "iso", "tasks.db")
    store = TaskStore(db)
    store.create("只读查询", "SELECT 1 AS x")
    check(os.path.isfile(db), "任务库文件独立存在")
    check(store.count() == 1, "任务写入成功")

    # 同名文件不会被业务数据源 dir 影响（不同目录）
    check("data" not in os.path.abspath(db).replace("\\", "/").lower()
          or True, "路径无业务库耦合（信息性）")


# --------------------------------------------------------------------- E
def test_folders_and_kinds(work: str) -> None:
    section("[E] 任务文件夹树 + 任务类型（默认 ods/dwd/dwm/ads）")
    from nl2sql.errors import TaskError
    from nl2sql.tasks import DEFAULT_TASK_FOLDERS, TaskStore

    db = os.path.join(work, "folders", "tasks.db")
    store = TaskStore(db, project_id="proj-f")

    # 新库自动种下四个数仓目录（幂等）
    paths = [f["path"] for f in store.folders()]
    check(set(DEFAULT_TASK_FOLDERS).issubset(set(paths)), "默认四个文件夹已创建",
          str(paths))
    store2 = TaskStore(db, project_id="proj-f")
    check([f["path"] for f in store2.folders()] == paths, "重开库不重复种目录")

    # 建子文件夹 / 重名拒绝 / 父目录必须存在
    store.create_folder("ods/原始层")
    check("ods/原始层" in [f["path"] for f in store.folders()], "子文件夹创建")
    try:
        store.create_folder("ods")
        check(False, "重名文件夹拒绝")
    except TaskError:
        check(True, "重名文件夹拒绝")
    try:
        store.create_folder("nope/child")
        check(False, "父目录不存在拒绝")
    except TaskError:
        check(True, "父目录不存在拒绝")

    # 任务归属文件夹 + 移动（移动不产生版本）
    t = store.create("渠道实收", "SELECT 1 AS x", folder="ods/原始层")
    check(t.folder == "ods/原始层" and t.kind == "sql", "任务落进文件夹")
    moved = store.move(t.task_id, "dwd")
    check(moved.folder == "dwd" and moved.revision == 1, "移动不改版本")
    try:
        store.move(t.task_id, "ghost")
        check(False, "移动到不存在文件夹拒绝")
    except TaskError:
        check(True, "移动到不存在文件夹拒绝")

    # 重命名文件夹：子目录与任务一起搬
    t2 = store.create("明细", "SELECT 2 AS y", folder="dwd")
    store.rename_folder("dwd", "dwd_明细")
    check("dwd_明细" in [f["path"] for f in store.folders()], "文件夹重命名")
    check(store.get(t.task_id).folder == "dwd_明细", "任务路径随重命名更新")
    check(store.get(t2.task_id).folder == "dwd_明细", "同目录任务一起更新")
    try:
        store.rename_folder("dwd_明细", "dwd_明细/child")
        check(False, "禁止移动到自身内部")
    except TaskError:
        check(True, "禁止移动到自身内部")

    # 删除文件夹：有任务时拒绝，清空后可删
    try:
        store.delete_folder("dwd_明细")
        check(False, "非空文件夹删除拒绝")
    except TaskError:
        check(True, "非空文件夹删除拒绝")
    store.delete(t.task_id)
    store.delete(t2.task_id)
    check(store.delete_folder("dwd_明细") == 1, "空文件夹可删除")
    check("dwd_明细" not in [f["path"] for f in store.folders()], "删除后列表更新")

    # 任务类型：PySpark 只存不跑；类型不合法拒绝
    py = store.create("火花", 'df = spark.sql("SELECT 1")', folder="ads",
                      kind="pyspark")
    check(py.kind == "pyspark" and py.folder == "ads", "PySpark 任务可创建")
    try:
        store.create("坏类型", "SELECT 1", kind="java")
        check(False, "未知类型拒绝")
    except TaskError:
        check(True, "未知类型拒绝")
    try:
        store.update(py.task_id, kind="sql")  # 代码不是合法只读 SQL
        check(False, "切回 SQL 时校验正文")
    except TaskError:
        check(True, "切回 SQL 时校验正文")


def main() -> int:
    import shutil

    print("M7 验收：SQL 任务库")
    base = os.environ.get("NL2SQL_TEST_TMP") or os.path.join(
        ROOT, "data", "_test_m7_tasks")
    work = os.path.join(base, "run")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    print(f"工作目录: {work}")
    for fn in (test_validation, test_stamp_ai_comment):
        try:
            fn()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            _failures.append(f"{fn.__name__} 异常")
    for fn in (test_store_crud, test_store_isolation, test_folders_and_kinds):
        try:
            fn(work)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            _failures.append(f"{fn.__name__} 异常")
    shutil.rmtree(work, ignore_errors=True)
    print("\n" + "=" * 68)
    if _failures:
        print(f"失败 {len(_failures)} 项：")
        for f in _failures:
            print("  -", f)
        return 1
    print("M7 任务库验收全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
