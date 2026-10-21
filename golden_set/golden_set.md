# 黄金问题集（30 题）

> 由 `build_baseline.py` 自动生成，**请勿手改**——修改请改 `golden_questions.py` 后重跑。
> 标准结果为 SQLite 真实执行产出，可用于自动判分（结果集比对）。

**数据窗口**：2024-01-01 ~ 2024-06-30　**本月**=2024-06　**上月**=2024-05

## 覆盖情况

| 分组 | 题号 | 数量 |
|---|---|---|
| 基础过滤与聚合 | A1, A2, A3, A4, A5 | 5 |
| 分组聚合 | B1, B2, B3, B4, B5, B6 | 6 |
| 时间趋势 | C1, C2, C3, C4, C5, C6 | 6 |
| 多表关联 | D1, D2, D3, D4, D5, D6, D7 | 7 |
| 窗口与排名 | E1, E2, E3, E4 | 4 |
| 子查询与CTE | F1, F2 | 2 |

## A1　2024年6月，数码品类的实收金额是多少？

- 分组：基础过滤与聚合　标签：`AGG`, `JOIN`, `WHERE`, `口径:实收金额`　结果行数：1

```sql
SELECT ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额
FROM fact_order f
JOIN dim_product p ON f.product_id = p.product_id
WHERE p.category = '数码'
  AND f.order_status = '已支付'
  AND f.order_date >= '2024-06-01' AND f.order_date <= '2024-06-30'
```

**标准结果**：`实收金额`

| 实收金额 |
|---|
| 99998.5 |

## A2　2024年6月，华东地区的有效订单数和实收金额分别是多少？

- 分组：基础过滤与聚合　标签：`AGG`, `JOIN`, `COUNT`, `口径:实收金额`　结果行数：1

```sql
SELECT COUNT(*) AS 有效订单数,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额
FROM fact_order f
JOIN dim_customer c ON f.customer_id = c.customer_id
WHERE c.region = '华东'
  AND f.order_status = '已支付'
  AND f.order_date >= '2024-06-01' AND f.order_date <= '2024-06-30'
```

**标准结果**：`有效订单数`　`实收金额`

| 有效订单数 | 实收金额 |
|---|---|
| 40 | 62466.51 |

## A3　2024年6月，单个订单成交金额超过5000元的订单有几笔？最高一笔多少？

- 分组：基础过滤与聚合　标签：`AGG`, `MAX`, `WHERE`　结果行数：1

```sql
SELECT COUNT(*) AS 订单数,
       MAX(amount) AS 最高成交金额
FROM fact_order
WHERE order_status = '已支付'
  AND amount > 5000
  AND order_date >= '2024-06-01' AND order_date <= '2024-06-30'
```

**标准结果**：`订单数`　`最高成交金额`

| 订单数 | 最高成交金额 |
|---|---|
| 9 | 14253 |

## A4　2024年上半年，产生过有效订单的客户有多少人？总有效订单数是多少？

- 分组：基础过滤与聚合　标签：`AGG`, `COUNT_DISTINCT`　结果行数：1

```sql
SELECT COUNT(DISTINCT customer_id) AS 客户数,
       COUNT(*) AS 有效订单数
FROM fact_order
WHERE order_status = '已支付'
  AND order_date >= '2024-01-01' AND order_date <= '2024-06-30'
```

**标准结果**：`客户数`　`有效订单数`

| 客户数 | 有效订单数 |
|---|---|
| 12 | 661 |

## A5　2024年上半年，'已退款'和'已取消'的订单各有多少笔？

- 分组：基础过滤与聚合　标签：`GROUP_BY`, `WHERE`　结果行数：2

```sql
SELECT order_status AS 订单状态,
       COUNT(*) AS 订单数
FROM fact_order
WHERE order_status IN ('已退款', '已取消')
  AND order_date >= '2024-01-01' AND order_date <= '2024-06-30'
GROUP BY order_status
ORDER BY 订单数 DESC, 订单状态
```

**标准结果**：`订单状态`　`订单数`

| 订单状态 | 订单数 |
|---|---|
| 已取消 | 76 |
| 已退款 | 73 |

## B1　2024年上半年，各渠道的实收金额和有效订单数是多少？按实收金额降序排列。

- 分组：分组聚合　标签：`GROUP_BY`, `JOIN`, `ORDER_BY`, `口径:实收金额`　结果行数：5

