"""校验黄金集数据库完整性（表清单与行数）。"""
import io
import sqlite3
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DB = r"golden_set\ecommerce.db"
EXPECTED = {
    "dim_channel": 5,
    "dim_customer": 12,
    "dim_date": 182,
    "dim_product": 16,
    "fact_order": 810,
}

conn = sqlite3.connect(DB)
tables = sorted(r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type = 'table'"))
print("表清单:", tables)

ok = True
unexpected = [t for t in tables if t not in EXPECTED]
if unexpected:
    ok = False
    print("发现意外表:", unexpected)

for t, want in EXPECTED.items():
    got = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    flag = "ok " if got == want else "FAIL"
    if got != want:
        ok = False
    print(f"  {flag} {t:14s} {got} (期望 {want})")
conn.close()

print("\n结论:", "黄金集数据库完整" if ok else "黄金集数据库异常")
sys.exit(0 if ok else 1)
