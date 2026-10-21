"""生成留出集基线：`golden_set/heldout.json`。

**为什么留出集要"写出来但不跑"**

黄金集 30 题是被看过的：M3 改语义层时，C3/F1 的失败直接指导了实现。
再用同一批题判断"泛化到没见过的问法"，等于用训练集当测试集。
留出集在 M3 实现**完成之后**才写，且只用来固化"题面 + 手写 SQL + 答案"，
**开发期不拿规划器跑它**（M4 实验才用，且只跑一次）。

答案不是系统生成的，而是**手写 SQL 执行**得到的：与黄金集 baseline.json
同一条产线（先写 SQL、再执行、把结果冻进文件）。人工 SQL 与题面若有口径
偏差，责任在写题的人——这正是 D3 暴露过的问题（题面没问客户数、黄金
SQL 多一列），所以每题都要对照题面读一遍 SQL。

运行:
    python scripts/make_heldout.py            # 生成/覆盖 heldout.json
    python scripts/make_heldout.py --check    # 只校验文件可读、题数对
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "golden_set", "ecommerce.db")
OUT_PATH = os.path.join(ROOT, "golden_set", "heldout.json")

H1 = "f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'"
PAID = "f.order_status = '已支付'"

# (id, category, question, sql)
CASES: list[tuple[str, str, str, str]] = [
    # ---------------------------------------------------- 基础过滤与聚合
    ("H1", "基础过滤与聚合",
     "2024年5月，家电品类的实收金额和有效订单数分别是多少？",
     f"""SELECT ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额,
       COUNT(*) AS 有效订单数
FROM fact_order f
JOIN dim_product p ON f.product_id = p.product_id
WHERE p.category = '家电' AND {PAID}
  AND f.order_date >= '2024-05-01' AND f.order_date <= '2024-05-31'"""),
    ("H2", "基础过滤与聚合",
     "2024年上半年，华北地区的已取消订单有多少笔？",
     f"""SELECT COUNT(*) AS 已取消订单数
FROM fact_order f
JOIN dim_customer c ON f.customer_id = c.customer_id
WHERE c.region = '华北' AND f.order_status = '已取消' AND {H1}"""),
    ("H3", "基础过滤与聚合",
     "2024年6月，实收金额超过1000元的有效订单有多少笔？",
     """SELECT COUNT(*) AS 订单数
FROM fact_order f
WHERE f.order_status = '已支付'
  AND (f.amount - f.discount_amount) > 1000
  AND f.order_date >= '2024-06-01' AND f.order_date <= '2024-06-30'"""),
    ("H4", "基础过滤与聚合",
     "2024年上半年，食品品类的销量合计是多少？",
     f"""SELECT SUM(f.quantity) AS 销量
FROM fact_order f
JOIN dim_product p ON f.product_id = p.product_id
WHERE p.category = '食品' AND {PAID} AND {H1}"""),

    # -------------------------------------------------------- 分组聚合
    ("H5", "分组聚合",
     "2024年上半年，各客户等级的有效订单数是多少？按订单数降序排列。",
     f"""SELECT c.customer_level AS 客户等级, COUNT(*) AS 有效订单数
FROM fact_order f
JOIN dim_customer c ON f.customer_id = c.customer_id
WHERE {PAID} AND {H1}
GROUP BY c.customer_level
ORDER BY 有效订单数 DESC, 客户等级"""),
    ("H6", "分组聚合",
     "2024年上半年，各品牌的平均单笔实收金额是多少？按平均金额降序排列。",
     f"""SELECT p.brand AS 品牌,
       ROUND(AVG(f.amount - f.discount_amount), 2) AS 平均金额
FROM fact_order f
JOIN dim_product p ON f.product_id = p.product_id
WHERE {PAID} AND {H1}
GROUP BY p.brand
ORDER BY 平均金额 DESC, 品牌"""),
    ("H7", "分组聚合",
     "2024年上半年，各渠道的退款订单数是多少？",
     f"""SELECT c.channel_name AS 渠道, COUNT(*) AS 退款订单数
FROM fact_order f
JOIN dim_channel c ON f.channel_id = c.channel_id
WHERE f.order_status = '已退款' AND {H1}
GROUP BY c.channel_name
ORDER BY 退款订单数 DESC, 渠道"""),
    ("H8", "分组聚合",
     "2024年上半年，下单次数超过5次的客户有哪些？列出客户姓名和订单数。",
     f"""SELECT c.customer_name AS 客户姓名, COUNT(*) AS 订单数
