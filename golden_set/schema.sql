-- =====================================================================
-- 电商数仓样例模型（P0: SQLite / MySQL 通用）
-- grain: 一行 = 一个订单（order 粒度）
-- 时间锚点: 数据截止 2024-06-30，便于「上月/本月」等相对问题
-- =====================================================================

-- 日期维度
CREATE TABLE dim_date (
    date_key    TEXT    NOT NULL PRIMARY KEY,   -- 'YYYY-MM-DD'
    year_num    INTEGER NOT NULL,
    month_num   INTEGER NOT NULL,
    month_name  TEXT    NOT NULL,
    quarter_num INTEGER NOT NULL,
    week_num    INTEGER NOT NULL,
    is_weekend  INTEGER NOT NULL                -- 1=周末
);

-- 客户维度
CREATE TABLE dim_customer (
    customer_id    INTEGER NOT NULL PRIMARY KEY,
    customer_name  TEXT    NOT NULL,
    customer_level TEXT    NOT NULL,   -- 普通/白银/黄金/铂金
    region         TEXT    NOT NULL,   -- 华东/华北/华南/西南/华中
    register_date  TEXT    NOT NULL,
    is_new_customer INTEGER NOT NULL   -- 1=新客
);

-- 商品维度
CREATE TABLE dim_product (
    product_id    INTEGER NOT NULL PRIMARY KEY,
    product_name  TEXT    NOT NULL,
    category      TEXT    NOT NULL,    -- 数码/家电/服饰/食品/图书
    brand         TEXT    NOT NULL,
    unit_cost     REAL    NOT NULL
);

-- 渠道维度
CREATE TABLE dim_channel (
    channel_id   INTEGER NOT NULL PRIMARY KEY,
    channel_name TEXT    NOT NULL,     -- 淘宝/京东/抖音/官网/线下门店
    channel_type TEXT    NOT NULL      -- 线上/线下
);

-- 订单事实表（order 粒度，已轻度汇总）
CREATE TABLE fact_order (
    order_id       INTEGER NOT NULL PRIMARY KEY,
    order_date     TEXT    NOT NULL,   -- 'YYYY-MM-DD'
    customer_id    INTEGER NOT NULL,
    product_id     INTEGER NOT NULL,
    channel_id     INTEGER NOT NULL,
    quantity       INTEGER NOT NULL,
    unit_price     REAL    NOT NULL,   -- 成交单价
    discount_amount REAL   NOT NULL,   -- 优惠金额
    order_status   TEXT    NOT NULL,   -- 已支付/已退款/已取消
    -- 生成列：amount = quantity * unit_price（成交金额，未减优惠）
    amount         REAL    GENERATED ALWAYS AS (quantity * unit_price) STORED
);

CREATE INDEX idx_fact_order_date     ON fact_order (order_date);
CREATE INDEX idx_fact_order_customer ON fact_order (customer_id);
CREATE INDEX idx_fact_order_product  ON fact_order (product_id);
CREATE INDEX idx_fact_order_channel  ON fact_order (channel_id);

-- ---------------------------------------------------------------------
-- 口径约定（供语义层使用，必须与 SQL 对齐）
-- ---------------------------------------------------------------------
-- 有效订单     : order_status = '已支付'
-- GMV(成交金额): SUM(amount)                    —— 含优惠，未剔除退款
-- 实收金额     : SUM(amount - discount_amount)  —— 已支付订单的净收入
-- 客单价       : 实收金额 / 去重客户数
-- 退款率       : 已退款订单数 / 全部订单数
--
-- MySQL 适配说明：
--   1) TEXT 日期 → DATE；GENERATED ... STORED → MySQL 5.7+ 亦支持
--   2) strftime('%Y-%m', order_date) → DATE_FORMAT(order_date, '%Y-%m')
--   3) 其余语法（窗口函数、CTE）MySQL 8.0+ 兼容
-- =====================================================================
