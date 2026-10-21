"""30 道黄金问题集：业务问题 → 标准 SQL。

设计约定
--------
* 数据窗口 2024-01-01 ~ 2024-06-30，"本月" = 2024-06，"上月" = 2024-05
* 展示口径（与 schema.sql 一致）
    - 有效订单   : order_status = '已支付'
    - GMV        : SUM(amount)                 含优惠、未剔除退款
    - 实收金额   : SUM(amount - discount_amount)
    - 客单价     : 实收金额 / 去重客户数
    - 退款率     : 已退款订单数 / 全部订单数
* 每条 SQL 都是**确定性**的（含完整 ORDER BY 与并列打断列），保证结果集可判分
* SQL 语法按 SQLite 编写，同时保持与 MySQL 8.0 接近（差异见 schema.sql 适配说明）
"""

GOLDEN_SET = [
    # ============ A. 基础过滤与聚合 (A1-A5) ============
    {
        "id": "A1",
        "category": "基础过滤与聚合",
        "question": "2024年6月，数码品类的实收金额是多少？",
        "intent": "单值聚合 + 维度过滤 + 状态过滤",
        "tags": ["AGG", "JOIN", "WHERE", "口径:实收金额"],
        "sql": """
SELECT ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额
FROM fact_order f
JOIN dim_product p ON f.product_id = p.product_id
WHERE p.category = '数码'
  AND f.order_status = '已支付'
  AND f.order_date >= '2024-06-01' AND f.order_date <= '2024-06-30'
""".strip(),
    },
    {
        "id": "A2",
        "category": "基础过滤与聚合",
        "question": "2024年6月，华东地区的有效订单数和实收金额分别是多少？",
        "intent": "多指标聚合 + 地区过滤（地区在客户维）",
        "tags": ["AGG", "JOIN", "COUNT", "口径:实收金额"],
        "sql": """
SELECT COUNT(*) AS 有效订单数,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额
FROM fact_order f
JOIN dim_customer c ON f.customer_id = c.customer_id
WHERE c.region = '华东'
  AND f.order_status = '已支付'
  AND f.order_date >= '2024-06-01' AND f.order_date <= '2024-06-30'
""".strip(),
    },
    {
        "id": "A3",
        "category": "基础过滤与聚合",
        "question": "2024年6月，单个订单成交金额超过5000元的订单有几笔？最高一笔多少？",
        "intent": "阈值过滤 + 最值",
        "tags": ["AGG", "MAX", "WHERE"],
        "sql": """
SELECT COUNT(*) AS 订单数,
       MAX(amount) AS 最高成交金额
FROM fact_order
WHERE order_status = '已支付'
  AND amount > 5000
  AND order_date >= '2024-06-01' AND order_date <= '2024-06-30'
""".strip(),
    },
    {
        "id": "A4",
        "category": "基础过滤与聚合",
        "question": "2024年上半年，产生过有效订单的客户有多少人？总有效订单数是多少？",
        "intent": "去重计数 + 全窗口聚合",
        "tags": ["AGG", "COUNT_DISTINCT"],
        "sql": """
SELECT COUNT(DISTINCT customer_id) AS 客户数,
       COUNT(*) AS 有效订单数
FROM fact_order
WHERE order_status = '已支付'
  AND order_date >= '2024-01-01' AND order_date <= '2024-06-30'
""".strip(),
    },
    {
        "id": "A5",
        "category": "基础过滤与聚合",
        "question": "2024年上半年，'已退款'和'已取消'的订单各有多少笔？",
        "intent": "分组计数 + IN 过滤",
        "tags": ["GROUP_BY", "WHERE"],
        "sql": """
SELECT order_status AS 订单状态,
       COUNT(*) AS 订单数
FROM fact_order
WHERE order_status IN ('已退款', '已取消')
  AND order_date >= '2024-01-01' AND order_date <= '2024-06-30'
GROUP BY order_status
ORDER BY 订单数 DESC, 订单状态
""".strip(),
    },

    # ============ B. 分组聚合 (B1-B6) ============
    {
        "id": "B1",
        "category": "分组聚合",
        "question": "2024年上半年，各渠道的实收金额和有效订单数是多少？按实收金额降序排列。",
        "intent": "单维分组 + 多指标 + 排序",
        "tags": ["GROUP_BY", "JOIN", "ORDER_BY", "口径:实收金额"],
        "sql": """
SELECT c.channel_name AS 渠道,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额,
       COUNT(*) AS 有效订单数
FROM fact_order f
JOIN dim_channel c ON f.channel_id = c.channel_id
WHERE f.order_status = '已支付'
  AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY c.channel_name
ORDER BY 实收金额 DESC, 渠道
""".strip(),
    },
    {
        "id": "B2",
        "category": "分组聚合",
        "question": "2024年上半年，各商品品类的实收金额和销量（件数）是多少？按实收金额降序。",
        "intent": "单维分组 + 求和指标",
        "tags": ["GROUP_BY", "JOIN", "SUM"],
        "sql": """
SELECT p.category AS 品类,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额,
       SUM(f.quantity) AS 销量
FROM fact_order f
JOIN dim_product p ON f.product_id = p.product_id
WHERE f.order_status = '已支付'
  AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY p.category
ORDER BY 实收金额 DESC, 品类
""".strip(),
    },
    {
        "id": "B3",
        "category": "分组聚合",
        "question": "2024年上半年，哪个品牌实收金额最高？列出各品牌的实收金额。",
        "intent": "单维分组 + 取首行",
        "tags": ["GROUP_BY", "JOIN", "ORDER_BY", "LIMIT"],
        "sql": """
SELECT p.brand AS 品牌,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额
FROM fact_order f
JOIN dim_product p ON f.product_id = p.product_id
WHERE f.order_status = '已支付'
  AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY p.brand
ORDER BY 实收金额 DESC, 品牌
""".strip(),
    },
    {
        "id": "B4",
        "category": "分组聚合",
        "question": "2024年上半年，各渠道的客单价是多少？按客单价降序排列。",
        "intent": "派生指标：总额 / 去重客户数",
        "tags": ["GROUP_BY", "COUNT_DISTINCT", "口径:客单价"],
        "sql": """
SELECT c.channel_name AS 渠道,
       ROUND(SUM(f.amount - f.discount_amount) * 1.0
             / COUNT(DISTINCT f.customer_id), 2) AS 客单价
FROM fact_order f
JOIN dim_channel c ON f.channel_id = c.channel_id
WHERE f.order_status = '已支付'
  AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY c.channel_name
ORDER BY 客单价 DESC, 渠道
""".strip(),
    },
    {
        "id": "B5",
        "category": "分组聚合",
        "question": "2024年上半年，实收金额超过20000元的商品品类有哪些？",
        "intent": "HAVING 过滤分组结果",
        "tags": ["GROUP_BY", "HAVING"],
        "sql": """
SELECT p.category AS 品类,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额
FROM fact_order f
JOIN dim_product p ON f.product_id = p.product_id
WHERE f.order_status = '已支付'
  AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY p.category
HAVING SUM(f.amount - f.discount_amount) > 20000
ORDER BY 实收金额 DESC, 品类
""".strip(),
    },
    {
        "id": "B6",
        "category": "分组聚合",
        "question": "2024年上半年，各地区的已退款订单数占该地区全部订单数的比例是多少？列出地区和退款率。",
        "intent": "比率指标：条件计数 / 总数",
        "tags": ["GROUP_BY", "CASE_WHEN", "口径:退款率"],
        "sql": """
SELECT c.region AS 地区,
       ROUND(SUM(CASE WHEN f.order_status = '已退款' THEN 1 ELSE 0 END) * 1.0
             / COUNT(*), 4) AS 退款率
FROM fact_order f
JOIN dim_customer c ON f.customer_id = c.customer_id
WHERE f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY c.region
ORDER BY 退款率 DESC, 地区
""".strip(),
    },

    # ============ C. 时间趋势 (C1-C6) ============
    {
        "id": "C1",
        "category": "时间趋势",
        "question": "2024年上半年，每月的有效订单数和实收金额是多少？按月份升序排列。",
        "intent": "按月分桶 + 多指标",
        "tags": ["TIME_BUCKET", "strftime", "GROUP_BY"],
        "sql": """
SELECT strftime('%Y-%m', order_date) AS 月份,
       COUNT(*) AS 有效订单数,
       ROUND(SUM(amount - discount_amount), 2) AS 实收金额
FROM fact_order
WHERE order_status = '已支付'
  AND order_date >= '2024-01-01' AND order_date <= '2024-06-30'
GROUP BY strftime('%Y-%m', order_date)
ORDER BY 月份
""".strip(),
    },
    {
        "id": "C2",
        "category": "时间趋势",
        "question": "2024年上半年，每月的有效订单数是多少？按月份升序。",
        "intent": "按月计数",
        "tags": ["TIME_BUCKET", "COUNT"],
        "sql": """
SELECT strftime('%Y-%m', order_date) AS 月份,
       COUNT(*) AS 有效订单数
FROM fact_order
WHERE order_status = '已支付'
  AND order_date >= '2024-01-01' AND order_date <= '2024-06-30'
GROUP BY strftime('%Y-%m', order_date)
ORDER BY 月份
""".strip(),
    },
    {
        "id": "C3",
        "category": "时间趋势",
        "question": "2024年上半年，每周的有效订单数是多少？按周升序排列。",
        "intent": "按 ISO 周分桶",
        "tags": ["TIME_BUCKET", "strftime"],
        "sql": """
SELECT strftime('%Y-%W', order_date) AS 年周,
       COUNT(*) AS 有效订单数
FROM fact_order
WHERE order_status = '已支付'
  AND order_date >= '2024-01-01' AND order_date <= '2024-06-30'
GROUP BY strftime('%Y-%W', order_date)
ORDER BY 年周
""".strip(),
    },
    {
        "id": "C4",
        "category": "时间趋势",
        "question": "本月（2024年6月）和上月（2024年5月）的实收金额分别是多少？",
        "intent": "跨行对比：CASE 条件聚合转列",
        "tags": ["CASE_WHEN", "口径:实收金额", "对比"],
        "sql": """
SELECT ROUND(SUM(CASE WHEN order_date >= '2024-06-01' AND order_date <= '2024-06-30'
                      THEN amount - discount_amount ELSE 0 END), 2) AS 本月实收金额,
       ROUND(SUM(CASE WHEN order_date >= '2024-05-01' AND order_date <= '2024-05-31'
                      THEN amount - discount_amount ELSE 0 END), 2) AS 上月实收金额
FROM fact_order
WHERE order_status = '已支付'
  AND order_date >= '2024-05-01' AND order_date <= '2024-06-30'
""".strip(),
    },
    {
        "id": "C5",
        "category": "时间趋势",
        "question": "2024年上半年，线上渠道和线下渠道各自的实收金额是多少？按月列出。",
        "intent": "时间 × 维度交叉：CASE 转列",
        "tags": ["TIME_BUCKET", "CASE_WHEN", "JOIN"],
        "sql": """
SELECT strftime('%Y-%m', f.order_date) AS 月份,
       ROUND(SUM(CASE WHEN c.channel_type = '线上'
                      THEN f.amount - f.discount_amount ELSE 0 END), 2) AS 线上实收金额,
       ROUND(SUM(CASE WHEN c.channel_type = '线下'
                      THEN f.amount - f.discount_amount ELSE 0 END), 2) AS 线下实收金额
FROM fact_order f
JOIN dim_channel c ON f.channel_id = c.channel_id
WHERE f.order_status = '已支付'
  AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY strftime('%Y-%m', f.order_date)
ORDER BY 月份
""".strip(),
    },
    {
        "id": "C6",
        "category": "时间趋势",
        "question": "2024年6月，每天的实收金额是多少？只列出实收金额大于3000元的日期，按日期升序。",
        "intent": "按天分桶 + HAVING",
        "tags": ["TIME_BUCKET", "HAVING", "日期粒度"],
        "sql": """
SELECT order_date AS 日期,
       ROUND(SUM(amount - discount_amount), 2) AS 实收金额
FROM fact_order
WHERE order_status = '已支付'
  AND order_date >= '2024-06-01' AND order_date <= '2024-06-30'
GROUP BY order_date
HAVING SUM(amount - discount_amount) > 3000
ORDER BY 日期
""".strip(),
    },

    # ============ D. 多表关联 (D1-D7) ============
    {
        "id": "D1",
        "category": "多表关联",
        "question": "2024年上半年，消费金额最高的10位客户是谁？列出客户姓名、所属地区和实收金额。",
        "intent": "多表 JOIN + TopN",
        "tags": ["JOIN", "ORDER_BY", "LIMIT", "TopN"],
        "sql": """
SELECT c.customer_name AS 客户姓名,
       c.region AS 地区,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额
FROM fact_order f
JOIN dim_customer c ON f.customer_id = c.customer_id
WHERE f.order_status = '已支付'
  AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY c.customer_id, c.customer_name, c.region
ORDER BY 实收金额 DESC, 客户姓名
LIMIT 10
""".strip(),
    },
    {
        "id": "D2",
        "category": "多表关联",
        "question": "2024年上半年，各客户等级的客户数和实收金额分别是多少？",
        "intent": "JOIN + 维度分组",
        "tags": ["JOIN", "GROUP_BY", "COUNT_DISTINCT"],
        "sql": """
SELECT c.customer_level AS 客户等级,
       COUNT(DISTINCT c.customer_id) AS 客户数,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额
FROM fact_order f
JOIN dim_customer c ON f.customer_id = c.customer_id
WHERE f.order_status = '已支付'
  AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY c.customer_level
ORDER BY 实收金额 DESC, 客户等级
""".strip(),
    },
    {
        "id": "D3",
        "category": "多表关联",
        "question": "2024年上半年，新客户和老客户各贡献了多少实收金额？",
        "intent": "布尔维度分组",
        "tags": ["JOIN", "CASE_WHEN", "口径:新老客"],
        "sql": """
SELECT CASE WHEN c.is_new_customer = 1 THEN '新客户' ELSE '老客户' END AS 客户类型,
       COUNT(DISTINCT c.customer_id) AS 客户数,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额
FROM fact_order f
JOIN dim_customer c ON f.customer_id = c.customer_id
WHERE f.order_status = '已支付'
  AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY CASE WHEN c.is_new_customer = 1 THEN '新客户' ELSE '老客户' END
ORDER BY 实收金额 DESC, 客户类型
""".strip(),
    },
    {
        "id": "D4",
        "category": "多表关联",
        "question": "2024年上半年，销量前5的商品是哪些？列出商品名称、品类和销量。",
        "intent": "JOIN + 按销量 TopN",
        "tags": ["JOIN", "ORDER_BY", "LIMIT", "TopN"],
        "sql": """
SELECT p.product_name AS 商品名称,
       p.category AS 品类,
       SUM(f.quantity) AS 销量
FROM fact_order f
JOIN dim_product p ON f.product_id = p.product_id
WHERE f.order_status = '已支付'
  AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY p.product_id, p.product_name, p.category
ORDER BY 销量 DESC, 商品名称
LIMIT 5
""".strip(),
    },
    {
        "id": "D5",
        "category": "多表关联",
        "question": "2024年上半年，各地区的消费客户数和实收金额是多少？",
        "intent": "地区维度 + 去重客户数",
        "tags": ["JOIN", "GROUP_BY", "COUNT_DISTINCT"],
        "sql": """
SELECT c.region AS 地区,
       COUNT(DISTINCT f.customer_id) AS 消费客户数,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额
FROM fact_order f
JOIN dim_customer c ON f.customer_id = c.customer_id
WHERE f.order_status = '已支付'
  AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY c.region
ORDER BY 实收金额 DESC, 地区
""".strip(),
    },
    {
        "id": "D6",
        "category": "多表关联",
        "question": "2024年6月，各渠道的销量和实收金额是多少？按渠道列出。",
        "intent": "渠道维度 + 当月过滤",
        "tags": ["JOIN", "GROUP_BY", "SUM"],
        "sql": """
SELECT c.channel_name AS 渠道,
       SUM(f.quantity) AS 销量,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额
FROM fact_order f
JOIN dim_channel c ON f.channel_id = c.channel_id
WHERE f.order_status = '已支付'
  AND f.order_date >= '2024-06-01' AND f.order_date <= '2024-06-30'
GROUP BY c.channel_name
ORDER BY 实收金额 DESC, 渠道
""".strip(),
    },
    {
        "id": "D7",
        "category": "多表关联",
        "question": "2024年上半年，各商品品类的毛利率是多少？（毛利率 = (实收金额 - 商品成本) / 实收金额）",
        "intent": "三表 JOIN + 派生比率",
        "tags": ["JOIN", "GROUP_BY", "口径:毛利率", "复杂计算"],
        "sql": """
SELECT p.category AS 品类,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额,
       ROUND(SUM(f.quantity * p.unit_cost), 2) AS 成本,
       ROUND((SUM(f.amount - f.discount_amount) - SUM(f.quantity * p.unit_cost)) * 1.0
             / SUM(f.amount - f.discount_amount), 4) AS 毛利率
FROM fact_order f
JOIN dim_product p ON f.product_id = p.product_id
WHERE f.order_status = '已支付'
  AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY p.category
ORDER BY 毛利率 DESC, 品类
""".strip(),
    },

    # ============ E. 窗口与排名 (E1-E4) ============
    {
        "id": "E1",
        "category": "窗口与排名",
        "question": "2024年上半年，各渠道按实收金额从高到低排名，列出渠道、实收金额和排名。",
        "intent": "窗口函数整体排序",
        "tags": ["WINDOW", "RANK", "ORDER_BY"],
        "sql": """
SELECT c.channel_name AS 渠道,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额,
       RANK() OVER (ORDER BY SUM(f.amount - f.discount_amount) DESC) AS 排名
FROM fact_order f
JOIN dim_channel c ON f.channel_id = c.channel_id
WHERE f.order_status = '已支付'
  AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY c.channel_name
ORDER BY 排名, 渠道
""".strip(),
    },
    {
        "id": "E2",
        "category": "窗口与排名",
        "question": "2024年上半年，各商品品类内部销量前2的商品是哪些？列出品类、商品名称和销量。",
        "intent": "分组内排名（PARTITION BY）+ 过滤",
        "tags": ["WINDOW", "PARTITION_BY", "ROW_NUMBER", "CTE"],
        "sql": """
WITH ranked AS (
    SELECT p.category AS 品类,
           p.product_name AS 商品名称,
           SUM(f.quantity) AS 销量,
           ROW_NUMBER() OVER (PARTITION BY p.category
                              ORDER BY SUM(f.quantity) DESC, p.product_name) AS 名次
    FROM fact_order f
    JOIN dim_product p ON f.product_id = p.product_id
    WHERE f.order_status = '已支付'
      AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
    GROUP BY p.category, p.product_name
)
SELECT 品类, 商品名称, 销量, 名次
FROM ranked
WHERE 名次 <= 2
ORDER BY 品类, 名次
""".strip(),
    },
    {
        "id": "E3",
        "category": "窗口与排名",
        "question": "2024年上半年，每月实收金额的累计值（年初至今累计）是多少？按月份升序。",
        "intent": "累计求和窗口",
        "tags": ["WINDOW", "SUM_OVER", "累计"],
        "sql": """
WITH monthly AS (
    SELECT strftime('%Y-%m', order_date) AS 月份,
           SUM(amount - discount_amount) AS 实收金额
    FROM fact_order
    WHERE order_status = '已支付'
      AND order_date >= '2024-01-01' AND order_date <= '2024-06-30'
    GROUP BY strftime('%Y-%m', order_date)
)
SELECT 月份,
       ROUND(实收金额, 2) AS 当月实收金额,
       ROUND(SUM(实收金额) OVER (ORDER BY 月份
                                 ROWS BETWEEN UNBOUNDED PRECEDING
                                 AND CURRENT ROW), 2) AS 累计实收金额
FROM monthly
ORDER BY 月份
""".strip(),
    },
    {
        "id": "E4",
        "category": "窗口与排名",
        "question": "2024年上半年，每位客户在自己所属地区内的消费金额排名，列出客户姓名、地区、实收金额和地区内排名。",
        "intent": "分区排名 + 并列处理",
        "tags": ["WINDOW", "PARTITION_BY", "RANK", "JOIN"],
        "sql": """
WITH cust AS (
    SELECT c.customer_id, c.customer_name, c.region,
           SUM(f.amount - f.discount_amount) AS 实收金额
    FROM fact_order f
    JOIN dim_customer c ON f.customer_id = c.customer_id
    WHERE f.order_status = '已支付'
      AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
    GROUP BY c.customer_id, c.customer_name, c.region
)
SELECT customer_name AS 客户姓名,
       region AS 地区,
       ROUND(实收金额, 2) AS 实收金额,
       RANK() OVER (PARTITION BY region ORDER BY 实收金额 DESC) AS 地区内排名
FROM cust
ORDER BY 地区, 地区内排名, 客户姓名
""".strip(),
    },

    # ============ F. 子查询与 CTE (F1-F2) ============
    {
        "id": "F1",
        "category": "子查询与CTE",
        "question": "2024年上半年，消费金额高于全部客户平均消费金额的客户有哪些？",
        "intent": "CTE + 标量子查询比较",
        "tags": ["CTE", "SUBQUERY", "对比"],
        "sql": """
WITH cust AS (
    SELECT c.customer_id, c.customer_name,
           SUM(f.amount - f.discount_amount) AS 实收金额
    FROM fact_order f
    JOIN dim_customer c ON f.customer_id = c.customer_id
    WHERE f.order_status = '已支付'
      AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
    GROUP BY c.customer_id, c.customer_name
)
SELECT customer_name AS 客户姓名,
       ROUND(实收金额, 2) AS 实收金额
FROM cust
WHERE 实收金额 > (SELECT AVG(实收金额) FROM cust)
ORDER BY 实收金额 DESC, 客户姓名
""".strip(),
    },
    {
        "id": "F2",
        "category": "子查询与CTE",
        "question": "2024年上半年，各渠道的实收金额占全渠道总实收金额的比例是多少？",
        "intent": "窗口总计求占比",
        "tags": ["WINDOW", "占比", "GROUP_BY"],
        "sql": """
SELECT c.channel_name AS 渠道,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额,
       ROUND(SUM(f.amount - f.discount_amount) * 1.0
             / SUM(SUM(f.amount - f.discount_amount)) OVER (), 4) AS 占比
FROM fact_order f
JOIN dim_channel c ON f.channel_id = c.channel_id
WHERE f.order_status = '已支付'
  AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY c.channel_name
ORDER BY 实收金额 DESC, 渠道
""".strip(),
    },
]

assert len(GOLDEN_SET) == 30, f"期望 30 条，实际 {len(GOLDEN_SET)}"
assert len({q["id"] for q in GOLDEN_SET}) == 30, "存在重复 id"


if __name__ == "__main__":
    for q in GOLDEN_SET:
        print(f"[{q['id']}] ({q['category']}) {q['question']}")
    print(f"\n共 {len(GOLDEN_SET)} 条")
