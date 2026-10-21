-- =====================================================================
-- 电商数仓样例模型 —— PostgreSQL 18 版本（第三引擎）
--
-- 与 schema.sql（SQLite）、schema_mysql.sql（MySQL 8）**数据等价**，
-- 但不共用 DDL：三个引擎的类型系统与函数族不同，硬凑一份"通用 DDL"
-- 只会三边都不对。三份 DDL 的差异恰好是方言差异的清单。
--
-- 与 SQLite 版对照，三处刻意的差异：
--
--   1) 日期存 DATE 而不是 TEXT
--      SQLite 没有日期类型，存 'YYYY-MM-DD' 字符串，靠 substr() 截。
--      PostgreSQL 与 MySQL 一样有 DATE；截取用 TO_CHAR()/EXTRACT()。
--      → 语义层里写死 substr(f.order_date,1,7) 的月维度，
--        在 PG 上不会报错，会**返回错的值**（截数字字符串）。
--        这是 M3「时间粒度作为一等公民」针对的同一类根因。
--
--   2) 金额存 NUMERIC(12,2) 而不是 REAL
--      PostgreSQL 的 NUMERIC 是精确十进制，与 MySQL 的 DECIMAL 同族。
--      适配器会在读取时归一化成 float（与 SQLite 口径一致）；
--      SUM 在 PG 上是精确的，两边结果应在 1e-6 内一致。
--
--   3) amount 用 STORED 生成列
--      PostgreSQL 12+ 支持；文本与 SQLite/MySQL 完全一致：
--        GENERATED ALWAYS AS (quantity * unit_price) STORED
--      PG 用 pg_attribute.attgenerated = 's' 识别（不是 information_schema），
--      见 nl2sql/sources/postgres.py 的模块 docstring 第 4 条。
--
-- 字符集：数据库创建时指定 UTF8；列上不需要额外声明。
-- =====================================================================

DROP TABLE IF EXISTS fact_order;
DROP TABLE IF EXISTS dim_channel;
DROP TABLE IF EXISTS dim_product;
DROP TABLE IF EXISTS dim_customer;
DROP TABLE IF EXISTS dim_date;

-- 日期维度
CREATE TABLE dim_date (
    date_key    DATE        NOT NULL PRIMARY KEY,   -- 'YYYY-MM-DD'
    year_num    INTEGER     NOT NULL,
    month_num   INTEGER     NOT NULL,
    month_name  VARCHAR(16) NOT NULL,
    quarter_num INTEGER     NOT NULL,
    week_num    INTEGER     NOT NULL,
    is_weekend  INTEGER     NOT NULL                -- 1=周末
);

-- 客户维度
CREATE TABLE dim_customer (
    customer_id     INTEGER     NOT NULL PRIMARY KEY,
    customer_name   VARCHAR(64) NOT NULL,
    customer_level  VARCHAR(16) NOT NULL,   -- 普通/白银/黄金/铂金
    region          VARCHAR(16) NOT NULL,   -- 华东/华北/华南/西南/华中
    register_date   DATE        NOT NULL,
    is_new_customer INTEGER     NOT NULL    -- 1=新客
);

-- 商品维度
CREATE TABLE dim_product (
    product_id   INTEGER       NOT NULL PRIMARY KEY,
    product_name VARCHAR(64)   NOT NULL,
    category     VARCHAR(16)   NOT NULL,    -- 数码/家电/服饰/食品/图书
    brand        VARCHAR(32)   NOT NULL,
    unit_cost    NUMERIC(12,2) NOT NULL
);

-- 渠道维度
CREATE TABLE dim_channel (
    channel_id   INTEGER     NOT NULL PRIMARY KEY,
    channel_name VARCHAR(32) NOT NULL,     -- 淘宝/京东/抖音/官网/线下门店
    channel_type VARCHAR(16) NOT NULL      -- 线上/线下
);

-- 订单事实表（order 粒度）
CREATE TABLE fact_order (
    order_id        INTEGER       NOT NULL PRIMARY KEY,
    order_date      DATE          NOT NULL,
    customer_id     INTEGER       NOT NULL,
    product_id      INTEGER       NOT NULL,
    channel_id      INTEGER       NOT NULL,
    quantity        INTEGER       NOT NULL,
    unit_price      NUMERIC(12,2) NOT NULL,   -- 成交单价
    discount_amount NUMERIC(12,2) NOT NULL,   -- 优惠金额
    order_status    VARCHAR(16)   NOT NULL,   -- 已支付/已退款/已取消
    -- 生成列：amount = quantity * unit_price（成交金额，未减优惠）
    amount          NUMERIC(14,2) GENERATED ALWAYS AS (quantity * unit_price) STORED
);

CREATE INDEX idx_fact_order_date     ON fact_order (order_date);
CREATE INDEX idx_fact_order_customer ON fact_order (customer_id);
CREATE INDEX idx_fact_order_product  ON fact_order (product_id);
CREATE INDEX idx_fact_order_channel  ON fact_order (channel_id);

-- ---------------------------------------------------------------------
-- 口径约定与 SQLite/MySQL 版完全一致（语义层不该因为换库而改口径）：
--   有效订单     : order_status = '已支付'
--   GMV(成交金额): SUM(amount)                    —— 含优惠，未剔除退款
--   实收金额     : SUM(amount - discount_amount)  —— 已支付订单的净收入
--   客单价       : 实收金额 / 去重客户数
--   退款率       : 已退款订单数 / 全部订单数
-- ---------------------------------------------------------------------
