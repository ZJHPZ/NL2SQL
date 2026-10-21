-- =====================================================================
-- 电商数仓样例模型 —— MySQL 8.0 版本
--
-- 与 schema.sql（SQLite）**数据等价**，但不共用 DDL：
-- 两边类型系统不同，硬凑一份"通用 DDL"只会两边都不对。
-- 见 docs/11 的一节「同一模型、两份 DDL」。
--
-- 三处刻意的差异，每一处都对应一个必须暴露的方言问题：
--
--   1) 日期存 DATE 而不是 TEXT
--      SQLite 没有日期类型，存 'YYYY-MM-DD' 字符串，靠 substr() 截。
--      MySQL 有 DATE，截取要用 DATE_FORMAT()/YEAR()/MONTH()。
--      → 语义层里写死 substr(f.order_date,1,7) 的月维度，
--        在 MySQL 上不会报错，会**返回错的值**（截数字字符串）。
--        这正是 M3「时间粒度作为一等公民」要修的根因。
--
--   2) 金额存 DECIMAL(12,2) 而不是 REAL
--      SQLite 的 REAL 是双精度浮点，MySQL 数仓里金额用 DECIMAL。
--      已核对：源数据所有金额恰好 2 位小数，DECIMAL 不丢精度。
--      顺带得到一个对比点：SQLite 的 SUM 会累积浮点误差，
--      MySQL 的 DECIMAL SUM 是精确的 —— 两边结果应当在 1e-6 内一致，
--      这个断言本身能证明"等价"不是靠放宽容差蒙出来的。
--
--   3) amount 用 STORED 生成列，两边语法一致
--      MySQL 5.7+ 与 SQLite 3.31+ 都支持；留作"生成列也能正确读出"的对照。
--      SQLite 那边踩过的坑：PRAGMA table_info 看不到生成列，
--      必须 table_xinfo。MySQL 这边对应的是 information_schema 的 EXTRA 列
--      里出现 'STORED GENERATED'。
--
-- 字符集：utf8mb4。维度列全是中文，utf8mb3 会吞掉 emoji 之类，
-- 这里没有 emoji，但选 utf8mb4 不花任何代价。
-- =====================================================================

SET NAMES utf8mb4;

DROP TABLE IF EXISTS fact_order;
DROP TABLE IF EXISTS dim_channel;
DROP TABLE IF EXISTS dim_product;
DROP TABLE IF EXISTS dim_customer;
DROP TABLE IF EXISTS dim_date;

-- 日期维度
CREATE TABLE dim_date (
    date_key    DATE        NOT NULL PRIMARY KEY,   -- 'YYYY-MM-DD'
    year_num    INT         NOT NULL,
    month_num   INT         NOT NULL,
    month_name  VARCHAR(16) NOT NULL,
    quarter_num INT         NOT NULL,
    week_num    INT         NOT NULL,
    is_weekend  TINYINT     NOT NULL                -- 1=周末
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 客户维度
CREATE TABLE dim_customer (
    customer_id     INT         NOT NULL PRIMARY KEY,
    customer_name   VARCHAR(64) NOT NULL,
    customer_level  VARCHAR(16) NOT NULL,   -- 普通/白银/黄金/铂金
    region          VARCHAR(16) NOT NULL,   -- 华东/华北/华南/西南/华中
    register_date   DATE        NOT NULL,
    is_new_customer TINYINT     NOT NULL    -- 1=新客
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 商品维度
CREATE TABLE dim_product (
    product_id   INT           NOT NULL PRIMARY KEY,
    product_name VARCHAR(64)   NOT NULL,
    category     VARCHAR(16)   NOT NULL,    -- 数码/家电/服饰/食品/图书
    brand        VARCHAR(32)   NOT NULL,
    unit_cost    DECIMAL(12,2) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 渠道维度
CREATE TABLE dim_channel (
    channel_id   INT         NOT NULL PRIMARY KEY,
    channel_name VARCHAR(32) NOT NULL,     -- 淘宝/京东/抖音/官网/线下门店
    channel_type VARCHAR(16) NOT NULL      -- 线上/线下
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 订单事实表（order 粒度）
CREATE TABLE fact_order (
    order_id        INT           NOT NULL PRIMARY KEY,
    order_date      DATE          NOT NULL,
    customer_id     INT           NOT NULL,
    product_id      INT           NOT NULL,
    channel_id      INT           NOT NULL,
    quantity        INT           NOT NULL,
    unit_price      DECIMAL(12,2) NOT NULL,   -- 成交单价
    discount_amount DECIMAL(12,2) NOT NULL,   -- 优惠金额
    order_status    VARCHAR(16)   NOT NULL,   -- 已支付/已退款/已取消
    amount          DECIMAL(14,2) GENERATED ALWAYS AS (quantity * unit_price) STORED
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE INDEX idx_fact_order_date     ON fact_order (order_date);
CREATE INDEX idx_fact_order_customer ON fact_order (customer_id);
CREATE INDEX idx_fact_order_product  ON fact_order (product_id);
CREATE INDEX idx_fact_order_channel  ON fact_order (channel_id);

-- ---------------------------------------------------------------------
-- 口径约定与 SQLite 版完全一致（语义层不该因为换库而改口径）：
--   有效订单     : order_status = '已支付'
--   GMV(成交金额): SUM(amount)                    —— 含优惠，未剔除退款
--   实收金额     : SUM(amount - discount_amount)  —— 已支付订单的净收入
--   客单价       : 实收金额 / 去重客户数
--   退款率       : 已退款订单数 / 全部订单数
-- ---------------------------------------------------------------------
