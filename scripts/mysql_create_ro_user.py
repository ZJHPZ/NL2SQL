"""建一个只给 SELECT 的项目账号，并把凭据写进 .env（gitignored）。

为什么值得为一次"数据灌库"专门建账号：

    本项目的守门（`nl2sql/guard/`）是解析 SQL 文本、拒绝写语句。
    那层防线**依赖解析器不出错**——正则/词法判断被绕过就等于没有。
    只读账号是库自己不让写，解析器写错也绕不过去。
    两层独立防线，比"我们的正则很严格"有力得多，也是论文安全章最硬的一条证据。

账号只授 `SELECL` 在目标库上 —— 默认 schema 都不给，
所以即使误连到别的库也读不到。

密码随机生成，只写进 `.env`（已在 .gitignore 里），不打印到终端，
避免进 shell 历史与聊天记录。

用法：
    # 需要一个能建用户的连接（root）
    $env:NL2SQL_MYSQL_ADMIN_PASSWORD = <管理员口令>
    python scripts/mysql_create_ro_user.py --database nl2sql_golden
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
RO_USER = "nl2sql_ro"


def write_env(database: str, user: str, password: str, host: str, port: int) -> None:
    """把连接信息写进 .env。已有同名键则替换，其余内容保留。"""
    wanted = {
        "NL2SQL_MYSQL_HOST": host,
        "NL2SQL_MYSQL_PORT": str(port),
        "NL2SQL_MYSQL_USER": user,
        "NL2SQL_MYSQL_PASSWORD": password,
        "NL2SQL_MYSQL_DATABASE": database,
    }
    existing: list[str] = []
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            key = line.split("=", 1)[0].strip()
            if key in wanted or not line.strip():
                continue
            existing.append(line)

    lines = existing + ([""] if existing else [])
    lines.append("# MySQL 只读账号（由 scripts/mysql_create_ro_user.py 生成）")
    lines += [f"{k}={v}" for k, v in wanted.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="建 MySQL 只读账号")
    parser.add_argument("--host", default=os.environ.get("NL2SQL_MYSQL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("NL2SQL_MYSQL_PORT", "3306")))
    parser.add_argument("--admin-user", default="root")
    parser.add_argument("--database", default="nl2sql_golden")
    parser.add_argument("--user", default=RO_USER)
    args = parser.parse_args(argv)

    admin_password = os.environ.get("NL2SQL_MYSQL_ADMIN_PASSWORD")
    if not admin_password:
        print("缺少 NL2SQL_MYSQL_ADMIN_PASSWORD（建用户需要管理权限）")
        return 1

    try:
        import pymysql
    except ImportError:
        print("需要 pymysql：pip install pymysql")
        return 1

    password = secrets.token_urlsafe(18)
    conn = pymysql.connect(
        host=args.host, port=args.port, user=args.admin_user,
        password=admin_password, charset="utf8mb4", autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE USER IF NOT EXISTS %s@'localhost' IDENTIFIED BY %s",
                        (args.user, password))
            cur.execute(f"ALTER USER %s@'localhost' IDENTIFIED BY %s", (args.user, password))
            # 先收干净再授：反复跑这个脚本不该累积权限
            cur.execute(f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM %s@'localhost'",
                        (args.user,))
            cur.execute(f"GRANT SELECT ON `{args.database}`.* TO %s@'localhost'", (args.user,))
            cur.execute("FLUSH PRIVILEGES")
            cur.execute("SHOW GRANTS FOR %s@'localhost'", (args.user,))
            grants = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

    print(f"账号 {args.user}@localhost 已就绪，授权：")
    for line in grants:
        print("  " + line)

    # 用新账号自证两件事：能读，不能写
    ro = pymysql.connect(host=args.host, port=args.port, user=args.user,
                         password=password, database=args.database,
                         charset="utf8mb4", autocommit=True)
    try:
        with ro.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM fact_order")
            print(f"\n自证 1 —— 能读：fact_order {cur.fetchone()[0]} 行")

            try:
                cur.execute("CREATE TABLE _should_fail (id INT)")
                print("自证 2 —— !!! 竟然建表成功了，只读账号没生效")
                return 1
            except pymysql.err.OperationalError as exc:
                code = exc.args[0]
                print(f"自证 2 —— 不能写：CREATE TABLE 被拒（错误码 {code}）")
                if code != 1142:  # 1142 = CREATE command denied
                    print(f"  预期 1142，实际 {code}：{exc.args[1]}")
                    return 1

            try:
                cur.execute("DELETE FROM fact_order")
                print("自证 3 —— !!! 竟然删成功了")
                return 1
            except pymysql.err.OperationalError as exc:
                print(f"自证 3 —— 不能删：DELETE 被拒（错误码 {exc.args[0]}）")
    finally:
        ro.close()

    write_env(args.database, args.user, password, args.host, args.port)
    print(f"\n凭据已写入 {ENV_FILE.name}（已 gitignore，不入库）。")
    print("密码未打印：需要时从 .env 读。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