```sql
SELECT c.channel_name AS 渠道,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额,
       COUNT(*) AS 有效订单数
FROM fact_order f
JOIN dim_channel c ON f.channel_id = c.channel_id
WHERE f.order_status = '已支付'
  AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY c.channel_name
ORDER BY 实收金额 DESC, 渠道
```

**标准结果**：`渠道`　`实收金额`　`有效订单数`

| 渠道 | 实收金额 | 有效订单数 |
|---|---|---|
| 淘宝 | 216339.57 | 112 |
| 官网 | 203928.84 | 144 |
| 京东 | 196581.44 | 125 |
| 抖音 | 192258.19 | 131 |
| 线下门店 | 182883.73 | 149 |

## B2　2024年上半年，各商品品类的实收金额和销量（件数）是多少？按实收金额降序。

- 分组：分组聚合　标签：`GROUP_BY`, `JOIN`, `SUM`　结果行数：5

```sql
SELECT p.category AS 品类,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额,
       SUM(f.quantity) AS 销量
FROM fact_order f
JOIN dim_product p ON f.product_id = p.product_id
WHERE f.order_status = '已支付'
  AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY p.category
ORDER BY 实收金额 DESC, 品类
```

**标准结果**：`品类`　`实收金额`　`销量`

| 品类 | 实收金额 | 销量 |
|---|---|---|
| 数码 | 555042.76 | 330 |
| 家电 | 270406.93 | 225 |
| 服饰 | 106909.1 | 214 |
| 食品 | 36077.53 | 223 |
| 图书 | 23555.45 | 201 |

## B3　2024年上半年，哪个品牌实收金额最高？列出各品牌的实收金额。

- 分组：分组聚合　标签：`GROUP_BY`, `JOIN`, `ORDER_BY`, `LIMIT`　结果行数：13

```sql
SELECT p.brand AS 品牌,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额
FROM fact_order f
JOIN dim_product p ON f.product_id = p.product_id
WHERE f.order_status = '已支付'
  AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY p.brand
ORDER BY 实收金额 DESC, 品牌
```

**标准结果**：`品牌`　`实收金额`

| 品牌 | 实收金额 |
|---|---|
| 星辰 | 429372.22 |
| 洁宝 | 239790.53 |
| 视界 | 90518.14 |
| 疾风 | 42797.03 |
| 云裳 | 42214.13 |
| 黑轴 | 35152.4 |
| 厨乐 | 30616.4 |
| 风行 | 21897.94 |
| 烘焙家 | 14748.06 |
| 博文 | 13273.25 |
| 山野 | 12849.4 |
| 华章 | 10282.2 |
| ... | 共 13 行 |

## B4　2024年上半年，各渠道的客单价是多少？按客单价降序排列。

- 分组：分组聚合　标签：`GROUP_BY`, `COUNT_DISTINCT`, `口径:客单价`　结果行数：5

```sql
SELECT c.channel_name AS 渠道,
       ROUND(SUM(f.amount - f.discount_amount) * 1.0
             / COUNT(DISTINCT f.customer_id), 2) AS 客单价
FROM fact_order f
JOIN dim_channel c ON f.channel_id = c.channel_id
WHERE f.order_status = '已支付'
  AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY c.channel_name
ORDER BY 客单价 DESC, 渠道
```

**标准结果**：`渠道`　`客单价`

| 渠道 | 客单价 |
|---|---|
| 淘宝 | 18028.3 |
| 官网 | 16994.07 |
| 京东 | 16381.79 |
| 抖音 | 16021.52 |
| 线下门店 | 15240.31 |

## B5　2024年上半年，实收金额超过20000元的商品品类有哪些？

- 分组：分组聚合　标签：`GROUP_BY`, `HAVING`　结果行数：5

```sql
SELECT p.category AS 品类,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额
FROM fact_order f
JOIN dim_product p ON f.product_id = p.product_id
WHERE f.order_status = '已支付'
  AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY p.category
HAVING SUM(f.amount - f.discount_amount) > 20000
ORDER BY 实收金额 DESC, 品类
```

**标准结果**：`品类`　`实收金额`

| 品类 | 实收金额 |
|---|---|
| 数码 | 555042.76 |
| 家电 | 270406.93 |
| 服饰 | 106909.1 |
| 食品 | 36077.53 |
| 图书 | 23555.45 |

## B6　2024年上半年，各地区的已退款订单数占该地区全部订单数的比例是多少？列出地区和退款率。

- 分组：分组聚合　标签：`GROUP_BY`, `CASE_WHEN`, `口径:退款率`　结果行数：5

