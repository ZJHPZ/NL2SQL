"""生成电商数仓样例数据（可复现：固定随机种子）。

用法: python seed_data.py
产出: ecommerce.db
"""
import os
import random
import sqlite3
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "ecommerce.db")
SCHEMA_PATH = os.path.join(HERE, "schema.sql")
SEED = 20240630

# ---------------------------------------------------------------- 维表定义
CHANNELS = [
    (1, "淘宝", "线上"),
    (2, "京东", "线上"),
    (3, "抖音", "线上"),
    (4, "官网", "线上"),
    (5, "线下门店", "线下"),
]

# (product_id, name, category, brand, unit_cost)
PRODUCTS = [
    (101, "智能手机Pro", "数码", "星辰", 2800.0),
    (102, "无线耳机", "数码", "星辰", 180.0),
    (103, "机械键盘", "数码", "黑轴", 260.0),
    (104, "4K显示器", "数码", "视界", 1200.0),
    (105, "扫地机器人", "家电", "洁宝", 1500.0),
    (106, "空气净化器", "家电", "洁宝", 900.0),
    (107, "电饭煲", "家电", "厨乐", 320.0),
    (108, "男士夹克", "服饰", "风行", 260.0),
    (109, "运动跑鞋", "服饰", "疾风", 380.0),
    (110, "女士连衣裙", "服饰", "云裳", 420.0),
    (111, "坚果礼盒", "食品", "山野", 120.0),
    (112, "进口牛奶", "食品", "牧场", 78.0),
    (113, "精品咖啡豆", "食品", "烘焙家", 160.0),
    (114, "数据结构与算法", "图书", "博文", 68.0),
    (115, "大数据技术原理", "图书", "博文", 89.0),
    (116, "机器学习实战", "图书", "华章", 99.0),
]

CUSTOMERS = [
    (1, "张伟", "黄金", "华东"),
    (2, "李娜", "白银", "华北"),
    (3, "王强", "铂金", "华南"),
    (4, "刘洋", "普通", "华东"),
    (5, "陈静", "黄金", "西南"),
    (6, "杨帆", "白银", "华中"),
    (7, "赵敏", "普通", "华北"),
    (8, "孙磊", "铂金", "华东"),
    (9, "周婷", "黄金", "华南"),
    (10, "吴昊", "普通", "西南"),
    (11, "郑爽", "白银", "华中"),
    (12, "冯超", "普通", "华东"),
]

# (order_status, weight)
STATUS_WEIGHTS = [("已支付", 82), ("已退款", 10), ("已取消", 8)]


def build_dim_date(start: date, end: date):
    rows = []
    d = start
    while d <= end:
        rows.append((
            d.isoformat(), d.year, d.month, f"{d.month}月",
            (d.month - 1) // 3 + 1,
            d.isocalendar()[1],
            1 if d.weekday() >= 5 else 0,
        ))
        d += timedelta(days=1)
    return rows


def generate():
    rng = random.Random(SEED)

    start, end = date(2024, 1, 1), date(2024, 6, 30)
    dim_date = build_dim_date(start, end)

    # 客户注册日期：多数在数据窗口之前，少量在窗口内（用于新客分析）
    dim_customer = []
    for cid, name, level, region in CUSTOMERS:
        if rng.random() < 0.35:
            reg = start + timedelta(days=rng.randint(0, 120))
            is_new = 1
        else:
            reg = date(2023, 1, 1) + timedelta(days=rng.randint(0, 360))
            is_new = 0
        dim_customer.append((cid, name, level, region, reg.isoformat(), is_new))

    total_days = (end - start).days
    rows = []
    order_id = 100001
    for day_offset in range(total_days + 1):
        d = start + timedelta(days=day_offset)
        # 周末单量更高
        base = 6 if d.weekday() >= 5 else 4
        for _ in range(rng.randint(max(0, base - 2), base + 2)):
            pid, pname, cat, brand, cost = rng.choice(PRODUCTS)
            cid, cname, level, region, reg, is_new = rng.choice(dim_customer)
            ch_id, ch_name, ch_type = rng.choice(CHANNELS)

            # 成交单价：成本加成 25% ~ 70%，取整到元
            unit_price = round(cost * rng.uniform(1.25, 1.70))
            quantity = rng.choices([1, 2, 3, 4, 5], weights=[55, 22, 12, 7, 4])[0]
            gross = unit_price * quantity
            # 优惠：35% 订单无优惠；有优惠则不超过成交金额的 20%
            if rng.random() < 0.35:
                discount = 0.0
            else:
                discount = round(gross * rng.uniform(0.02, 0.18), 2)

            status = rng.choices(
                [s for s, _ in STATUS_WEIGHTS],
                weights=[w for _, w in STATUS_WEIGHTS],
            )[0]

            rows.append((
                order_id, d.isoformat(), cid, pid, ch_id,
                quantity, float(unit_price), discount, status,
            ))
            order_id += 1

    return dim_date, dim_customer, rows


def main():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema_sql = f.read()

    dim_date, dim_customer, fact_rows = generate()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript(schema_sql)
    cur.executemany("INSERT INTO dim_date VALUES (?,?,?,?,?,?,?)", dim_date)
    cur.executemany("INSERT INTO dim_customer VALUES (?,?,?,?,?,?)", dim_customer)
    cur.executemany("INSERT INTO dim_product VALUES (?,?,?,?,?)", PRODUCTS)
    cur.executemany("INSERT INTO dim_channel VALUES (?,?,?)", CHANNELS)
    cur.executemany(
        "INSERT INTO fact_order (order_id, order_date, customer_id, product_id,"
        " channel_id, quantity, unit_price, discount_amount, order_status)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        fact_rows,
    )
    conn.commit()

    print(f"DB       : {DB_PATH}")
    print(f"dim_date : {len(dim_date)}")
    print(f"dim_cust : {len(dim_customer)}")
    print(f"dim_prod : {len(PRODUCTS)}")
    print(f"fact_order: {len(fact_rows)}")
    for tbl in ["dim_date", "dim_customer", "dim_product", "dim_channel", "fact_order"]:
        n = cur.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        print(f"  check {tbl:14s} = {n}")

    print("\n-- 抽样 5 行 fact_order --")
    for r in cur.execute("SELECT * FROM fact_order ORDER BY order_id LIMIT 5"):
        print("  ", r)
    print("\n-- 状态分布 --")
    for r in cur.execute(
        "SELECT order_status, COUNT(*) FROM fact_order GROUP BY order_status ORDER BY 2 DESC"
    ):
        print("  ", r)
    conn.close()


if __name__ == "__main__":
    main()
