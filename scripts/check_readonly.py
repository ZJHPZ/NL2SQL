"""清理黄金集数据库中的探针表，并验证 SQLite 只读 URI 行为。"""
import io
import os
import shutil
import sqlite3
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "golden_set", "ecommerce.db")


def cleanup() -> None:
    conn = sqlite3.connect(DB)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    if "zzz_probe" in tables:
        conn.execute("DROP TABLE zzz_probe")
        conn.commit()
        print("已删除污染表 zzz_probe")
    else:
        print("未发现污染表")
    remaining = sorted(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"))
    print("现有表:", remaining)
    conn.close()


def probe_ro_uri() -> None:
    """在临时副本上验证：只读 URI 是否真的阻止写入。"""
    tmp = os.path.join(ROOT, "data", "_roprobe")
    os.makedirs(tmp, exist_ok=True)
    copy = os.path.join(tmp, "copy.db")
    shutil.copy2(DB, copy)

    from nl2sql.sources.sqlite import _to_ro_uri

    uri = _to_ro_uri(copy)
    print("\n只读 URI:", uri)

    # 1) 直接用 URI 连接
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("CREATE TABLE direct_probe (a INT)")
        conn.commit()
        print("  ✗ 直接 URI 连接：写入成功（只读未生效）")
        conn.execute("DROP TABLE direct_probe")
        conn.commit()
    except sqlite3.Error as e:
        print(f"  ✓ 直接 URI 连接：写入被拒 -> {type(e).__name__}: {e}")
    finally:
        conn.close()

    # 2) 通过适配器
    from nl2sql.sources.sqlite import SqliteAdapter

    a = SqliteAdapter(copy)
    try:
        a.execute("CREATE TABLE adapter_probe (a INT)", limit=5, timeout_s=5)
        print("  ✗ 适配器：写入成功（只读未生效）")
    except sqlite3.Error as e:
        print(f"  ✓ 适配器：写入被拒 -> {type(e).__name__}: {e}")
    finally:
        a.close()

    # 3) 对照：非只读模式应当可以写
    conn = sqlite3.connect(copy)
    try:
        conn.execute("CREATE TABLE rw_probe (a INT)")
        conn.commit()
        print("  · 对照（可写模式）：写入成功，说明文件本身可写")
        conn.execute("DROP TABLE rw_probe")
        conn.commit()
    finally:
        conn.close()

    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    cleanup()
    probe_ro_uri()