```sql
SELECT c.region AS 地区,
       ROUND(SUM(CASE WHEN f.order_status = '已退款' THEN 1 ELSE 0 END) * 1.0
             / COUNT(*), 4) AS 退款率
FROM fact_order f
JOIN dim_customer c ON f.customer_id = c.customer_id
WHERE f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY c.region
ORDER BY 退款率 DESC, 地区
```

**标准结果**：`地区`　`退款率`

| 地区 | 退款率 |
|---|---|
| 华南 | 0.1168 |
| 西南 | 0.1085 |
| 华东 | 0.0906 |
| 华北 | 0.0746 |
| 华中 | 0.0597 |

## C1　2024年上半年，每月的有效订单数和实收金额是多少？按月份升序排列。

- 分组：时间趋势　标签：`TIME_BUCKET`, `strftime`, `GROUP_BY`　结果行数：6

```sql
SELECT strftime('%Y-%m', order_date) AS 月份,
       COUNT(*) AS 有效订单数,
       ROUND(SUM(amount - discount_amount), 2) AS 实收金额
FROM fact_order
WHERE order_status = '已支付'
  AND order_date >= '2024-01-01' AND order_date <= '2024-06-30'
GROUP BY strftime('%Y-%m', order_date)
ORDER BY 月份
```

**标准结果**：`月份`　`有效订单数`　`实收金额`

| 月份 | 有效订单数 | 实收金额 |
|---|---|---|
| 2024-01 | 117 | 150971.9 |
| 2024-02 | 96 | 140703.69 |
| 2024-03 | 116 | 155735.02 |
| 2024-04 | 106 | 162493.29 |
| 2024-05 | 113 | 210179.88 |
| 2024-06 | 113 | 171907.99 |

## C2　2024年上半年，每月的有效订单数是多少？按月份升序。

- 分组：时间趋势　标签：`TIME_BUCKET`, `COUNT`　结果行数：6

```sql
SELECT strftime('%Y-%m', order_date) AS 月份,
       COUNT(*) AS 有效订单数
FROM fact_order
WHERE order_status = '已支付'
  AND order_date >= '2024-01-01' AND order_date <= '2024-06-30'
GROUP BY strftime('%Y-%m', order_date)
ORDER BY 月份
```

**标准结果**：`月份`　`有效订单数`

| 月份 | 有效订单数 |
|---|---|
| 2024-01 | 117 |
| 2024-02 | 96 |
| 2024-03 | 116 |
| 2024-04 | 106 |
| 2024-05 | 113 |
| 2024-06 | 113 |

## C3　2024年上半年，每周的有效订单数是多少？按周升序排列。

- 分组：时间趋势　标签：`TIME_BUCKET`, `strftime`　结果行数：26

```sql
SELECT strftime('%Y-%W', order_date) AS 年周,
       COUNT(*) AS 有效订单数
FROM fact_order
WHERE order_status = '已支付'
  AND order_date >= '2024-01-01' AND order_date <= '2024-06-30'
GROUP BY strftime('%Y-%W', order_date)
ORDER BY 年周
```

**标准结果**：`年周`　`有效订单数`

| 年周 | 有效订单数 |
|---|---|
| 2024-01 | 30 |
| 2024-02 | 23 |
| 2024-03 | 36 |
| 2024-04 | 22 |
| 2024-05 | 21 |
| 2024-06 | 26 |
| 2024-07 | 23 |
| 2024-08 | 19 |
| 2024-09 | 29 |
| 2024-10 | 27 |
| 2024-11 | 26 |
| 2024-12 | 25 |
| ... | 共 26 行 |

## C4　本月（2024年6月）和上月（2024年5月）的实收金额分别是多少？

- 分组：时间趋势　标签：`CASE_WHEN`, `口径:实收金额`, `对比`　结果行数：1

```sql
SELECT ROUND(SUM(CASE WHEN order_date >= '2024-06-01' AND order_date <= '2024-06-30'
                      THEN amount - discount_amount ELSE 0 END), 2) AS 本月实收金额,
       ROUND(SUM(CASE WHEN order_date >= '2024-05-01' AND order_date <= '2024-05-31'
                      THEN amount - discount_amount ELSE 0 END), 2) AS 上月实收金额
FROM fact_order
WHERE order_status = '已支付'
  AND order_date >= '2024-05-01' AND order_date <= '2024-06-30'
```

**标准结果**：`本月实收金额`　`上月实收金额`

