"""实验二：口径漂移（同一句业务问题，语义口径 vs 朴素手写口径）。

论文要证明的命题：**绕过语义层是有代价的，代价可以量化**。

做法：对同一批业务问题，分别执行
    A. 语义口径（黄金集/语义模型的口径：订单状态过滤、优惠扣减、去重计数…）
    B. 朴素口径（不看语义层、直接照字面写 SQL 时最常见的错法）
两个数字都落在真实黄金库上，算差异率。

注意这不是假设：表里第 1 行正是 T 系列调试时 agent 真实写出的 SQL
（`SUM(f.amount)`，忽略退款/未支付），与语义层答案差 20.4%。
其它行是"同一类绕过"的系统化版本，全部可复现。

本实验不需要模型、不需要网络，是确定性的——这正是它作为论文证据的价值。

用法:
    python scripts/drift_table.py
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_DB = os.path.join(ROOT, "golden_set", "ecommerce.db")
OUT_DIR = os.path.join(ROOT, "data", "_experiments", "drift")

H1 = "order_date >= '2024-01-01' AND order_date <= '2024-06-30'"
PAID = f"order_status = '已支付' AND {H1}"
NET = "amount - discount_amount"

# (id, 指标, 语义口径 SQL, 朴素口径 SQL, 常见错法)
CASES = [
    # 真实样本：docs/05 §③，agent 回答"消费最高的前5个客户"时自己写了 SUM(f.amount)，
    # 冯超 的金额从 161826.97（语义口径）变成 194758（毛金额、含退款/取消）。
    ("DR1", "冯超实收金额（真实样本）",
     "SELECT ROUND(SUM(f.amount - f.discount_amount), 2) FROM fact_order f "
     "JOIN dim_customer c ON c.customer_id = f.customer_id "
     f"WHERE c.customer_name = '冯超' AND f.order_status = '已支付' AND {H1}",
     "SELECT ROUND(SUM(f.amount), 2) FROM fact_order f "
     "JOIN dim_customer c ON c.customer_id = f.customer_id "
     f"WHERE c.customer_name = '冯超' AND {H1}",
     "agent 真实手写 SUM(f.amount)：漏掉'只算已支付'和'减优惠'（docs/05 §③）"),
    ("DR2", "有效订单数",
     f"SELECT COUNT(*) FROM fact_order WHERE {PAID}",
     f"SELECT COUNT(*) FROM fact_order WHERE {H1}",
     "把全部订单都算成有效订单"),
    ("DR3", "客户数",
     "SELECT COUNT(DISTINCT customer_id) FROM fact_order "
     f"WHERE {PAID}",
     f"SELECT COUNT(customer_id) FROM fact_order WHERE {H1}",
     "把订单行数当客户数（没去重、也没过滤状态）"),
    ("DR4", "客单价",
     "SELECT ROUND(SUM(amount - discount_amount) * 1.0 / "
     "COUNT(DISTINCT customer_id), 2) FROM fact_order "
     f"WHERE {PAID}",
     f"SELECT ROUND(AVG(amount - discount_amount), 2) FROM fact_order WHERE {H1}",
     "把订单行平均值当客单价（客单价是'每客户'不是'每单'）"),
    ("DR5", "毛利率",
     "SELECT ROUND((SUM(amount - discount_amount) - "
     "SUM(quantity * (SELECT unit_cost FROM dim_product p "
     "WHERE p.product_id = fact_order.product_id))) * 1.0 / "
     f"SUM(amount - discount_amount), 4) FROM fact_order WHERE {PAID}",
     "SELECT ROUND((SUM(amount) - SUM(quantity * (SELECT unit_cost FROM "
     "dim_product p WHERE p.product_id = fact_order.product_id))) * 1.0 / "
     f"SUM(amount), 4) FROM fact_order WHERE {H1}",
     "分子分母都用毛金额且不过滤退款"),
    ("DR6", "退款率",
     "SELECT ROUND(SUM(CASE WHEN order_status = '已退款' THEN 1 ELSE 0 END) "
     "* 1.0 / COUNT(*), 4) FROM fact_order "
     f"WHERE {H1}",
     "SELECT ROUND(SUM(CASE WHEN order_status = '已退款' THEN 1 ELSE 0 END) "
     "* 1.0 / SUM(CASE WHEN order_status = '已支付' THEN 1 ELSE 0 END), 4) "
     f"FROM fact_order WHERE {H1}",
     "分母用已支付订单（退款率的分母是全部订单）"),
    ("DR7", "销量",
     f"SELECT SUM(quantity) FROM fact_order WHERE {PAID}",
     f"SELECT SUM(quantity) FROM fact_order WHERE {H1}",
     "把退款/取消的件数也算进销量"),
    ("DR8", "最高成交金额",
     f"SELECT MAX(amount) FROM fact_order WHERE {PAID}",
     f"SELECT MAX(amount) FROM fact_order WHERE {H1}",
     "没过滤状态（被退款订单抬高了最大值）"),
    ("DR9", "成本",
     "SELECT ROUND(SUM(quantity * (SELECT unit_cost FROM dim_product p "
     "WHERE p.product_id = fact_order.product_id)), 2) FROM fact_order "
     f"WHERE {PAID}",
     "SELECT ROUND(SUM(quantity * (SELECT unit_cost FROM dim_product p "
     "WHERE p.product_id = fact_order.product_id)), 2) FROM fact_order "
     f"WHERE {H1}",
     "把未成交订单的成本也算进去"),
    ("DR10", "淘宝渠道实收占比",
     "SELECT ROUND((SELECT SUM(amount - discount_amount) FROM fact_order f "
     "JOIN dim_channel c ON c.channel_id = f.channel_id "
     f"WHERE f.order_status = '已支付' AND c.channel_name = '淘宝' AND {H1}) * 1.0 "
     f"/ (SELECT SUM(amount - discount_amount) FROM fact_order WHERE {PAID}), 4)",
     "SELECT ROUND((SELECT SUM(amount) FROM fact_order f JOIN dim_channel c "
     f"ON c.channel_id = f.channel_id WHERE c.channel_name = '淘宝' AND {H1}) * 1.0 "
     f"/ (SELECT SUM(amount) FROM fact_order WHERE {H1}), 4)",
     "分子分母都用毛金额且不过滤状态（占比口径整体漂移）"),
    ("DR11", "2024-06 实收金额",
     f"SELECT ROUND(SUM({NET}), 2) FROM fact_order WHERE {PAID} "
     "AND order_date >= '2024-06-01' AND order_date <= '2024-06-30'",
     f"SELECT ROUND(SUM(amount), 2) FROM fact_order WHERE {H1} "
     "AND order_date >= '2024-06-01' AND order_date <= '2024-06-30'",
     "月度趋势：每月都用毛金额（趋势可能仍像，但水位整体虚高）"),
    ("DR12", "新客户实收金额",
     "SELECT ROUND(SUM(f.amount - f.discount_amount), 2) FROM fact_order f "
     "JOIN dim_customer c ON c.customer_id = f.customer_id "
     f"WHERE f.order_status = '已支付' AND c.is_new_customer = 1 AND {H1}",
     "SELECT ROUND(SUM(f.amount), 2) FROM fact_order f "
     "JOIN dim_customer c ON c.customer_id = f.customer_id "
     f"WHERE c.is_new_customer = 1 AND {H1}",
     "新客口径：不扣优惠不滤状态"),
    ("DR13", "2024H1 全渠道实收金额",
     f"SELECT ROUND(SUM({NET}), 2) FROM fact_order WHERE {PAID}",
     f"SELECT ROUND(SUM(amount), 2) FROM fact_order WHERE {H1}",
     "全渠道汇总同样虚高：把退款/取消订单的毛金额算进去了"),
]


def _exec(conn: sqlite3.Connection, sql: str):
    cur = conn.execute(sql)
    row = cur.fetchone()
    return None if row is None else row[0]


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    conn = sqlite3.connect(f"file:{GOLDEN_DB}?mode=ro", uri=True)
    rows = []
    for cid, metric, sem_sql, naive_sql, why in CASES:
        sem = _exec(conn, sem_sql)
        naive = _exec(conn, naive_sql)
        if sem is None or naive is None:
            diff = None
        elif abs(float(sem)) < 1e-12:
            diff = None
        else:
            diff = (float(naive) - float(sem)) / abs(float(sem))
        rows.append({"id": cid, "metric": metric, "semantic": sem,
                     "naive": naive, "diff": diff,
                     "semantic_sql": sem_sql, "naive_sql": naive_sql,
                     "why": why})
        d = f"{diff:+.2%}" if diff is not None else "n/a"
        print(f"{cid:<5}{metric:<10} 语义 {sem!s:>12}  朴素 {naive!s:>12}  差异 {d:>10}")
    conn.close()

    diffs = sorted(abs(r["diff"]) for r in rows if r["diff"] is not None)
    median = diffs[len(diffs) // 2] if len(diffs) % 2 else (
        diffs[len(diffs) // 2 - 1] + diffs[len(diffs) // 2]) / 2 if diffs else None
    result = {
        "generated_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
        "db": os.path.relpath(GOLDEN_DB, ROOT),
        "rows": rows,
        "summary": {
            "n": len(rows),
            "worst": max(rows, key=lambda r: abs(r["diff"]))["id"] if diffs else None,
            "median_abs_diff": median,
            "min_abs_diff": diffs[0] if diffs else None,
            "max_abs_diff": diffs[-1] if diffs else None,
            "n_over_5pct": sum(1 for x in diffs if x > 0.05),
        },
    }
    path = os.path.join(OUT_DIR, "drift.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    s = result["summary"]
    print(f"\n共 {s['n']} 例；绝对差异中位数 {s['median_abs_diff']:.1%}"
          f"（{s['n_over_5pct']}/{s['n']} 例 >5%，最大 {s['max_abs_diff']:.1%}）；"
          f"JSON: {path}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace")
    raise SystemExit(main())
