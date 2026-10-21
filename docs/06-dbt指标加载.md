# 06 dbt `metrics.yml` 加载器

这份文档记录一条早期决策的**落地证据**，而不是设计意图。

决策（`方案骨架.md` 第 3 节 C 项）：

> **兼容 dbt `metrics.yml`，不自创指标 DSL。**

决策本身很容易写，难的是证明它不是一句空话。所以这篇的重点在第 5 节：
**怎么验证"加载出来的模型"和"手写的模型"是同一个东西**。

---

## 1. 为什么是加载器而不是"另建一个语义层"

"什么是指标、口径怎么算"这件事，dbt 已经有一套被大量项目使用的格式。
我们再发明一套的代价是**双重迁移成本**：用户既要把口径从 dbt 搬过来，
以后还得考虑搬回去。

所以语义层的定位是**加载器 + 编译器**：

```
dbt metrics.yml  ──(加载器)──▶  SemanticModel  ──(规划器)──▶  LogicalPlan
                                                              │
                                                     (编译器) ▼
                                                          方言 SQL
```

加载器只做**翻译**，不做增强。它不连 dbt、不读 `manifest.json`、
不跑 `dbt parse`——只解析 YAML 文本。因此零副作用、可在任意环境跑、
便于测试（`python -m nl2sql.semantics.dbt_loader <file>`）。

---

## 2. dbt 自己有两套规范，两套都读

| | legacy（独立 metrics 文件） | latest（v1.12 / v2，模型内联） |
|---|---|---|
| 指标类型 | `calculation_method` | `type` |
| 聚合 | `sum` / `count` / `count_distinct` / `average` / `min` / `max` | `simple` + `agg` |
| 派生 | `calculation_method: derived` + `expression` | `type: derived` + `expr` |
| 比率 | 手写表达式 | `type: ratio` + `numerator` / `denominator` |
| 过滤 | `filters: [{field, operator, value}]` | `filter: "{{ Dimension(...) }} = 'x'"` |
| 自包含 | ✅ 不需要 `semantic_models` | ❌ 需要语义模型提供维度/实体 |

`golden_set/metrics.yml` 里两套都用到了（`refund_rate` 用 `type: ratio`，
其余用 `calculation_method`），验收脚本断言 `specs == ["latest", "legacy"]`。

### 两个必须做对的细节

**① 过滤是条件聚合，不是 WHERE。**

dbt 的 `filters` 语义是"只统计满足条件的行"，而不是"把不满足条件的行排除在结果外"。
写成 WHERE 会把分组里的其它行一起筛掉——数字看着像对的，其实口径已经变了。

```
sum   + 过滤 → SUM(CASE WHEN c THEN x END)
count + 过滤 → SUM(CASE WHEN c THEN 1 ELSE 0 END)
count_distinct + 过滤 → COUNT(DISTINCT CASE WHEN c THEN x END)
```

**② 别把 `SUM(SUM(x))` 包出来。**

真实文件里 `expression: SUM(x)` 配 `calculation_method: sum` 很常见。
再套一层就是 SQL 错误或语义错误。加载器检测到最外层已有对应聚合函数就
原样采用并发警告。

---

## 3. JOIN 不是我们发明的语法

dbt 用 `entities` 表达外键：

```yaml
- name: orders            # 事实表
  entities:
    - {name: order,    type: primary, expr: order_id}
    - {name: customer, type: foreign, expr: customer_id}
- name: customers         # 维表
  entities:
    - {name: customer, type: primary, expr: customer_id}
```

`foreign` 按名字配对到持有同名 `primary` 的模型，两者的 `expr` 组成等值条件：

```sql
LEFT JOIN dim_customer c ON f.customer_id = c.customer_id
```

**这条推导链就是 dbt 语义层自己的连接解析方式**（MetricFlow 也是这么做的），
所以我们不需要额外的 join 配置。改外键只改一处。

一个诚实的限制：我们的 `JoinSpec.condition` 是相对 FROM 别名写的字符串
（不是 SQL AST），表达不了"经由中间表"的多跳链。非 FROM 表上的外键会被
**记录成警告**而不是硬猜——猜错就是错误结果。

---

## 4. 兼容的边界：dbt 规范里确实没有的东西