| 本月实收金额 | 上月实收金额 |
|---|---|
| 171907.99 | 210179.88 |

## C5　2024年上半年，线上渠道和线下渠道各自的实收金额是多少？按月列出。

- 分组：时间趋势　标签：`TIME_BUCKET`, `CASE_WHEN`, `JOIN`　结果行数：6

```sql
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
```

**标准结果**：`月份`　`线上实收金额`　`线下实收金额`

| 月份 | 线上实收金额 | 线下实收金额 |
|---|---|---|
| 2024-01 | 123289.39 | 27682.51 |
| 2024-02 | 111331.08 | 29372.61 |
| 2024-03 | 118006.98 | 37728.04 |
| 2024-04 | 141432.83 | 21060.46 |
| 2024-05 | 176396.52 | 33783.36 |
| 2024-06 | 138651.24 | 33256.75 |

## C6　2024年6月，每天的实收金额是多少？只列出实收金额大于3000元的日期，按日期升序。

- 分组：时间趋势　标签：`TIME_BUCKET`, `HAVING`, `日期粒度`　结果行数：20

```sql
SELECT order_date AS 日期,
       ROUND(SUM(amount - discount_amount), 2) AS 实收金额
FROM fact_order
WHERE order_status = '已支付'
  AND order_date >= '2024-06-01' AND order_date <= '2024-06-30'
GROUP BY order_date
HAVING SUM(amount - discount_amount) > 3000
ORDER BY 日期
```

**标准结果**：`日期`　`实收金额`

| 日期 | 实收金额 |
|---|---|
| 2024-06-01 | 7579.76 |
| 2024-06-02 | 9471.26 |
| 2024-06-05 | 8105.17 |
| 2024-06-06 | 4452.78 |
| 2024-06-07 | 8305.81 |
| 2024-06-08 | 15506.9 |
| 2024-06-13 | 6527.78 |
| 2024-06-14 | 4805 |
| 2024-06-15 | 7145.5 |
| 2024-06-16 | 4157.61 |
| 2024-06-18 | 4154.62 |
| 2024-06-19 | 7305.06 |
| ... | 共 20 行 |

## D1　2024年上半年，消费金额最高的10位客户是谁？列出客户姓名、所属地区和实收金额。

- 分组：多表关联　标签：`JOIN`, `ORDER_BY`, `LIMIT`, `TopN`　结果行数：10

```sql
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
```

**标准结果**：`客户姓名`　`地区`　`实收金额`

| 客户姓名 | 地区 | 实收金额 |
|---|---|---|
| 冯超 | 华东 | 161826.97 |
| 杨帆 | 华中 | 96498.57 |
| 赵敏 | 华北 | 94462.86 |
| 刘洋 | 华东 | 92241.74 |
| 孙磊 | 华东 | 77237.69 |
| 王强 | 华南 | 74441.27 |
| 吴昊 | 西南 | 72034.84 |
| 郑爽 | 华中 | 69097.98 |
| 陈静 | 西南 | 68767.54 |
| 周婷 | 华南 | 65749.71 |

## D2　2024年上半年，各客户等级的客户数和实收金额分别是多少？

- 分组：多表关联　标签：`JOIN`, `GROUP_BY`, `COUNT_DISTINCT`　结果行数：4

```sql
SELECT c.customer_level AS 客户等级,
       COUNT(DISTINCT c.customer_id) AS 客户数,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额
FROM fact_order f
JOIN dim_customer c ON f.customer_id = c.customer_id
WHERE f.order_status = '已支付'
  AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY c.customer_level
ORDER BY 实收金额 DESC, 客户等级
```

**标准结果**：`客户等级`　`客户数`　`实收金额`

| 客户等级 | 客户数 | 实收金额 |
|---|---|---|
| 普通 | 4 | 420566.41 |
| 白银 | 3 | 226824.36 |
| 黄金 | 3 | 192922.04 |
| 铂金 | 2 | 151678.96 |

## D3　2024年上半年，新客户和老客户各贡献了多少实收金额？

- 分组：多表关联　标签：`JOIN`, `CASE_WHEN`, `口径:新老客`　结果行数：2

```sql
SELECT CASE WHEN c.is_new_customer = 1 THEN '新客户' ELSE '老客户' END AS 客户类型,
       COUNT(DISTINCT c.customer_id) AS 客户数,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额
FROM fact_order f
JOIN dim_customer c ON f.customer_id = c.customer_id
WHERE f.order_status = '已支付'
  AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY CASE WHEN c.is_new_customer = 1 THEN '新客户' ELSE '老客户' END
ORDER BY 实收金额 DESC, 客户类型
```

