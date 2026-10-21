"""把黄金集从 SQLite 灌进 PostgreSQL，并核对两边数据一致。

为什么不是"再跑一遍 seed_data.py 再插一遍"：
    那样要维护两套造数逻辑，一旦哪天改了其中一份，两边就不等价了，
    而"等价性验收"的前提恰恰是**数据完全相同**。
    所以这里只做一件事：把 SQLite 里的行原样搬过去。
    与 `mysql_load_golden.py` 的搬运口径逐条对齐（金额用 Decimal、
    行数/合计/状态分布三项核对），换的只是驱动与方言。

密码只从环境变量 `NL2SQL_PG_PASSWORD` 读，不接受命令行参数 ——
命令行会进 shell 历史，也会进进程列表。本地 trust 认证的库可以不给：
要不要口令由服务端决定，这里不替它把关。

用法：
    $env:NL2SQL_PG_PASSWORD = <你的口令>      # trust 认证可省略
    python scripts/pg_load_golden.py --database nl2sql_golden
    python scripts/pg_load_golden.py --dry-run          # 只打印计划
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
PG_DDL = ROOT / "golden_set" / "schema_postgres.sql"

# 搬运顺序要遵守外键直觉（虽然本模型没建外键），维度在前、事实在后更好读日志
TABLES = ("dim_date", "dim_customer", "dim_product", "dim_channel", "fact_order")

# 这些列在 PostgreSQL 里是 NUMERIC，用 Decimal 传，不要用 float
MONEY_COLUMNS = {
    "dim_product": {"unit_cost"},
    "fact_order": {"unit_price", "discount_amount", "amount"},
}

SCHEMA = "public"


def split_sql(text: str) -> list[str]:
    """按 ';' 切语句，顺带剥掉 '--' 行注释。

    psycopg 的 `cursor.execute()` 一次只接受一条语句（多语句要靠
    `execute_many` 或拼接），DDL 只有几十行，切一次更省事、报错位置也更清楚。
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


def pg_columns(cur, table: str) -> list[str]:
    """取可写的列（排除生成列）。生成列不能出现在 INSERT 里。"""
    cur.execute(
        """
        SELECT column_name, is_generated
          FROM information_schema.columns
         WHERE table_schema = %s AND table_name = %s
         ORDER BY ordinal_position
        """,
        (SCHEMA, table),
    )
    columns = []
    for name, is_generated in cur.fetchall():
        # information_schema: 'NEVER' 普通列；'ALWAYS' = GENERATED ... STORED
        if (is_generated or "").upper() != "NEVER":
            print(f"  [跳过生成列] {table}.{name}（is_generated={is_generated}）")
            continue
        columns.append(name)
    return columns


def verify(cur) -> list[str]:
    """核对两边行数与金额合计。返回问题列表，空表示一致。"""
    problems: list[str] = []
    con = sqlite3.connect(f"file:{SQLITE_DB}?mode=ro", uri=True)
    try:
        for table in TABLES:
            want = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            cur.execute(f'SELECT COUNT(*) FROM "{SCHEMA}"."{table}"')
            got = cur.fetchone()[0]
            mark = "OK " if want == got else "!!!"
            print(f"  {mark} {table:14} sqlite={want:4}  postgres={got:4}")
            if want != got:
                problems.append(f"{table} 行数不一致：{want} vs {got}")

        want = con.execute(
            "SELECT COUNT(*), SUM(quantity), SUM(amount), SUM(discount_amount) "
            "FROM fact_order"
        ).fetchone()
        cur.execute(
            f'SELECT COUNT(*), SUM(quantity), SUM(amount), SUM(discount_amount) '
            f'FROM "{SCHEMA}".fact_order'
        )
        got = cur.fetchone()
        labels = ("订单数", "数量合计", "金额合计", "优惠合计")
        for label, a, b in zip(labels, want, got):
            if a is None or b is None:
                continue
            if abs(float(a) - float(b)) > 1e-6:
                problems.append(f"{label} 不一致：{a} vs {b}")
                print(f"  !!! {label:8} sqlite={a}  postgres={b}")
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
        f'SELECT order_status, COUNT(*) FROM "{SCHEMA}".fact_order GROUP BY 1'
    )
    got_status = dict(cur.fetchall())
    if want_status != got_status:
        problems.append(f"状态分布不一致：{want_status} vs {got_status}")
    else:
        print(f"  OK  状态分布 {got_status}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把黄金集灌进 PostgreSQL")
    parser.add_argument("--host", default=os.environ.get("NL2SQL_PG_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("NL2SQL_PG_PORT", "5432")))
    parser.add_argument("--user", default=os.environ.get("NL2SQL_PG_USER", "postgres"))
    parser.add_argument("--database", default="nl2sql_golden")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要执行的动作")
    args = parser.parse_args(argv)

    # trust 认证下允许不给口令：要不要口令由服务端决定
    password = os.environ.get("NL2SQL_PG_PASSWORD") or None
    if not SQLITE_DB.exists():
        print(f"找不到 SQLite 黄金库：{SQLITE_DB}")
        return 1

    try:
        import psycopg
        from psycopg import sql as pgsql
    except ImportError:
        print("需要 psycopg：pip install psycopg[binary]")
        return 1

    ddl = split_sql(PG_DDL.read_text(encoding="utf-8"))
    print(f"DDL 语句 {len(ddl)} 条；目标库 `{args.database}` @ {args.host}:{args.port}")
    if args.dry_run:
        for stmt in ddl:
            print("  " + stmt.splitlines()[0][:90])
        print("（dry-run，未连接）")
        return 0

    connect_kwargs = {
        "host": args.host,
        "port": args.port,
        "user": args.user,
        "connect_timeout": 10,
    }
    if password:
        connect_kwargs["password"] = password

    # 1) 连维护库 `postgres` 建目标库（CREATE DATABASE 要求 autocommit）
    admin = psycopg.connect(dbname="postgres", autocommit=True, **connect_kwargs)
    try:
        with admin.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (args.database,)
            )
            exists = cur.fetchone() is not None
            if not exists:
                cur.execute(
                    pgsql.SQL("CREATE DATABASE {} ENCODING 'UTF8'").format(
                        pgsql.Identifier(args.database)
                    )
                )
                print(f"已创建数据库 {args.database}")
            else:
                print(f"数据库 {args.database} 已存在，重建表")
    finally:
        admin.close()

    # 2) 连目标库建表 + 灌数（事务失败回滚）
    conn = psycopg.connect(dbname=args.database, autocommit=False, **connect_kwargs)
    try:
        with conn.cursor() as cur:
            for stmt in ddl:
                cur.execute(stmt)
            print("建表完成")

            for table in TABLES:
                columns = pg_columns(cur, table)
                rows = sqlite_rows(table, columns)
                placeholders = ", ".join(["%s"] * len(columns))
                col_sql = ", ".join(f'"{c}"' for c in columns)
                cur.executemany(
                    f'INSERT INTO "{table}" ({col_sql}) VALUES ({placeholders})',
                    rows,
                )
                print(f"  {table:14} 写入 {len(rows):4} 行")

            print("核对：")
            problems = verify(cur)
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