说"兼容"就必须说清兼容到什么程度。以下四类 dbt 的指标规范里**没有对应字段**：

| 缺什么 | dbt 的做法 | 我们的做法 |
|---|---|---|
| **窗口指标**（占比、排名） | 交给 BI 层，或用 `ratio` / `cumulative` 近似 | `meta.nl2sql.window: true` |
| **同义词** | 无此概念 | `meta.nl2sql.synonyms` |
| **输出精度** | 无（由下游格式化） | `meta.nl2sql.decimals` / `ratio` |
| **派生表作为 FROM** | 另建一个 dbt model 物化掉 | `meta.nl2sql.logical_source` |

用 `meta:` 而**不是**新造顶层字段，是因为 `meta` 正是 dbt 指定的
"放你自己的东西"的地方：dbt 会原样保留并忽略它，不会报错也不会误用。
**这不是另造 DSL。**

### 可移植性报告

代价是这些字段**在 dbt 里不生效**。所以加载器把扩展分成两档，分开计数：

| 档 | 键 | 拿回 dbt 会怎样 |
|---|---|---|
| **标注类** | `synonyms` / `decimals` / `ratio` | 数字仍然对，只是不匹配中文问法、不四舍五入 |
| **结构类** | `window` / `select` / `table` / `logical_*` | **跑不出同样的数**，需要在 dbt 里另建模型 |

```
口径可移植性 86%　原生 0 / 仅标注 12 / 用结构扩展 2
  dbt 里没有等价物（结构扩展）: revenue_share, revenue_rank
```

界面上（`/api/info` 的 `semantic` 字段）也能看到同一份报告。
**说明白"哪 14% 搬不走"，比声称 100% 兼容有价值。**

---

## 5. 验收：怎么证明它没丢信息

`tests/test_s3b_dbt_loader.py` 的 B 段是核心。做法是把黄金语义模型
用 dbt 语法**重述一遍**（`golden_set/metrics.yml`），然后与手写的
`golden_model.build_model()` 逐项比对。

只看执行结果不够——"计划不同但恰好结果相同"是存在的（比如漏了一个恒真过滤），
那种模型换个问句就错。所以比三层：

| 层 | 断言 | 结果 |
|---|---|---|
| 结构 | 指标集合 / 维度集合 / `logical_source` / JOIN（含别名与条件）/ 同义词 | 全部一致 |
| 表达式 | 每个指标与维度的 `expr` **逐字**相同；`window`/`decimals`/`ratio`/`table` 一致 | 0 处差异 |
| **30 题** | 逻辑计划逐题相同 → 编译出的 SQL **文本**逐题相同 → 结果集逐题相同 | 0 / 0 / 0 |
| 异常 | 规则规划器答不出的题，两边**抛同样的错**（类型与消息） | 一致 |

SQL 文本完全相同是个很强的结论：说明加载器**没有丢失任何进入编译的信息**。
（手写模型与翻译结果本来是两种合法写法，文本能一致是因为加载器把
`columns:` 里声明的列名补上了表别名，而不是靠猜。）

另外还有两段：
* A 段覆盖加载器本身——两种规范、jinja 翻译、错误可行动、
  YAML 陷阱（流式写法漏引号会把键拆开）、多文件合并
* D 段覆盖 HTTP 面——壳层注入 `NL2SQL_METRICS` 后 `/api/info` 如实报告来源

### 端到端复核（含守门 / 缓存 / 校验 / 结果组装）

B 段比的是规划器 + 编译器。整条链路还包着守门、缓存、SQL 校验、
结果组装，那些环节也可能对模型有隐含假设。所以再跑一遍完整引擎：

```bash
python scripts/compare_planners.py --planners rule --metrics golden_set/metrics.yml
python scripts/compare_planners.py --planners rule          # 对照：内置参考模型
```

| 语义层来源 | 规则规划器准确率 | 失败的题 |
|---|---|---|
| `golden_set/metrics.yml` | **17/30 = 56.7%** | A3 B5 B6 C3 C4 C5 C6 D3 D4 E2 E3 E4 F1 |
| 内置参考模型 | **17/30 = 56.7%** | A3 B5 B6 C3 C4 C5 C6 D3 D4 E2 E3 E4 F1 |

