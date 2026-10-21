"""把黄金集从 SQLite 灌进 MySQL，并核对两边数据一致。

为什么不是"跑一遍 seed_data.py 再插一遍"：
    那样要维护两套造数逻辑，一旦哪天改了其中一份，两边就不等价了，
    而"等价性验收"的前提恰恰是**数据完全相同**。
    所以这里只做一件事：把 SQLite 里的行原样搬过去。

密码只从环境变量 `NL2SQL_MYSQL_PASSWORD` 读，不接受命令行参数 ——
命令行会进 shell 历史，也会进进程列表。

用法：
    $env:NL2SQL_MYSQL_PASSWORD = <你的口令>
    python scripts/mysql_load_golden.py --database nl2sql_golden
    python scripts/mysql_load_golden.py --dry-run          # 只打印计划
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SQLITE_DB = ROOT / "golden_set" / "ecommerce.db"
MYSQL_DDL = ROOT / "golden_set" / "schema_mysql.sql"

# 搬运顺序要遵守外键直觉（虽然本模型没建外键），维度在前、事实在后更好读日志
TABLES = ("dim_date", "dim_customer", "dim_product", "dim_channel", "fact_order")

# 这些列在 MySQL 里是 DECIMAL，用 Decimal 传，不要用 float
MONEY_COLUMNS = {
    "dim_product": {"unit_cost"},
    "fact_order": {"unit_price", "discount_amount", "amount"},
}


def split_sql(text: str) -> list[str]:
    """按 ';' 切语句，顺带剥掉 '--' 行注释。

    为什么不直接一次 multi-statement 发过去：pymysql 默认不开
    MULTI_STATEMENTS，开了以后报错位置会变得难查。DDL 只有几十行，
    切一次更省事。
    """
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            continue
        lines.append(line)
    sql = "\n".join(lines)
    return [stmt.strip() for stmt in sql.split(";") if stmt.strip()]


def sqlite_rows(table: str, columns: list[str]) -> list[tuple]:
    con = sqlite3.connect(f"file:{SQLITE_DB}?mode=ro", uri=True)
    try:
        col_sql = ", ".join(f'"{c}"' for c in columns)
        rows = con.execute(f'SELECT {col_sql} FROM "{table}"').fetchall()
    finally:
        con.close()

    money = MONEY_COLUMNS.get(table, set())
    if not money:
        return rows

    out = []
    for row in rows:
        out.append(
            tuple(
                Decimal(repr(v)) if (col in money and v is not None) else v
                for col, v in zip(columns, row)
            )
        )
    return out


def mysql_columns(cur, database: str, table: str) -> list[str]:
    """取可写的列（排除生成列）。生成列不能出现在 INSERT 里。"""
    cur.execute(
        """
        SELECT COLUMN_NAME, EXTRA
          FROM information_schema.COLUMNS
         WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
         ORDER BY ORDINAL_POSITION
        """,
        (database, table),
    )
    columns = []
    for name, extra in cur.fetchall():
        if "GENERATED" in (extra or "").upper():
            print(f"  [跳过生成列] {table}.{name}（{extra}）")
            continue
        columns.append(name)
    return columns


def verify(cur, database: str) -> list[str]:
    """核对两边行数与金额合计。返回问题列表，空表示一致。"""
    problems: list[str] = []
    con = sqlite3.connect(f"file:{SQLITE_DB}?mode=ro", uri=True)
    try:
        for table in TABLES:
            want = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM `{database}`.`{table}`")
            got = cur.fetchone()[0]
            mark = "OK " if want == got else "!!!"
            print(f"  {mark} {table:14} sqlite={want:4}  mysql={got:4}")
            if want != got:
                problems.append(f"{table} 行数不一致：{want} vs {got}")

        want = con.execute(
            "SELECT COUNT(*), SUM(quantity), SUM(amount), SUM(discount_amount) "
            "FROM fact_order"
        ).fetchone()
        cur.execute(
            f"SELECT COUNT(*), SUM(quantity), SUM(amount), SUM(discount_amount) "
            f"FROM `{database}`.fact_order"
        )
        got = cur.fetchone()
        labels = ("订单数", "数量合计", "金额合计", "优惠合计")
        for label, a, b in zip(labels, want, got):
            if a is None or b is None:
                continue
            if abs(float(a) - float(b)) > 1e-6:
                problems.append(f"{label} 不一致：{a} vs {b}")
                print(f"  !!! {label:8} sqlite={a}  mysql={b}")
            else:
                print(f"  OK  {label:8} = {b}")

        # 状态分布：退款率这类口径都建立在这上面，值得单独核一次
        want_status = dict(
            con.execute(
                "SELECT order_status, COUNT(*) FROM fact_order GROUP BY 1"
            ).fetchall()
        )
    finally:
        con.close()

    cur.execute(
        f"SELECT order_status, COUNT(*) FROM `{database}`.fact_order GROUP BY 1"
    )
    got_status = dict(cur.fetchall())
    if want_status != got_status:
        problems.append(f"状态分布不一致：{want_status} vs {got_status}")
    else:
        print(f"  OK  状态分布 {got_status}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把黄金集灌进 MySQL")
    parser.add_argument("--host", default=os.environ.get("NL2SQL_MYSQL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("NL2SQL_MYSQL_PORT", "3306")))
    parser.add_argument("--user", default=os.environ.get("NL2SQL_MYSQL_USER", "root"))
    parser.add_argument("--database", default="nl2sql_golden")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要执行的动作")
    args = parser.parse_args(argv)

    password = os.environ.get("NL2SQL_MYSQL_PASSWORD")
    if not password:
        print("缺少环境变量 NL2SQL_MYSQL_PASSWORD（密码不走命令行，避免进历史）")
        return 1
    if not SQLITE_DB.exists():
        print(f"找不到 SQLite 黄金库：{SQLITE_DB}")
        return 1

    try:
        import pymysql
    except ImportError:
        print("需要 pymysql：pip install pymysql")
        return 1

    ddl = split_sql(MYSQL_DDL.read_text(encoding="utf-8"))
    print(f"DDL 语句 {len(ddl)} 条；目标库 `{args.database}` @ {args.host}:{args.port}")
    if args.dry_run:
        for stmt in ddl:
            print("  " + stmt.splitlines()[0][:90])
        print("（dry-run，未连接）")
        return 0

    conn = pymysql.connect(
        host=args.host, port=args.port, user=args.user, password=password,
        charset="utf8mb4", autocommit=False,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{args.database}` "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
            cur.execute(f"USE `{args.database}`")
            for stmt in ddl:
                cur.execute(stmt)
            print("建表完成")

            for table in TABLES:
                columns = mysql_columns(cur, args.database, table)
                rows = sqlite_rows(table, columns)
                placeholders = ", ".join(["%s"] * len(columns))
                col_sql = ", ".join(f"`{c}`" for c in columns)
                cur.executemany(
                    f"INSERT INTO `{table}` ({col_sql}) VALUES ({placeholders})", rows
                )
                print(f"  {table:14} 写入 {len(rows):4} 行")

            print("核对：")
            problems = verify(cur, args.database)
        if problems:
            conn.rollback()
            print("\n核对未通过，已回滚：")
            for item in problems:
                print(f"  - {item}")
            return 1
        conn.commit()
    finally:
        conn.close()

    print(f"\n完成。两边数据一致，库 `{args.database}` 已就绪。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
