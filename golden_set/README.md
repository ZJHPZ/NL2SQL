# 黄金评测集使用说明

面向 **个人开发者、小而精** 定位的最小可判分评测集。P0 数据源为 SQLite / MySQL。

> **定位**：这是"改代码时的安全网"，不是对外验收标准。目标是让每次改动都能立刻知道变好还是变坏。

---

## 1. 目录内容

| 文件 | 作用 | 可否手改 |
|---|---|---|
| `schema.sql` | 电商数仓样例模型（5 张表）+ 口径约定 | ✅ 改模型时同步改 |
| `seed_data.py` | 生成样例数据（固定随机种子，可复现） | ✅ |
| `ecommerce.db` | SQLite 数据库，810 条订单 | ❌ 由脚本产出 |
| `golden_questions.py` | **30 道题的唯一来源**（问题 + 标准 SQL + 标签） | ✅ **改题只改这里** |
| `golden_set.jsonl` | 机器可读题库（id / question / golden_sql / tags） | ❌ 自动生成 |
| `golden_set.md` | 人读版：每题 + 标准结果 | ❌ 自动生成 |
| `baseline.json` | 标准结果基线，评测判分依据 | ❌ 自动生成 |
| `build_baseline.py` | 执行 30 题 → 产出上述三个文件 | — |
| `evaluate.py` | 结果集比对评测器 | — |

## 2. 数据模型（order 粒度）

```
fact_order ──┬─ dim_customer (客户等级/地区/新老客)
             ├─ dim_product  (品类/品牌/成本)
             ├─ dim_channel  (渠道/线下线上)
             └─ dim_date     (年/季/月/周/是否周末)
```

**时间锚点**：2024-01-01 ~ 2024-06-30，**本月** = 2024-06，**上月** = 2024-05。
（这是刻意设计的：让"本月/上月"这类相对时间问题有确定含义，可判分。）

### 口径约定（必须与语义层一致）

| 口径 | 定义 |
|---|---|
| 有效订单 | `order_status = '已支付'` |
| GMV | `SUM(amount)` —— 含优惠、未剔除退款 |
| 实收金额 | `SUM(amount - discount_amount)` —— 已支付订单净收入 |
| 客单价 | 实收金额 / 去重客户数 |
| 退款率 | 已退款订单数 / 全部订单数 |
| 毛利率 | (实收金额 − 商品成本) / 实收金额 |

> `amount` 是 SQLite/MySQL 的**生成列** = `quantity * unit_price`（成交金额，未减优惠）。
> 口径与字段的差异正是语义层要承载的东西——别在 Prompt 里口述，写进 `metrics.yml`。

## 3. 快速开始

```bash
cd golden_set
python seed_data.py         # 1) 建库 + 造数（可重复执行，结果一致）
python build_baseline.py    # 2) 执行 30 题 → 生成题库与标准结果
python evaluate.py          # 3) 自检：应输出 30/30、100%
```

## 4. 评测模型产出

准备 `predictions.jsonl`，每行一条：

```json
{"id": "A1", "sql": "SELECT SUM(amount) FROM fact_order"}
```

然后：

```bash
python evaluate.py predictions.jsonl
```

输出示例：

```
总数 30　通过 26　执行准确率 86.7%
  PASS           26
  ROW_MISMATCH    2
  ERROR           1
  MISSING         1
  FAIL [B1] ERROR: no such table: no_such_table
```

### 判分规则（Execution Accuracy）

- **PASS**：结果集一致。**行序无关、列名无关**，数值按容差 `1e-6` 比较
  （避免因 `ROUND` 位数、整数/浮点表示差异误判）
- **ROW_MISMATCH**：可执行但结果不符
- **ERROR**：执行报错
- **TIMEOUT**：超过 10s
- **MISSING**：预测缺该题

> 只比结果、不比 SQL 文本——这是 Text2SQL 的标准做法。
> 一条 SQL 写法不同但结果相同，应当算对。

## 5. 30 题覆盖情况

| 分组 | 题号 | 数量 | 考察点 |
|---|---|---|---|
| 基础过滤与聚合 | A1–A5 | 5 | 单值聚合、阈值过滤、去重计数、IN |
| 分组聚合 | B1–B6 | 6 | GROUP BY、HAVING、CASE 比率、派生指标 |
| 时间趋势 | C1–C6 | 6 | 月/周/日分桶、跨期对比、CASE 转列 |
| 多表关联 | D1–D7 | 7 | 三表 JOIN、TopN、布尔维度、复杂比率 |
| 窗口与排名 | E1–E4 | 4 | RANK / ROW_NUMBER / 累计 / PARTITION BY |
| 子查询与 CTE | F1–F2 | 2 | CTE、标量子查询、窗口总计占比 |

每条题在 `golden_questions.py` 里都带 `tags`，可按下述维度切片统计：
`AGG` / `GROUP_BY` / `HAVING` / `JOIN` / `WINDOW` / `CTE` / `TIME_BUCKET` / `CASE_WHEN` / `COUNT_DISTINCT` / `口径:*`

> 按标签分组统计准确率，比一个总数更有用——能直接看出模型/语义层在哪类问题上弱。

## 6. 多方言扩展（Hive 作为 L2）

本题库用 SQLite 语法编写，与 MySQL 8.0 差异仅两处（已在 `schema.sql` 注明）：

| SQLite | MySQL |
|---|---|
| `strftime('%Y-%m', order_date)` | `DATE_FORMAT(order_date, '%Y-%m')` |
| `TEXT` 日期类型 | `DATE` |

后续接 Hive 时，标准结果**不需要重算**：只要数据相同，结果集就相同。
做法是为每道题生成方言变体 SQL，然后 **用同一份 `baseline.json` 判分**——
这正好可以当作 sqlglot 转译能力的回归测试。

## 7. 扩充建议

- **加题**：改 `golden_questions.py` → 重跑 `build_baseline.py` → 重跑 `evaluate.py` 自检
- **加字段/表**：改 `schema.sql` + `seed_data.py` → 全流程重跑（基线会变，属正常）
- **保持真题比例**：刻意保留几道"模糊题"（如 C4 的"本月"），用来测试消歧与澄清能力
- **控制规模**：30 题够用。题量增长会拖慢迭代，价值递减

> ⚠️ `baseline.json` 是判分依据。任何改动数据或模型后**必须重跑 `build_baseline.py`**，
> 否则评测结果无意义。
