"""受控写守门与执行器测试（P1）。

跑法：在仓库根执行 `python tests/test_write_guard.py`
（脚本式测试，与 tests/test_m7_tasks.py 保持一致：不依赖 pytest）。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nl2sql.guard.write import WriteGuard  # noqa: E402
from nl2sql.tasks import (  # noqa: E402
    TASK_KIND_WRITE,
    TaskError,
    TaskStore,
    validate_task_content,
)
from apps.web.backend import write_runner  # noqa: E402

_FAILED: list[str] = []
_CHECKS = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _CHECKS
    _CHECKS += 1
    if not ok:
        _FAILED.append(f"{name}{('：' + detail) if detail else ''}")
        print(f"  FAIL {name}{('：' + detail) if detail else ''}")


def _deny(guard: WriteGuard, sql: str, *, rule: str = "") -> None:
    result = guard.check(sql)
    check(f"deny {sql[:40]!r}", not result.allowed and bool(result.reason),
          f"allowed={result.allowed} rule={result.rule}")
    if rule:
        check(f"rule {rule}", result.rule == rule, f"got {result.rule}")


def test_guard() -> None:
    print("[A] 写语句守门")
    guard = WriteGuard()
    ok_cases = [
        "INSERT INTO fact_order (order_id, amount) VALUES ('o1', 10.5)",
        "INSERT INTO main.fact_order (order_id) VALUES ('o2')",
        "UPDATE fact_order SET amount = 1 WHERE order_id = 'o1'",
        "DELETE FROM fact_order WHERE order_id = 'o2'",
        "UPDATE dim_product SET name = 'x' WHERE product_id IN (1, 2)",
    ]
    for sql in ok_cases:
        result = guard.check(sql)
        check(f"allow {sql[:40]!r}", result.allowed, result.reason)

    # 字符串/注释里的关键字不能影响判断
    check(
        "string literal safe",
        guard.check(
            "UPDATE fact_order SET note = 'DROP TABLE x where 1=1' "
            "WHERE order_id = 'o1'"
        ).allowed,
    )

    _deny(guard, "", rule="empty")
    _deny(guard, "SELECT 1", rule="not_write")
    _deny(guard, "INSERT INTO t (a) VALUES (1); DELETE FROM t WHERE a=1",
          rule="multi_statement")
    _deny(guard, "DROP TABLE fact_order", rule="not_write")
    _deny(guard, "UPDATE fact_order SET amount = 1", rule="missing_where")
    _deny(guard, "DELETE FROM fact_order", rule="missing_where")
    _deny(guard, "UPDATE fact_order SET a=1 WHERE 1=1", rule="missing_where")
    _deny(guard, "UPDATE fact_order SET a=1 WHERE true", rule="missing_where")
    _deny(guard, "UPDATE fact_order SET a=1 WHERE 1=1 OR order_id='x'",
          rule="missing_where")
    _deny(guard, "DELETE FROM fact_order WHERE 1", rule="missing_where")
    _deny(guard, "INSERT INTO fact_order (a) SELECT amount FROM t",
          rule="insert_select")
    _deny(guard, "INSERT INTO sqlite_master (a) VALUES (1)",
          rule="internal_table")
    _deny(guard, "UPDATE fact_order SET a=1 WHERE order_id=1 UNION SELECT 1",
          rule="forbidden_keyword")
    _deny(guard, "ALTER TABLE fact_order ADD COLUMN x TEXT", rule="not_write")
    _deny(guard, "INSERT INTO fact_order (a) VALUES (1) RETURNING a",
          rule="forbidden_keyword")
    blocked = WriteGuard(blocked_tables=["secret_table"])
    _deny(blocked, "DELETE FROM secret_table WHERE id = 1",
          rule="blocked_table")

    try:
        validate_task_content(TASK_KIND_WRITE, "UPDATE t SET a=1 WHERE 1=1")
        check("validate_task_content rejects tautology", False)
    except TaskError:
        check("validate_task_content rejects tautology", True)
    check(
        "validate_task_content accepts write",
        validate_task_content(
            TASK_KIND_WRITE, "UPDATE fact_order SET amount=2 WHERE order_id='o1'"
        ).startswith("UPDATE"),
    )


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE fact_order (
            order_id TEXT PRIMARY KEY,
            amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'new'
        );
        INSERT INTO fact_order (order_id, amount, status)
        VALUES ('o1', 10.0, 'new'), ('o2', 20.0, 'new'), ('o3', 30.0, 'new');
        """
    )
    conn.commit()
    conn.close()