FROM fact_order f
JOIN dim_customer c ON f.customer_id = c.customer_id
WHERE {PAID} AND {H1}
GROUP BY c.customer_id, c.customer_name
HAVING COUNT(*) > 5
ORDER BY 订单数 DESC, 客户姓名"""),

    # -------------------------------------------------------- 时间趋势
    ("H9", "时间趋势",
     "2024年上半年，每月的已退款订单数是多少？按月份升序排列。",
     """SELECT substr(f.order_date, 1, 7) AS 月份, COUNT(*) AS 退款订单数
FROM fact_order f
WHERE f.order_status = '已退款'
  AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY substr(f.order_date, 1, 7)
ORDER BY 月份"""),
    ("H10", "时间趋势",
     "2024年第二季度，每月的实收金额是多少？按月份升序排列。",
     f"""SELECT substr(f.order_date, 1, 7) AS 月份,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额
FROM fact_order f
WHERE {PAID}
  AND f.order_date >= '2024-04-01' AND f.order_date <= '2024-06-30'
GROUP BY substr(f.order_date, 1, 7)
ORDER BY 月份"""),
    ("H11", "时间趋势",
     "2024年上半年，每周的实收金额是多少？按周升序排列。",
     f"""SELECT strftime('%Y-%W', f.order_date) AS 年周,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额
FROM fact_order f
WHERE {PAID} AND {H1}
GROUP BY strftime('%Y-%W', f.order_date)
ORDER BY 年周"""),

    # -------------------------------------------------------- 多表关联
    ("H12", "多表关联",
     "2024年上半年，各地区的消费客户数是多少？按客户数降序排列。",
     f"""SELECT c.region AS 地区, COUNT(DISTINCT f.customer_id) AS 消费客户数
FROM fact_order f
JOIN dim_customer c ON f.customer_id = c.customer_id
WHERE {PAID} AND {H1}
GROUP BY c.region
ORDER BY 消费客户数 DESC, 地区"""),
    ("H13", "多表关联",
     "2024年上半年，各商品品类的商品成本合计是多少？按成本降序排列。",
     f"""SELECT p.category AS 品类,
       ROUND(SUM(f.quantity * p.unit_cost), 2) AS 成本
FROM fact_order f
JOIN dim_product p ON f.product_id = p.product_id
WHERE {PAID} AND {H1}
GROUP BY p.category
ORDER BY 成本 DESC, 品类"""),
    ("H14", "多表关联",
     "2024年上半年，各客户等级的平均订单金额（实收）是多少？",
     f"""SELECT c.customer_level AS 客户等级,
       ROUND(AVG(f.amount - f.discount_amount), 2) AS 平均订单金额
FROM fact_order f
JOIN dim_customer c ON f.customer_id = c.customer_id
WHERE {PAID} AND {H1}
GROUP BY c.customer_level
ORDER BY 平均订单金额 DESC, 客户等级"""),

    # -------------------------------------------------------- 窗口与排名
    ("H15", "窗口与排名",
     "2024年上半年，各客户等级的实收金额排名，列出等级、实收金额和排名。",
     f"""SELECT c.customer_level AS 客户等级,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额,
       RANK() OVER (ORDER BY SUM(f.amount - f.discount_amount) DESC) AS 排名
FROM fact_order f
JOIN dim_customer c ON f.customer_id = c.customer_id
WHERE {PAID} AND {H1}
GROUP BY c.customer_level
ORDER BY 排名, 客户等级"""),
    ("H16", "窗口与排名",
     "2024年上半年，各商品品类内部实收金额前2的品牌是哪些？"
     "列出品类、品牌、实收金额和品类内排名。",
     f"""WITH brand AS (
    SELECT p.category AS 品类, p.brand AS 品牌,
           SUM(f.amount - f.discount_amount) AS 实收金额
    FROM fact_order f
    JOIN dim_product p ON f.product_id = p.product_id
    WHERE {PAID} AND {H1}
    GROUP BY p.category, p.brand
)
SELECT 品类, 品牌, ROUND(实收金额, 2) AS 实收金额, 品类内排名
FROM (
    SELECT 品类, 品牌, 实收金额,
           ROW_NUMBER() OVER (PARTITION BY 品类
                              ORDER BY 实收金额 DESC) AS 品类内排名
    FROM brand
)
WHERE 品类内排名 <= 2
ORDER BY 品类, 品类内排名"""),
    ("H17", "窗口与排名",
     "2024年上半年，每月实收金额占上半年总实收金额的比例是多少？"
     "按比例降序排列。",
     """WITH m AS (
    SELECT substr(f.order_date, 1, 7) AS 月份,
           SUM(f.amount - f.discount_amount) AS v
    FROM fact_order f
    WHERE f.order_status = '已支付'
      AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
    GROUP BY substr(f.order_date, 1, 7)
)
SELECT 月份,
       ROUND(v, 2) AS 实收金额,
       ROUND(v * 1.0 / SUM(v) OVER (), 4) AS 占比