**失败题目集合逐个相同**（只有毫秒数不同）。这是比"准确率数字相同"
更强的结论：连"哪些题答不出"都一致，说明换成语义文件后整条链路的行为
没有发生任何可观测的改变。

运行：

```bash
python tests/test_s3b_dbt_loader.py
python -m nl2sql.semantics.dbt_loader golden_set/metrics.yml --show-sql
```

---

## 6. 写这个加载器时踩到的真问题

都不算"架构问题"，但每一个都会让用户看到错误数字或无从下手。

**① legacy `calculation_method` + latest `filter:` 混写时过滤被静默丢掉。**
真实文件里两套写法混用很常见。只认 `filters:` 的后果是条件被忽略——
指标数字变大，而且**不报错**。这是整个模块里最危险的一个 bug。
现在两种写法都读。

**② `f.name` 没补别名。** dbt 的维度 `expr` 是裸列名，我们的编译器按
`别名.列` 拼。多表 JOIN 时裸列名会歧义。靠什么知道哪些标识符是列？
**靠 dbt 模型 YAML 里的 `columns:`**——这正是它存在的意义：不连库也能
知道表结构。没声明 `columns:` 时**不猜**（猜错会生成查不动的 SQL），
改发提示让作者补上。

**③ YAML 流式写法漏引号会静默半解析。**
`dimensions: [{name: month, expr: substr(order_date, 1, 7)}]` 里的逗号是
YAML 分隔符，会被拆成 `expr: substr(order_date` 加一个新键 `1`。
用户以为表达式生效了，其实没有。现在对维度/实体/指标做**不认识的键**检查
并报出来（警告而非报错：dbt 会持续加字段，硬拒绝会让新版本文件加载不了）。

**④ 指标文件写错时界面只显示"核心引擎未就绪"。**
`/api/info` 原本用 `Depends(get_engine)`，引擎装配失败就直接 503，
用户看不到任何原因——而 `main.py` 的 lifespan 明明把失败原因记进了
`holder.warnings`，只是没人读。现在 `/api/info` 直接读 `app.state`，
失败时返回 200 + `engine_ready: false` + `startup_warnings`。
（这条与文档开头"引擎未就绪时接口仍可用"的声明原本是矛盾的，属于
**文档说了一件事、代码做了另一件**，现在一致了。）

---

## 7. 已知边界

| 边界 | 原因 | 影响 |
|---|---|---|
| 不支持 `type: cumulative` | 需要 time spine（时间骨架表）才能确定每个时间点的取值 | 拒绝并提示改用普通指标 + 时间范围 |
| 不支持 `type: conversion` | 需要实体级事件配对，属于行为分析 | 拒绝并建议在数仓里预建转化表 |
| 不读 `manifest.json` / 不跑 `dbt parse` | 会引入 dbt 运行时依赖 | `ref()` 只做字符串解析，不做跨项目解析 |
| 多跳外键不自动建 JOIN | `JoinSpec.condition` 是相对 FROM 别名的字符串 | 警告 + 指向 `meta.nl2sql.joins` |
| `timestamp` / `time_grains` 不进入模型 | 时间粒度由 `TimeRange` 决定 | 只报一次汇总提示 |
| 窗口指标不可移植 | dbt 规范里没有"在聚合结果上再开窗"的表达 | 报告里单独标出 |

---

## 8. 怎么用

### 命令行（写完文件立刻验证）

```bash
python -m nl2sql.semantics.dbt_loader path/to/metrics.yml --show-sql
```

`--show-sql` 会把每个指标展开成纯 SQL——**写指标文件时最有用的一个开关**：
不用启动服务就能看出口径对不对。

### 代码

```python
from nl2sql import NL2SQLConfig

cfg = NL2SQLConfig(state_dir="data")
cfg.add_sqlite("shop.db", source_id="shop", semantic_path="metrics.yml")
```

或走环境变量（壳层注入，核心不读环境变量）：

```bash
NL2SQL_METRICS=golden_set/metrics.yml uvicorn apps.web.backend.main:app
```

不设 `semantic_path` / `NL2SQL_METRICS` 时用内置的黄金集参考模型
（黄金集是评测基准，30 题基线建立在它上面）。
**指向不存在的文件会报错**，不会静默退回参考模型——静默退化会让用户
以为自己的口径生效了。