**标准结果**：`客户类型`　`客户数`　`实收金额`

| 客户类型 | 客户数 | 实收金额 |
|---|---|---|
| 老客户 | 7 | 521359.44 |
| 新客户 | 5 | 470632.33 |

## D4　2024年上半年，销量前5的商品是哪些？列出商品名称、品类和销量。

- 分组：多表关联　标签：`JOIN`, `ORDER_BY`, `LIMIT`, `TopN`　结果行数：5

```sql
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
```

**标准结果**：`商品名称`　`品类`　`销量`

| 商品名称 | 品类 | 销量 |
|---|---|---|
| 智能手机Pro | 数码 | 103 |
| 空气净化器 | 家电 | 97 |
| 机械键盘 | 数码 | 96 |
| 运动跑鞋 | 服饰 | 81 |
| 进口牛奶 | 食品 | 78 |

## D5　2024年上半年，各地区的消费客户数和实收金额是多少？

- 分组：多表关联　标签：`JOIN`, `GROUP_BY`, `COUNT_DISTINCT`　结果行数：5

```sql
SELECT c.region AS 地区,
       COUNT(DISTINCT f.customer_id) AS 消费客户数,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额
FROM fact_order f
JOIN dim_customer c ON f.customer_id = c.customer_id
WHERE f.order_status = '已支付'
  AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY c.region
ORDER BY 实收金额 DESC, 地区
```

**标准结果**：`地区`　`消费客户数`　`实收金额`

| 地区 | 消费客户数 | 实收金额 |
|---|---|---|
| 华东 | 4 | 389711.19 |
| 华中 | 2 | 165596.55 |
| 华北 | 2 | 155690.67 |
| 西南 | 2 | 140802.38 |
| 华南 | 2 | 140190.98 |

## D6　2024年6月，各渠道的销量和实收金额是多少？按渠道列出。

- 分组：多表关联　标签：`JOIN`, `GROUP_BY`, `SUM`　结果行数：5

```sql
SELECT c.channel_name AS 渠道,
       SUM(f.quantity) AS 销量,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额
FROM fact_order f
JOIN dim_channel c ON f.channel_id = c.channel_id
WHERE f.order_status = '已支付'
  AND f.order_date >= '2024-06-01' AND f.order_date <= '2024-06-30'
GROUP BY c.channel_name
ORDER BY 实收金额 DESC, 渠道
```

**标准结果**：`渠道`　`销量`　`实收金额`

| 渠道 | 销量 | 实收金额 |
|---|---|---|
| 淘宝 | 44 | 50322 |
| 抖音 | 53 | 43285.92 |
| 线下门店 | 39 | 33256.75 |
| 官网 | 46 | 25316.27 |
| 京东 | 24 | 19727.05 |

## D7　2024年上半年，各商品品类的毛利率是多少？（毛利率 = (实收金额 - 商品成本) / 实收金额）

- 分组：多表关联　标签：`JOIN`, `GROUP_BY`, `口径:毛利率`, `复杂计算`　结果行数：5

```sql
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
```

**标准结果**：`品类`　`实收金额`　`成本`　`毛利率`

| 品类 | 实收金额 | 成本 | 毛利率 |
|---|---|---|---|
| 数码 | 555042.76 | 394060 | 0.29 |
| 服饰 | 106909.1 | 76880 | 0.2809 |
| 家电 | 270406.93 | 196700 | 0.2726 |
| 食品 | 36077.53 | 26244 | 0.2726 |
| 图书 | 23555.45 | 17315 | 0.2649 |

## E1　2024年上半年，各渠道按实收金额从高到低排名，列出渠道、实收金额和排名。

- 分组：窗口与排名　标签：`WINDOW`, `RANK`, `ORDER_BY`　结果行数：5

```sql
SELECT c.channel_name AS 渠道,
       ROUND(SUM(f.amount - f.discount_amount), 2) AS 实收金额,
       RANK() OVER (ORDER BY SUM(f.amount - f.discount_amount) DESC) AS 排名
FROM fact_order f
JOIN dim_channel c ON f.channel_id = c.channel_id
WHERE f.order_status = '已支付'
  AND f.order_date >= '2024-01-01' AND f.order_date <= '2024-06-30'
GROUP BY c.channel_name
ORDER BY 排名, 渠道
```

**标准结果**：`渠道`　`实收金额`　`排名`