FROM m
ORDER BY 占比 DESC, 月份"""),

    # ---------------------------------------------------- 子查询与CTE
    ("H18", "子查询与CTE",
     "2024年上半年，实收金额低于全部客户平均消费金额的客户有哪些？"
     "列出客户姓名和实收金额。",
     """WITH cust AS (
    SELECT c.customer_id, c.customer_name,
           SUM(f.amount - f.discount_amount) AS 实收金额
    FROM fact_order f
    JOIN dim_customer c ON f.customer_id = c.customer_id
    WHERE f.order_status = '已支付'
      AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
    GROUP BY c.customer_id, c.customer_name
)
SELECT customer_name AS 客户姓名, ROUND(实收金额, 2) AS 实收金额
FROM cust
WHERE 实收金额 < (SELECT AVG(实收金额) FROM cust)"""),
    ("H19", "子查询与CTE",
     "2024年上半年，实收金额高于全部品类平均实收金额的品类有哪些？"
     "列出品类和实收金额。",
     """WITH cat AS (
    SELECT p.category AS 品类,
           SUM(f.amount - f.discount_amount) AS 实收金额
    FROM fact_order f
    JOIN dim_product p ON f.product_id = p.product_id
    WHERE f.order_status = '已支付'
      AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
    GROUP BY p.category
)
SELECT 品类, ROUND(实收金额, 2) AS 实收金额
FROM cat
WHERE 实收金额 > (SELECT AVG(实收金额) FROM cat)"""),
    ("H20", "子查询与CTE",
     "2024年上半年，实收金额不低于全部客户平均消费金额1.2倍的客户有哪些？"
     "列出客户姓名和实收金额。",
     """WITH cust AS (
    SELECT c.customer_id, c.customer_name,
           SUM(f.amount - f.discount_amount) AS 实收金额
    FROM fact_order f
    JOIN dim_customer c ON f.customer_id = c.customer_id
    WHERE f.order_status = '已支付'
      AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
    GROUP BY c.customer_id, c.customer_name
)
SELECT customer_name AS 客户姓名, ROUND(实收金额, 2) AS 实收金额
FROM cust
WHERE 实收金额 >= (SELECT AVG(实收金额) * 1.2 FROM cust)"""),
]


def build() -> dict:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        cases = []
        for cid, category, question, sql in CASES:
            cur.execute(sql)
            columns = [d[0] for d in cur.description]
            rows = [list(map(_plain, r)) for r in cur.fetchall()]
            cases.append({
                "id": cid,
                "category": category,
                "question": question,
                "sql": sql,
                "columns": columns,
                "row_count": len(rows),
                "rows": rows,
            })
    finally:
        conn.close()
    return {
        "version": "m3-heldout-v1",
        "note": (
            "留出集：M3 实现完成后才写，开发期不跑。答案由手写 SQL 执行得到，"
            "与 baseline.json 同一产线。M4 实验使用，用于检验语义层改动是否"
            "只对黄金集过拟合。"
        ),
        "db": "golden_set/ecommerce.db",
        "semantics": "golden_set/metrics.yml",
        "cases": cases,
    }


def _plain(v):
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def check() -> int:
    if not os.path.exists(OUT_PATH):
        print("heldout.json 不存在")
        return 1
    with io.open(OUT_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    cases = data["cases"]
    problems = []
    if len(cases) != len(CASES):
        problems.append(f"题数 {len(cases)} != {len(CASES)}")
    for c in cases:
        if not c["rows"]:
            problems.append(f"{c['id']} 答案为空")
        if not c["question"].strip():
            problems.append(f"{c['id']} 题面为空")
    cats = sorted({c["category"] for c in cases})
    print(f"heldout.json: {len(cases)} 题 / {len(cats)} 类 / "
          f"{sum(c['row_count'] for c in cases)} 行答案")
    print("分类:", {k: sum(1 for c in cases if c["category"] == k) for k in cats})
    if problems:
        print("问题:")
        for p in problems:
            print("  -", p)
        return 1
    print("校验通过")
    return 0


def main() -> int:
    if "--check" in sys.argv:
        return check()
    data = build()
    with io.open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"已写入 {os.path.relpath(OUT_PATH, ROOT)}："
          f"{len(data['cases'])} 题，"
          f"{sum(c['row_count'] for c in data['cases'])} 行答案")
    return check()


if __name__ == "__main__":
    sys.exit(main())