def _amounts(path: Path) -> dict[str, float]:
    conn = sqlite3.connect(str(path))
    rows = conn.execute("SELECT order_id, amount FROM fact_order").fetchall()
    conn.close()
    return {str(r[0]): float(r[1]) for r in rows}


def test_runner() -> None:
    print("[B] 预演 / 提交执行器")
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "demo.db"
        _make_db(db)
        guard = WriteGuard()
        update = guard.check(
            "UPDATE fact_order SET amount = 99 WHERE order_id = 'o1'"
        )
        check("guard allows update", update.allowed, update.reason)

        preview = write_runner.dry_run(str(db), update, max_affected=10)
        check("dry rows", preview["rows"] == 1, str(preview["rows"]))
        check("dry before sample", len(preview["before"]) == 1)
        check("dry after sample amount", preview["after"][0]["amount"] == 99.0)
        check("dry rolled back", _amounts(db)["o1"] == 10.0)

        done = write_runner.commit(str(db), update, max_affected=10)
        check("commit rows", done["rows"] == 1, str(done["rows"]))
        check("commit persisted", _amounts(db)["o1"] == 99.0)

        over = guard.check("UPDATE fact_order SET status = 'paid'")
        check("no-where rejected", not over.allowed)
        over = guard.check(
            "UPDATE fact_order SET status = 'paid' WHERE amount > 0"
        )
        try:
            write_runner.dry_run(str(db), over, max_affected=2)
            check("limit rollback", False, "expected WriteRunError")
        except write_runner.WriteRunError as exc:
            check("limit rule", exc.rule == "affected_limit", exc.rule)
        check("limit left data untouched",
              _amounts(db)["o2"] == 20.0 and _amounts(db)["o3"] == 30.0)

        insert = guard.check(
            "INSERT INTO fact_order (order_id, amount) VALUES ('o9', 1.0)"
        )
        ins = write_runner.dry_run(str(db), insert, max_affected=10)
        check("insert dry rows", ins["rows"] == 1, str(ins["rows"]))
        check("insert not persisted", "o9" not in _amounts(db))
        write_runner.commit(str(db), insert, max_affected=10)
        check("insert committed", _amounts(db)["o9"] == 1.0)

        delete = guard.check("DELETE FROM fact_order WHERE order_id = 'o9'")
        write_runner.commit(str(db), delete, max_affected=10)
        check("delete committed", "o9" not in _amounts(db))

        # 任务库不该被写：路由器负责拦截，这里确认执行器只认传入路径
        check("runner accepts explicit path", db.exists())


def test_task_store() -> None:
    print("[C] 任务库：写入 kind + 预演凭据 + 审计")
    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(str(Path(tmp) / "tasks.db"), project_id="wtest")
        task = store.create(
            "写测试", "UPDATE fact_order SET amount=1 WHERE order_id='o1'",
            kind=TASK_KIND_WRITE,
        )
        check("kind write", task.kind == TASK_KIND_WRITE, task.kind)
        guard = WriteGuard()
        check_result = guard.check(task.sql)
        saved = store.save_dry_run(
            task_id=task.task_id, revision=task.revision, source_id="src1",
            check=check_result, rows=1,
            before=[{"order_id": "o1", "amount": 10.0}],
            after=[{"order_id": "o1", "amount": 1.0}],
        )
        loaded = store.get_dry_run(saved["dry_run_id"])
        check("dry run saved", loaded is not None)
        assert loaded is not None
        check("dry run rows", loaded["rows"] == 1)
        check("dry run before", loaded["before"][0]["amount"] == 10.0)
        audit_id = store.record_write_audit(
            task=task, source_id="src1", check=check_result,
            status="preview", rows=1,
        )
        check("audit id", audit_id > 0, str(audit_id))
        store.delete_dry_run(saved["dry_run_id"])
        check("dry run deleted", store.get_dry_run(saved["dry_run_id"]) is None)
        rows = store.list_write_audit(limit=10)
        check("audit listed", len(rows) == 1 and rows[0]["status"] == "preview")


def main() -> int:
    test_guard()
    test_runner()
    test_task_store()
    print(f"\n{_CHECKS} checks, {len(_FAILED)} failed")
    if _FAILED:
        for line in _FAILED:
            print(" -", line)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