| 渠道 | 实收金额 | 排名 |
|---|---|---|
| 淘宝 | 216339.57 | 1 |
| 官网 | 203928.84 | 2 |
| 京东 | 196581.44 | 3 |
| 抖音 | 192258.19 | 4 |
| 线下门店 | 182883.73 | 5 |

## E2　2024年上半年，各商品品类内部销量前2的商品是哪些？列出品类、商品名称和销量。

- 分组：窗口与排名　标签：`WINDOW`, `PARTITION_BY`, `ROW_NUMBER`, `CTE`　结果行数：10

```sql
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
```

**标准结果**：`品类`　`商品名称`　`销量`　`名次`

| 品类 | 商品名称 | 销量 | 名次 |
|---|---|---|---|
| 图书 | 机器学习实战 | 77 | 1 |
| 图书 | 数据结构与算法 | 64 | 2 |
| 家电 | 空气净化器 | 97 | 1 |
| 家电 | 电饭煲 | 70 | 2 |
| 数码 | 智能手机Pro | 103 | 1 |
| 数码 | 机械键盘 | 96 | 2 |
| 服饰 | 运动跑鞋 | 81 | 1 |
| 服饰 | 女士连衣裙 | 72 | 2 |
| 食品 | 进口牛奶 | 78 | 1 |
| 食品 | 坚果礼盒 | 76 | 2 |

## E3　2024年上半年，每月实收金额的累计值（年初至今累计）是多少？按月份升序。

- 分组：窗口与排名　标签：`WINDOW`, `SUM_OVER`, `累计`　结果行数：6

```sql
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
```

**标准结果**：`月份`　`当月实收金额`　`累计实收金额`

| 月份 | 当月实收金额 | 累计实收金额 |
|---|---|---|
| 2024-01 | 150971.9 | 150971.9 |
| 2024-02 | 140703.69 | 291675.59 |
| 2024-03 | 155735.02 | 447410.61 |
| 2024-04 | 162493.29 | 609903.9 |
| 2024-05 | 210179.88 | 820083.78 |
| 2024-06 | 171907.99 | 991991.77 |

## E4　2024年上半年，每位客户在自己所属地区内的消费金额排名，列出客户姓名、地区、实收金额和地区内排名。

- 分组：窗口与排名　标签：`WINDOW`, `PARTITION_BY`, `RANK`, `JOIN`　结果行数：12

```sql
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
```

**标准结果**：`客户姓名`　`地区`　`实收金额`　`地区内排名`

| 客户姓名 | 地区 | 实收金额 | 地区内排名 |
|---|---|---|---|
| 冯超 | 华东 | 161826.97 | 1 |
| 刘洋 | 华东 | 92241.74 | 2 |
| 孙磊 | 华东 | 77237.69 | 3 |
| 张伟 | 华东 | 58404.79 | 4 |
| 杨帆 | 华中 | 96498.57 | 1 |
| 郑爽 | 华中 | 69097.98 | 2 |
| 赵敏 | 华北 | 94462.86 | 1 |
| 李娜 | 华北 | 61227.81 | 2 |
| 王强 | 华南 | 74441.27 | 1 |
| 周婷 | 华南 | 65749.71 | 2 |
| 吴昊 | 西南 | 72034.84 | 1 |
| 陈静 | 西南 | 68767.54 | 2 |

## F1　2024年上半年，消费金额高于全部客户平均消费金额的客户有哪些？

- 分组：子查询与CTE　标签：`CTE`, `SUBQUERY`, `对比`　结果行数：4

```sql
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
```

**标准结果**：`客户姓名`　`实收金额`

| 客户姓名 | 实收金额 |
|---|---|
| 冯超 | 161826.97 |
| 杨帆 | 96498.57 |
| 赵敏 | 94462.86 |
| 刘洋 | 92241.74 |

## F2　2024年上半年，各渠道的实收金额占全渠道总实收金额的比例是多少？

- 分组：子查询与CTE　标签：`WINDOW`, `占比`, `GROUP_BY`　结果行数：5

```sql
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
```

**标准结果**：`渠道`　`实收金额`　`占比`

| 渠道 | 实收金额 | 占比 |
|---|---|---|
| 淘宝 | 216339.57 | 0.2181 |
| 官网 | 203928.84 | 0.2056 |
| 京东 | 196581.44 | 0.1982 |
| 抖音 | 192258.19 | 0.1938 |
| 线下门店 | 182883.73 | 0.1844 |
