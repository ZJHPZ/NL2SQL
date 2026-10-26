# talk-to-sql

自然语言查询数仓：**多数据源 + 可自定义 RAG 智库 + 可插拔智能体组件**。

> 定位：**小而精，面向个人开发者**，优化个人工作场景。
> 交付物：**SQL + 结果表**（不做长篇分析讲解）。

---

## 当前进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| 分析 | 需求、取舍、决策记录 | ✅ 见 `方案骨架.md` |
| 分析 | 题库评估（3,378 题） | ✅ 见 `题库分析.md` |
| 评测 | 30 题黄金集 + 结果集比对评测器 | ✅ 见 `golden_set/` |
| S1 | 契约层 + 配置 + 注册表 + 门面 + 层检查 | ✅ |
| S2 | 存储 + SQLite 适配器 + 元数据目录 | ✅ |
| S3 | 方言编译 + 守门 + 缓存 | ✅ |
| **S3b** | **dbt `metrics.yml` 加载器（语义层不再是手写的）** | ✅ 见 `docs/06` |
| **S3c** | **语义层表达能力：累计 / HAVING / 跨期对比 / 分组内条件筛选** | ✅ 见 `docs/07` |
| S4 | 语义编排 + 可插拔规划器 | ✅ |
| S5 | Web 工作台（FastAPI + React/Vite） | ✅ |
| S6 | 知识库 + 验证式混合 | ✅ |
| **T1–T7** | **升级为智能体：工具循环 + 插件体系** | ✅ |
| **P0** | **插件与工具管理（看得见 / 管得了 / 配得了）** | ✅ |
| **P0b** | **技能机制（`SKILL.md`）：发现 / 报告 / `skill.load` / 界面第四块** | ✅ 见 `docs/08` |
| **P1** | **项目 playbook 技能 3 份 + 引导注入 + 决策场景集（选择 11/12；正确率差值在噪声内）** | ✅ 见 `docs/08` §13 |
| **M1** | **第二、三个引擎：MySQL + PostgreSQL 适配器 + 跨引擎等价验收** | ✅ 见 `docs/11`（PostgreSQL 为 2026-10 补做，§10） |
| **M2** | **LangGraph 执行器 + SSE 流式（30 题录制—重放 30/30 一致）** | ✅ 见 `docs/12` |
| **M3** | **语义层两个原语：时间粒度 + 标量比较（黄金集 29/30）** | ✅ 见 `docs/13` |
| **M4** | **实验：覆盖率 / 口径漂移 / 消融 / 留出集（rule 13/20；llm 12/20）** | ✅ 见 `docs/14` |
| **M5** | **观测与失败归因：统计接口 + 九态可读文案（真实试用 0/20 未开始）** | ✅ 见 `docs/15` |
| **M7** | **数据开发工作台：多项目 + 可视化数据源（JDBC 泛用桥）+ SQL 任务（生成/修改/版本/回滚，不写业务库）** | ✅ 见 `docs/16` |
| S7–S9 | SDK / 多方言 / 开题后的其余引擎 | ⬜ |

### 插件与工具管理

界面上「插件与工具」页签解决三件事：

| 需求 | 实现 |
|---|---|
| **看得见** | 插件清单（版本/能力/加载错误）+ **磁盘上可用但未启用**的插件 + 工具全集（含**未注册的**，带来源、风险、未注册原因）+ 能力清单 |
| **管得了** | 逐个启用/停用**插件**（写白名单→重装工具集）与**工具**，落盘到配置；停用主力工具时给退化警告 |
| **配得了** | 循环参数、能力授予、配置文件原文——**改完热生效，无需重启** |

**外部插件默认不加载**（`enabled_plugins = ()`）：插件是可执行代码，显式点名才运行。
界面上把"未启用"的插件也列出来并提供「启用」按钮——否则默认配置下的用户
看到的是一片空白，无从知道磁盘上有什么可以开。

**配置放在文件**（`config/nl2sql.toml`）而不是数据库：个人工具的用户就是开发者，
配置文件他能看懂、能直接改、能进 git。文件不存在时一切按代码默认运行。

#### 一个被推翻的设计决定

我最初坚持"界面不能改能力"，理由是能力白名单是安全边界。
**但这条被推翻了**——面向个人工作者，用户就是自己数据的主人，替他做决定是越权。

推翻之后补上了三件事，让自由度是**可控**的：

1. 每项能力标注 `risk` 与 `unlocks`（解锁什么），元数据在后端不在前端硬编码
2. 界面摆出「**还有几道防线**」：guard 的只读状态、是否存在写工具
3. 授予高危能力给提示、收回能力提示受影响工具

> 关键诚实点：**授予 `source.write` 不会立即解锁写操作**——
> 当前没有实现写操作的工具，且 guard 的只读守门独立于此。
> 用户点"授予"时应该知道**自己实际改动了什么**。

**两件明确不做**：运行时热插拔插件代码（Python 卸载模块有诡异问题）、
插件市场（那是产品化）。界面上明说"新增插件需重启"，不假装热插拔。

详见 **`docs/04-插件与工具管理.md`**。

### 智能体与插件（本次升级的重点）

从「LLM 单次调用 + 硬编码流水线」升级为真正的智能体。分野只有一句话：

> **工具执行的结果要回到模型眼前，模型据此决定下一步。**

有了这条，才可能出现：SQL 报错 → 模型看到 `no such column` 与候选字段 →
决定先查表结构 → 用正确字段重跑。

| 能力 | 说明 |
|---|---|
| agent 循环 | 模型每步自主选工具；失败信息回灌；防打转 + 步数上限 |
| 工具集 | 11 个（9 内置 + 2 示例插件），含 `schema.describe` 的**取值域采样** |
| 主动追问 | `clarify.ask` 回合制：返回 `status="need_clarification"`，会话跨请求保留 |
| 插件体系 | `plugins/<name>/plugin.toml` + `plugin.py`；**能力必须显式声明** |
| 隔离 | 插件只能 import `nl2sql.plugins`，由层检查强制（规则 `PLUGIN_IMPORT`） |

**两道运行时防线**（实测暴露的真实故障，光靠 Prompt 不够）：
1. 没执行过查询就不许给数据类回答——模型会"说明口径"甚至编出带 `—` 的表格
2. 过程叙述（"我先去查一下"）不算最终回答——它会掩盖"其实没查"

实测（真实模型）：清晰问题 3/3 都**真的查到数据**并给出正确数值；
缺时间范围的问题**主动追问**，用户回答后跨回合正确接续。

追问的边界（2026-10 打真模型后收紧，T7/T8 复跑通过）：问**指标的具体数值**
（"销售额是多少"）却没给时间范围时，先追问时间范围、**不默认全时段**——
时间范围不同金额能差几倍，是猜错代价最高的一类信息；比较/排名类
（"哪个渠道表现最好"）仍用默认口径作答并披露标准；预测类
（"预测下个季度"）明确说明无法预测，**不编造数字**。规则写死在
`nl2sql/agent/machine.py` 的 `DECISION_PROMPT`，不靠模型临场裁量。

**散文结论 + 结构化结果表同时返回**：agent 的回答是自然语言（给判断），
同时把工具拿到的数据提升为 `QueryOutcome.result`（给可排序、可导出的明细）。
刻意**不解析散文还原表格**——结构化数据本来就在工具返回值里，只是之前没往下传。

设计细节与实测数据见 **`docs/03-智能体与插件架构.md`**。
加新工具照着 `plugins/demo/` 抄即可。

> agent 模式**默认开启**（2026-10 起）：`AgentConfig.planner` 默认 `"agent"`，
> 有模型就走智能体，无模型自动回落线性管线（不是报错）。代价是每次多 1–2 次
> 模型调用、耗时从毫秒级变秒级——要最快最确定的路径可显式设
> `planner="rule"/"hybrid"`（Web 端对应 `NL2SQL_PLANNER`）。
>
> 相对时间（「上个月」「本月」）必须有一个"现在"做锚点：`ask()` 缺省用服务端
> 当天（`date.today()`），评测/回放应显式传 `now=`，否则锚点缺失会退化成
> 全时段查询——这是 2026-10 修掉的一个 **quietly wrong**（状态仍是 ok）。

**S3 验收**：手写逻辑计划跑通黄金集 —— 编译 30/30、守门 30/30、结果与基线一致 30/30。
即**执行链路已先于 LLM 被验证**。

**S4 / S6 验收**：端到端问答可用。四种模式实测（黄金集 30 题，同一 Key）：

| 模式 | 严格正确 | 模型调用 | 定位 |
|---|---|---|---|
| `rule`（无模型） | 25/30 = 83.3% | 0 | 零成本零延迟，**全部确定性** |
| `hybrid` | 25/30 = 83.3% | **~1** | **默认**：并列最高分 + 最低代价，29/30 确定性 |
| `hybrid_verified` | 25/30 = 83.3% | ~30 | 并列但贵 30 倍，且引入不确定性 |
| `llm`（DeepSeek） | 23/30 = 76.7% | 30 | 准确率**最低**、最贵、不确定 |

> **上表是 S3c 语义层扩容后重测的**（`docs/07`）。扩容前是
> rule 17/30、llm 18/30 —— 那时"llm 更准"，所以默认是 `llm`。
> 现在规则规划器反超，默认已改为 `hybrid`。
> 这不是模型变差了，是**语义层补上了它原先表达不了的 7 类问法**。

> 上表用**内置参考模型**测得。换成 `golden_set/metrics.yml`（dbt 语法）
> 后数字与失败题目集合完全相同，见下节。
>
> llm 的数字有**抽样波动**（不同次运行在 23–24/30 之间，本次 s4b 单次为
> 24/30）：DeepSeek 在 temperature=0 下仍 run-to-run 非确定，见 `docs/14` §1.3。

**知识库机制完备**（检索/版本/停用/测试检索/自动生成），但实测
**few-shot 贡献 0 题**（留出集 7/13 → 7/13），**验证式混合不划算**。
两个负结果的完整分析与下一步方向见 `docs/02` 第 19 节。

> **结论**：瓶颈在**语义层表达能力**，不在规划器。
> 这个结论指的是"能表达多少种问法"，不是"语义层写在文件里还是代码里"
> ——后者已由 S3b 解决（加载器 + 等价性验收）。
>
> **S3c 已按这个结论扩容**（累计、HAVING、跨期对比、分组内条件筛选）：
> 真实语义计划 21/30 → **28/30**，规则规划器 17/30 → **25/30**。
> 8 道题的提升**没有一道**来自调 Prompt 或加示例。见 `docs/07`。

### 语义层：dbt `metrics.yml` 加载器（S3b）

**语义层不再是一份手写代码。** 早期决策是"兼容 dbt `metrics.yml`、
不自创指标 DSL"，S3b 把它落了地：

```bash
# 写完指标文件立刻验证（最有用的一个开关：把口径展开成纯 SQL）
python -m nl2sql.semantics.dbt_loader golden_set/metrics.yml --show-sql
```

| 能力 | 说明 |
|---|---|
| 两套规范 | legacy `calculation_method` + latest `type`（`simple`/`derived`/`ratio`） |
| JOIN 自动推导 | 由 dbt 的 `entities` 外键配对生成，不另写 join 配置 |
| 过滤语义 | `filters` → **条件聚合**（不是 WHERE），否则口径会静默改变 |
| 扩展边界 | dbt 没有的（窗口指标、同义词、精度、派生表 FROM）走 `meta.nl2sql.*` |
| **可移植性报告** | 区分「标注类」与「结构类」扩展，给出"口径可移植性 86%" |

**怎么证明这不是一句空话**：`golden_set/metrics.yml` 用 dbt 语法重述了
黄金语义模型，`tests/test_s3b_dbt_loader.py` 断言它与手写参考模型
**计划 / 编译出的 SQL 文本 / 结果集三层、30 题逐题一致**（0 处差异）。
文本能完全一致说明加载器**没有丢失任何进入编译的信息**。

端到端复核（含守门 / 缓存 / 校验）：

```bash
python scripts/compare_planners.py --planners rule --metrics golden_set/metrics.yml
```

两种语义层来源都是 **17/30 = 56.7%**，且**失败的题目集合逐个相同**
（A3 B5 B6 C3 C4 C5 C6 D3 D4 E2 E3 E4 F1）——连"哪些题答不出"都一致。

`cumulative` / `conversion` 两类指标**明确拒绝**并说明原因与替代做法——
近似一个累计指标会给出错误数字，比报错糟得多。详见
**`docs/06-dbt指标加载.md`**。

### 语义层表达能力（S3c）

原先语义层只能表达"分组 + 聚合 + 排序 + 取前 N"。一旦问题要求在
**聚合结果之上**再做点什么，就只能走 `raw_select` 逃生通道——
能跑，但语义层没真正表达它，模型规划器也就学不会。

补上四项能力（都是**计划层构造**，不是新指标类型）：

| 能力 | 语义 | 写错会怎样 |
|---|---|---|
| **HAVING** | 过滤**分组**（"实收金额超过2万的品类"） | 与行级过滤混淆 → 口径静默改变 |
| **累计** | 按分组维度开窗滚动求和 | 引用的维度不在分组里 → 外层查不到该列 |
| **跨期对比** | 两个时间窗透视成两列 | 与 `time_range` 叠加 → 某一列恒为 0，**不报错** |
| **分组内条件筛选** | `PARTITION BY` 排名 + `qualify` 前 N | 用 `having` → 排名还不存在，取不到数 |

一个机制覆盖两类问法：**透视**同时实现"跨期对比"（桶 = 时间段）
与"分类转列"（桶 = 维度取值）。

```bash
python tests/test_s3c_expressiveness.py
```

| | 动手前 | 动手后 |
|---|---|---|
| 真实语义计划 | 21/30 | **28/30** |
| 规则规划器准确率 | 17/30 = 56.7% | **25/30 = 83.3%** |

+8 题**没有一道**来自调 Prompt 或加示例。剩下 2 题走逃生通道并明确记录了原因
（ISO 周分桶属于方言细节、与全局平均值比较需要另一类比较能力）。
详见 **`docs/07-语义层表达能力.md`**。

> **默认规划器随之改了**：`auto` 从 `llm` 改为 `hybrid`。
> 原因是扩容后规则规划器**反超**了模型（25/30 vs 23/30），
> 而 hybrid 用约 1 次调用拿到并列最高分。
> 这是"语义层表达能力是瓶颈"这个判断的直接后果——
> 补上表达能力之后，确定性规划器的覆盖面自然就上来了。

> **模型接入**：设置环境变量 `NL2SQL_LLM_API_KEY` / `NL2SQL_LLM_BASE_URL` /
> `NL2SQL_LLM_MODEL`，由壳层读取后显式注入。默认 DeepSeek（OpenAI 兼容协议，
> 换厂商只改两行）。自检：`python scripts/check_llm.py`。
> **API Key 不要写进代码、文件或对话**；明文传输过的 Key 应视为已泄露并轮换。

> **换数据库引擎只改一个环境变量**：`NL2SQL_ADAPTER=mysql` +
> `NL2SQL_MYSQL_HOST/PORT/USER/DATABASE/PASSWORD`（口令只从环境读）。
> 上层（规划器 / 编译器 / 守门 / 缓存）一行都不用动——这正是 M1 要证明的事。
> 数据准备两步：`python scripts/mysql_load_golden.py`（灌黄金集，幂等）与
> `python scripts/mysql_create_ro_user.py`（建只读账号，口令随机生成并只写 `.env`）。
> 第三引擎同理：`NL2SQL_ADAPTER=postgres` +
> `NL2SQL_PG_HOST/PORT/USER/DATABASE/PASSWORD`，验收 `python tests/test_pg_engine.py`
> （自带 `embedded-postgres` 二进制时不需要装服务；golden 数据由
> `scripts/pg_load_golden.py` 从 SQLite 原样搬）。
> 详见 **`docs/11-MySQL适配器与跨引擎等价.md`**（MySQL §1–§9，PostgreSQL §10）。

> **执行器可切换**：`NL2SQL_RUNTIME=auto / loop / langgraph`（默认 `auto`，
> 装了 langgraph 即用图执行器）。两者**行为等价**（6 个场景逐字段一致，
> 含事件序列），界面顶栏会显示当前执行器；请求图执行器但没装时**降级并留痕**
> （`/api/info` 的 `runtime.note` + 界面"已降级"标记），不会静默换掉。
> 流式：`GET /api/query/stream`（SSE，首字节实测 <0.5s）。
> 详见 **`docs/12-LangGraph执行器与等价性.md`**。

> **语义层补了两个原语**（M3）：**时间粒度 `grain`**（维度只声明"这是哪个桶"，
> 截断由编译器按 sqlite `strftime` / mysql `DATE_FORMAT` 展开，`month`/`day`
> 也从 `substr` 迁了过来）与 **标量比较 `ScalarCompare(measure, against, op,
> factor)`**（`against` 显式区分"整表平均"与"当前结果集平均"——选错会安静地
> 返回一组合理答案）。规则规划器黄金集 **25/30 → 29/30 = 96.7%**，唯一失败
> D3 是黄金集题面与 SQL 口径不一致；顺带把 M1 的 C3 跨引擎缺口**正确**闭合
> （`tests/test_m1_mysql.py` C 节 29+1 → **30/30**）。
> 留出集 20 题在 `golden_set/heldout.json`（**开发期不跑**，M4 一次性跑完 13/20）。
> 详见 **`docs/13-语义表达力.md`**。

> **四个实验跑完**（M4，`docs/14` 有完整表格与复现命令）：
> **① 覆盖率扫描**——语义模型按指标删减 8 档，规则规划器实测与"语义天花板"
> 的差**恒为 0–1 题**（那 1 题是已知的 D3 口径不一致），三档 29/24/20；
> 由此**推翻原预判**："缺覆盖时规则规划器会 `UNRESOLVED` 老实认输"是错的，
> 中配档 6 个失败全是 `status=ok`——**失败不可见是覆盖率不足本身的性质**。
> **② 口径漂移**——13 个常见指标，绕开语义层手写 SQL 的偏差 13/13 > 5%，
> 中位 **22.5%**，最大 66.5 倍。**③ 消融**——缓存暖路径快 28%；知识库对规则
> 规划器 0 差异（它不读知识库，如实记录）；守门改写 28/30 条 SQL 的 `LIMIT`
> 但零准确率影响；窗口指标是独立能力项（去掉后 5 题必错）。
> **④ 留出集**（开发期唯一一次）：**13/20 = 65.0%**，7 个失败全部 `status=ok`；
> 与黄金集 96.7% 的差距说明**开发集数字不能外推**。
> **模型（llm）档已补跑**：覆盖率三档 23/20/17（rule 29/24/20）、留出集 12/20
> ——**换规划器没有提高上限**，反而多出 `EXEC_ERROR`/`COMPILE_FAILED` 一类
> "没产出结果"的失败；模型输出 run-to-run 非确定（同 Prompt 两次回复可能不同），
> 数字只作量级参考。曲线：`docs/assets/coverage_curve.png`；
> 详见 **`docs/14-实验.md`**。

> **观测与失败归因**（M5）：`GET /api/admin/stats` 给出问答次数 / 九态分布 /
> 成功率 / 失败率 / 缓存命中 / 延迟 P50·P95 / 工具失败率 / 最近失败列表；
> 数据来自 append-only 的 `query_log` / `tool_log`（**不复用 `history`**——
> 回放契约与可索引的观测列会互相绑架）。`QueryStatus` 九种状态在界面上各有
> "为什么 + 怎么办"的可读文案（追问单独样式，**不算失败**）；管理页「使用
> 统计」标签页可按 1/7/30/90 天回看。契约测试 87 项离线可跑（九态同步 /
> 聚合口径 / 两执行器工具上报 / 管理端点）。
> **真实试用仍是 0/20 条未开始**（模板与判定标准在 `docs/15` §5）——
> 观测是让试用能被记录、被归因的基础设施，不是试用本身。
> 详见 **`docs/15-观测与试用记录.md`**。

> **agent 容错与上下文预算**（借 langchain 官方实现思路落地，不搬框架）：
> ①**模型调用重试**——只重试瞬态故障（超时/连接重置/429/5xx），401/400/安全类一次失败，
> 默认 2 次、0.5s 起 ×2 退避、8s 封顶、±25% 抖动；每次重试写 `trace["llm_retries"]`
> + SSE `model_retry` 事件。SDK 自带 retries 只作底层兜底，不让两层互相放大。
> ②**决策修复**——输出被长度截断/空回复/坏 JSON 时，把"错在哪、怎么改"写进
> 下一轮消息重问一次（纯文本回答刻意不修，它本来就是可接受的 final）。
> ③**工具调用重试**——**只对只读工具、只对瞬态异常**（`database is locked`/断连），
> 写工具可能不幂等不重试，SQL 确定性失败原样回灌给模型改。
> ④**上下文预算**——Prompt 历史（14000 字符，保留最近 3 条观察）与会话渲染
> （4000 字符、单行 600）按预算裁旧内容，**不引入摘要模型**（多一次调用、
> 引入非确定性；结构化历史按预算裁更便宜可复现）。压缩只发生在渲染层，
> 持久化的完整历史不受影响。离线验收 `tests/test_agent_resilience.py` 74 项。
> 详见 **`docs/03` §14**。

实施中发现的问题记录在六处文档：
- S2 两个静默失效坑（SQLite 只读 URI、FTS5 中文分词）→ `docs/02` 第 16 节
- S3 编译器五个坑 → `docs/02` 第 17 节
- S4 规划器八个静默错误 + 双规划器实测 + 混合策略缺口 → `docs/02` 第 18 节
- S6 两个负结果（few-shot 贡献 0、验证式混合不划算）与瓶颈定位 → `docs/02` 第 19 节
- S3b 四个加载器坑（过滤被静默丢掉、裸列名未补别名、YAML 半解析、
  引擎装配失败时界面只显示"未就绪"）→ `docs/06` 第 6 节
- S3c 四处"聚合之上"的坑（行级阈值被误判成分组阈值、桶条件与
  time_range 叠加导致某列恒为 0、组内前N 被同时当成整表 LIMIT、
  "新客"/"新客户"被当成两个类别）+ 一个潜伏的 `PlanDimension.name` 崩溃
  → `docs/07` 第 2/4/5 节
- M1 两处判分器 bug（比错列、丢 NULL）与跨引擎验收带出的其余坑
  （只读账号把 1305 掩盖成 1142、`%W` 与 `%v` 语义不同）
  → `docs/11` 第 4/6 节
- M1 后补第三引擎 PostgreSQL 的坑：`pg_ctl start` 在 Windows 受限令牌下报
  `could not create restricted token: error code 87`（直接跑 `postgres.exe` 绕开）；
  本沙箱里后端进程间信号被拦，`statement_timeout` 与 `pg_cancel_backend` 都不生效
  （`SHOW` 显示 200ms、`pg_sleep` 照跑），超时翻译改用 SQLSTATE 57014 验证；
  周口径要对齐 SQLite `%Y-%W` 而非 ISO 周年、`TO_CHAR` 没有 `'Q'` 模式
  → `docs/11` §10
- M2 两个"代码看起来对、数字不对"的坑（LangGraph 只给**注解**为 `RunnableConfig`
  的参数注入 config；Starlette 的 TestClient 会把 SSE 响应整个缓冲完，
  首字节必须用真 uvicorn 量）+ 三个设计坑（`emit` 参数名与载荷撞车、
  事件接收器不能挂在共享 core 上、空 TypedDict 状态变 None）
  + 打真模型打出的两个新问题：① 模型在 temperature=0 下仍 run-to-run
  非确定（请求 deepseek-chat、响应 deepseek-flash），所以 30 题硬验收改为
  **录制—重放**（loop 录 Prompt+回复 → langgraph 重放并断言 Prompt 逐字
  一致，30/30 通过）；② 递归预算公式 `2*max_steps+5`=21 不够（8 轮要 26），
  改 `3*max_steps+5` 并把 `RecursionError` 兜底成 `RECURSION_LIMIT` 业务结果
  → `docs/12` §3.2
- M3 一个"只在问过别的题之后才出现"的坑（规划器会向共享语义模型注册条件聚合
  指标，短 label"订单数"子串命中"全部订单数"，B6 从 2 列变 4 列；单题测试
  永远绿）+ 三个语义层细节（`grain` 的 SELECT/GROUP BY/ORDER BY/HAVING/WHERE
  必须走同一出口、周桶 `%u` 与 `%v` 语义不同、`pivot+having/qualify` 仍未解开）
  → `docs/13` 第 2/4/7 节
- M4 两个"预判被数据推翻"的坑：① 设计实验时以为缺覆盖会 `UNRESOLVED`，
  实测规则规划器也大多"跑通但答案不对"（部分匹配总能落到剩余指标上）；
  ② 留出集里"第二季度每月"退化成上半年 6 个月（季度解析缺口），
  但 7 个失败中 6 个行数完全正确、只有数字错——**失败不可见**贯穿始终。
  另有工程坑：本机没装 matplotlib（图用 Pillow 画）、`read_image` 在本模型
  不可用（PNG 只能程序化校验）→ `docs/14` 第 2/5/8 节
- M5 两个"看起来是小功能"的取舍：① 统计**没有复用** `history` 表
  （它要回放，统计要可索引的观测列，混表会互相绑架）；② 工具调用统计
  **只在 agent 模式**有数据（线性管线不经过工具循环，天然为 0）——不写清
  会被当成 bug。口径坑：`need_clarification` 不算失败（否则主动澄清看起来
  像出错）、`empty`/`truncated` 算成功（数据事实不是系统失败）
  → `docs/15` 第 1/2/3 节
- T7/T8 一个"同一能力两次运行两个结果"的 Prompt 边界坑：问「销售额是多少？」——
  一次追问时间范围、一次直接给全时段合计。模型没变，是 Prompt 边界没写清
  （「完全无法判断才追问」和「犹豫时选择作答」放在一起，模型自己临场裁量）。
  修法是把边界写死：指标数值缺时间范围必须追问；比较/排名默认口径作答并披露；
  预测类不编造。真模型 T7/T8 连续两次复跑通过 → `docs/05` ⑥ 后续收紧
- (b) 三个"不做"比"做"更重要的取舍：①**不照搬 langchain 的 middleware 管线**——
  本项目是显式状态机 + 两执行器，护栏位置一目了然，为三个策略加一层钩子适配
  收益为负，只把策略搬进 `resilience.py`/`context.py`；②**写工具不重试**——
  重试写工具等于多写一次，幂等性没保证就不该自动重试；③**不引入摘要模型**——
  多一次模型调用 + 引入非确定性，比按字符预算裁旧观察贵。另：SDK 自带 retries
  是黑盒（重试了没、为什么都看不见），观测只能靠自己在 `_ask_model`/`invoke` 里
  分类重试 → `docs/03` §14

---

## 仓库结构

```
talk-to-sql/
├── nl2sql/              ★ 核心引擎（可独立 pip install，无 Web 依赖）
│   ├── contracts/       接口与数据模型（仅标准库）
│   │   ├── tool.py      工具契约 + 能力清单（智能体的手脚与权限）
│   │   └── session.py   会话契约（回合制澄清需要）
│   ├── storage/         元数据 / 知识 / 缓存 / 会话 / 观测账本（默认全 SQLite）
│   ├── sources/         数据源适配器（SQLite / MySQL / PostgreSQL / JDBC 泛用桥，共用同一套能力声明）
│   │   ├── jdbc.py      JDBC 适配器：懒启动 Java 桥、复用同一进程、limit+1 探测截断
│   │   ├── jdbc_bridge/ Java 17 长驻桥（stdin/stdout JSON；只读/多语句/锁定读在 Java 侧再校验）
│   │   └── drivers.py   JDBC 驱动注册表（固定版本 + Maven Central 下载 + 状态报告）
│   ├── dialect/         按方言能力声明做确定性编译（sqlglot 仅作可选体检）
│   ├── guard/           执行前确定性安全校验
│   ├── catalog/         元数据快照
│   ├── semantics/       **dbt metrics.yml 加载器** → 语义模型 → 逻辑计划
│   │   ├── dbt_loader.py 两种 dbt 规范 + entities 推导 JOIN + 可移植性报告
│   │   ├── golden_model.py 黄金集**参考模型**（评测基准，非本体）
│   │   └── golden_plans.py 黄金集手写逻辑计划（S3 验收桥接，含新能力的用法示范）
│   ├── knowledge/       知识条目 / 版本 / 多路召回
│   ├── cache/           结果缓存（逻辑计划指纹）
│   ├── plugins/         **插件加载 + 对外稳定接口 ToolRegistry**
│   ├── skills/          **技能加载（SKILL.md → 只读目录 + 加载报告）**
│   ├── agent/           **智能体：单步状态机 + 两个执行器**
│   │   ├── machine.py   单步状态机（决策解析、7 道护栏、结果挑选、收尾）
│   │   ├── resilience.py 重试策略（异常分类 + 指数退避/抖动）
│   │   ├── context.py   Prompt 历史与会话渲染的预算压缩
│   │   ├── runtime.py   执行器 A：while 循环（等价性基准，零第三方依赖）
│   │   ├── graph.py     执行器 B：GraphSpec → LangGraph StateGraph（通用编译器）
│   │   ├── factory.py   按 `NL2SQL_RUNTIME` 选择，降级留痕
│   │   └── builtin.py   9 个内置工具 + 条件注册的 skill.load / project.info / task.*
│   ├── tasks.py         **M7 任务库**（独立 SQLite：任务 + 版本历史 + AI 修改留痕；只读单语句校验）
│   ├── config.py        配置（演示 pydantic-free 的显式配置）
│   ├── registry.py      组件注册表（实例级，非全局）
│   └── api.py           门面 `NL2SQL`
│
├── plugins/             **用户插件目录（一切皆插件）**
│   └── demo/            示例插件：数据质量探查工具（可当模板抄）
│
├── skills/              **技能目录（声明式档：只写文本，不写代码）**
│   ├── cross-period-comparison/      跨期对比（两个时间窗并排取数）
│   ├── having-vs-group-filter/       组级条件（HAVING vs 行级 WHERE）
│   ├── unverified-metric-disclosure/ 口径未注册的指标：披露而不是编
│   └── README.md        格式说明（P0 通机制，P1 放入项目 playbook）
│
├── apps/                三个壳：只做适配，不含业务逻辑
│   ├── web/             壳① 本地 Web 工作台（首发，已完成）
│   │   ├── backend/     FastAPI：装配 + 序列化 + 静态托管
│   │   │   ├── projects.py  **M7 项目中心**（ProjectStore + ProjectHub：每项目一个引擎）
│   │   │   └── routers/      info/query/knowledge/admin + **projects/tasks**
│   │   └── frontend/    React + Vite（TS）：问答 / 项目 / 任务 三个视图
│   ├── sdk/             壳② 对外 Python SDK
│   └── internal/        壳③ 内部工具
│
├── golden_set/          黄金评测集（30 题 + 判分器）
│   ├── metrics.yml      黄金语义模型的 **dbt 语法版本**（等价性验收物）
│   ├── schema_postgres.sql  PostgreSQL 版 DDL（第三引擎验收用，数值/生成列按 PG 类型）
│   └── heldout.json     **留出集 20 题**（M3 写完、M4 一次性跑 13/20、之后不再调参）
├── scripts/             开发工具
│   ├── check_layers.py  层依赖检查（架构约束可执行化）
│   ├── make_heldout.py  留出集基线生成/校验（手写 SQL 执行出答案）
│   ├── coverage_scan.py 实验① 覆盖率扫描（三档 + 8 档曲线）
│   ├── plot_coverage.py 覆盖率曲线（Pillow；本机无 matplotlib）
│   ├── drift_table.py   实验② 口径漂移 13 例
│   ├── ablation.py      实验③ 消融（缓存/知识库/守门/窗口指标）
│   ├── heldout_eval.py  实验④ 留出集一次性评估（开发期只跑一次）
│   ├── pg_load_golden.py PostgreSQL 灌数（从 SQLite 原样搬，保证数据完全相同）
│   └── scaffold.py      模块骨架生成
├── docs/                设计与边界文档
│   ├── assets/          论文图（coverage_curve.png 等）
│   ├── 08-技能设计草案.md  "声明式档 = SKILL.md" 的设计草案（待拍板）
│   └── 09-参考实现-superpowers.md  技能型智能体的成熟参考实现分析
├── data/                运行时状态（gitignore）
│
├── 方案骨架.md          项目设计骨架与决策记录
└── 题库分析.md          题库评估结论
```

---

## 快速开始

```bash
# 1) 架构约束自检：核心不得依赖 Web 框架 / 不得反向依赖壳 / 不得有全局状态
python scripts/check_layers.py

# 2) S2 验收：适配器 / 元数据目录 / 知识库 / 缓存 / 历史
python tests/test_s2_storage.py

# 3) S3 验收：手写逻辑计划 → 编译 → 守门 → 执行 → 与基线比对
python tests/test_s3_pipeline.py

# 3b) S3b 验收：dbt metrics.yml 加载器
#     含核心断言：yml 加载的语义模型与手写参考模型在 30 题上
#     计划 / SQL 文本 / 结果集**三层逐题一致**
python tests/test_s3b_dbt_loader.py
python -m nl2sql.semantics.dbt_loader golden_set/metrics.yml --show-sql

# 3c) S3c 验收：语义层表达能力（累计 / HAVING / 跨期对比 / 分组内条件筛选）
#     盯的是"每个能力写错会怎样"：HAVING 没跑进 WHERE、桶条件与 time_range
#     冲突被拒、组内前N 走三层而不是 HAVING……
python tests/test_s3c_expressiveness.py

# 4) S4 验收：规划器 + 编排链路（含规则规划器准确率基线）
python tests/test_s4_agent.py

# 4b) 混合策略验收（离线部分无需 Key）
python tests/test_s4c_hybrid.py

# 4c) 知识库 + few-shot 效果 + 验证式混合（离线部分无需 Key）
python tests/test_s6_knowledge.py

# 5) S5 Web 工作台验收
python tests/test_s5_web_api.py          # 接口契约 + 安全边界
python tests/test_s5_web_static.py       # 静态托管 + 真实 uvicorn 进程

# 5b) 智能体与插件验收（离线，脚本化模型可断言因果）
python tests/test_t1_agent.py

# 5b2) 插件与工具管理验收
python tests/test_p0_plugin_admin.py

# 5b2b) 技能机制验收（发现 / 信任模型 / 报告 / skill.load / 界面第四块）
#       硬断言之一是"零技能配置下工具清单与加机制之前逐字相同"
python tests/test_p0b_skills.py

# 5b2c) 决策场景集（行为评测层：追问 / 披露 / 没数据时不编；需 Key）
#       同一场景集跑 baseline vs skills；差值只作量级参考，未跑 ≠ 通过
python scripts/decision_eval.py --mode both

# 5b3) 插件作者视角试炼（换个身份写一个真插件，验证"可插拔"不是自说自话）
python scripts/plugin_dev_trial.py --no-llm    # 离线 1–5 节
python scripts/plugin_dev_trial.py             # 加第 6 节：模型是否真的选中新工具（需 Key）

# 5d) 跨引擎等价验收（需本机 MySQL + .env 里的凭据；A–E 节只读，不写库）
#     F 节验证"改一个环境变量就换引擎"，D 节跑两边黄金集并逐题比对
python tests/test_m1_mysql.py

# 5d2) 第三引擎 PostgreSQL 验收（不需要装服务/管理员权限）
#     用 embedded-postgres 自带的 PostgreSQL 18 二进制：pip install embedded-postgres
#     （或用 NL2SQL_PG_BIN 指向任意 PG 的 bin 目录）；测试自己 initdb/起停/灌数。
#     没装二进制时打印安装说明并返回 0 —— 未跑 ≠ 通过。
python tests/test_pg_engine.py

# 5e) 执行器等价验收（离线部分无需 Key）
#     6 个脚本化场景逐字段比对"自写循环"与"LangGraph 执行器"（含事件序列）；
#     递归预算用 8 次工具调用压满；SSE 首字节用线程内真 uvicorn 量
#     （TestClient 会把响应缓冲完，量不出首字节）
python tests/test_m2_langgraph.py
python tests/test_m2_langgraph.py --require-live   # 需 Key：30 题录制—重放（30/30 一致）

# 5f) 语义表达力验收（离线：时间粒度 + 标量比较，含黄金集 29/30 与 D3 口径说明）
python tests/test_m3_semantics.py
# 留出集只校验完整性，**不跑规划器**（开发期不跑，M4 才用）
python scripts/make_heldout.py --check
# 按类别看准确率（论文实验章用；自检时 30/30，预测文件传入时按类汇总）
python golden_set/evaluate.py --by-category

# 5g) M4 四个实验（rule 全部离线；加 --with-llm 需 Key，2026-10 已补跑）
python scripts/coverage_scan.py --plot docs/assets/coverage_curve.png  # ① 覆盖率
python scripts/drift_table.py                                           # ② 口径漂移
python scripts/ablation.py                                              # ③ 消融
python scripts/heldout_eval.py                                          # ④ 留出集（只跑一次！）
# 模型档：$env:NL2SQL_LLM_API_KEY='sk-xxx'
# python scripts/coverage_scan.py --with-llm --plot docs/assets/coverage_curve.png
# python scripts/heldout_eval.py --with-llm

# 5h) M5 观测验收（离线：九态契约 / 聚合口径 / 两个执行器的工具上报 / 管理端点）
python tests/test_m5_observability.py
# 起界面后：管理页 → 使用统计；或 GET /api/admin/stats?days=7

# 5i) agent 容错验收（离线：模型/工具重试、输出修复、上下文预算；74 项断言）
python tests/test_agent_resilience.py

# 5j) M7 数据开发工作台验收（离线，无需 Key）
#     多项目 / 可视化数据源 / 任务生成-修改-版本-回滚 / 项目信息注入
python tests/test_m7_tasks.py
python tests/test_m7_agent_tasks.py
python tests/test_m7_jdbc.py            # JDBC 桥需要 JDK 17；首次要联网下载 Maven 驱动
python tests/test_m7_projects_api.py    # 项目/数据源/任务 HTTP 契约
# 起界面后：「项目」新建项目并连库 → 点项目进入 →「任务」里对 ods/dwd/dwm/ads
#     文件夹右键 → 新建任务（SQL 可运行；PySpark 只保存代码与版本，执行器未接入）

# 5k) 前端体验（P0，无需 Key）
#     查询走 SSE：连上即返回进度（实测首反馈 ~70ms，之前要等 2.4–3.9s），
#     时间线显示「连接→选工具→执行→整理」；可随时取消；结论按 Markdown 渲染；
#     首轮有骨架屏；键盘可达（skip link / 焦点环 / aria-live）——axe 四态 0 违规。
#     视觉为 v9「实验记录本 · 暖纸墨线」（按 impeccable skill 重设计，v8 之上建立
#     主次）：左侧 236px **墨色记录书脊**（rail，纸色当前项 + 朱红小方），右侧纸页
#     主列：56px 顶栏 + 带 gutter 编号的记录页；页头 2px 墨线压底、页面标题 38px
#     serif，结果区按「页眉注记（KPI）→ 记录正文（结论，2px 墨线开篇）→ SQL 证据 →
#     结果表（墨条表头）」排，单一朱红作行动色（主按钮 hover / 错误），橄榄作状态色。
#     导航分两级：首页只放「工作台 / 项目」；点项目直接进「数据开发」工作台（IDE 式
#     布局）：顶部是打开任务的 tab 条（「概览」页签常驻），左侧 264px 导航栏（项目
#     信息 + 搜索 + 文件夹树 + 新建任务），中间是 CodeMirror 6 编辑器（54px 纸色
#     行号槽、SQL 语法高亮、括号/引号自动闭合、撤销重做；“名称 /
#     类型 / 文件夹 / 版本 + 运行 / 保存 / 删除”），底部是「运行记录 / 版本历史 /
#     任务信息」面板（默认展开运行记录）。「概览」页签是项目首页：serif 大标题、
#     4 格统计、最近任务与项目资料两卡，可跳回项目概览页。右键菜单（新建任务 /
#     子文件夹 / 重命名 / 删除）+ 新建任务对话框照旧（SQL 可运行；PySpark 只保存
#     代码与版本，执行器未接入会如实提示）。编辑器细节：Ctrl+Enter 保存、Tab /
#     Shift+Tab 缩进；Ctrl+Space 补全（SQL 关键字 / 函数、数据源表名、FROM / JOIN
#     引用表的列、别名.列，补全数据来自 GET /api/tables，与任务执行同一数据源，
#     不调 LLM、不新增后端接口）；未知表 / 未知限定列用赭石波浪线标出并给出提示
#     （只报高置信问题，裸列 / 函数 / CTE / 子查询不误报）；Ctrl+Shift+F 或
#     「格式化」按钮按 sql-formatter 规范化（关键字大写、换行缩进，按需懒加载
#     chunk）；SQL 未保存时切换页签 / 任务或关闭标签会先弹确认
#     （保存并切换 / 放弃修改并切换 / 取消），刷新或关窗也会提醒，避免丢改动。
#     文案层级与暖纸墨线不变：页头 2px 墨线压底、标题 serif，结果区按「页眉注记
#     （KPI）→ 记录正文（结论，2px 墨线开篇）→ SQL 证据 → 结果表（墨条表头）」排，
#     单一朱红作行动色（主按钮 hover / 错误），橄榄作状态色。
#     三字体分工（IBM Plex Serif 标题 / IBM Plex Sans 正文 / JetBrains Mono 编号与
#     数据），圆角 2–8px，无渐变/无玻璃/无重投影；1280/900/520 三档无横向溢出，
#     axe 七态 0 违规（含 IDE 概览 / 编辑器 / 移动端三态独立探针）；
#     右下角常驻一枚朱红「问」印章（任何页面都在），点开是「随行智能体」抽屉：
#     与当前项目同一套只读查询引擎对话（流式进度 / 结论 / SQL / 6 行小表），结果可
#     一键「在结果页打开」送回工作台；切项目自动清空并开新会话。抽屉同为纸面 +
#     2px 墨顶线，桌面 416px 右下浮出，≤900 收窄、≤640 变底部整幅。
#     设计系统与纪律见 DESIGN.md，方向契约见 .impeccable/surfaces/。
#     验证：起界面后点「查询」观察进度逐步出现，中途点「取消」应立即恢复；
#     $env:NL2SQL_LLM_API_KEY='sk-xxx' 后问题才有真实模型结论（离线时用规则规划器）。

# 5c) 智能体真实模型验收（需 Key）
#     $env:NL2SQL_LLM_API_KEY='sk-xxx'
python tests/test_t7_agent_live.py
python tests/test_t8_agent_http.py       # agent 模式的 HTTP 契约

# 6) 启动 Web 工作台
#    cd apps/web/frontend && npm install && npm run build
#    cd ../../.. && uvicorn apps.web.backend.main:app --port 8000
#    打开 http://127.0.0.1:8000
#    查询走 SSE（/api/query/stream）：进度实时可见、可取消、结论 Markdown 渲染

# 5) 黄金集：造数 → 建基线 → 自检（应输出 30/30）
cd golden_set
python seed_data.py
python build_baseline.py
python evaluate.py
cd ..

# 6) 端到端问答（规则规划器，无需模型）
python -c "
from nl2sql import NL2SQL, NL2SQLConfig
cfg = NL2SQLConfig(state_dir='data').add_sqlite('golden_set/ecommerce.db')
with NL2SQL(cfg) as engine:
    engine.refresh_snapshot()
    out = engine.ask('2024年6月，数码品类的实收金额是多少？', now='2024-06-30')
    print(out.status)
    print(out.sql)
    print(out.result.columns, out.result.rows)
"

# 7) 接真实模型（可选，需自备 API Key，通过环境变量注入）
$env:NL2SQL_LLM_API_KEY='sk-xxx'          # 不要写进代码或文件
python scripts/check_llm.py               # 连通性自检
python tests/test_s4b_llm.py              # 模型对黄金集的准确率
python scripts/compare_planners.py        # 规则 vs 模型 逐题对比与互补度
```

> `ask()` 目前仍返回 `status="error"` 且 `error_code="NOT_IMPLEMENTED"`——
> 执行链路要到 S3/S4 才接线。这是刻意设计：**明确告知未接线，而不是抛异常或返回假数据**。

---

## 核心设计原则

| 原则 | 落地方式 |
|---|---|
| **一个核心 + 三个壳** | 核心不依赖 Web 框架、无全局状态；差异全在适配层 |
| **不让 LLM 直写方言 SQL** | 语义层产出逻辑计划，按方言能力声明确定性编译 |
| **缓存不参与正确性判断** | 只保留逻辑计划指纹缓存，去掉跨会话语义缓存 |
| **不自创指标 DSL** | 兼容 dbt `metrics.yml` |
| **免审 ≠ 无版本** | 知识库无审批流，但有版本、停用开关、测试检索 |
| **错误反馈就是产品体验** | 只给 SQL + 结果表，因此错误必须结构化、可操作 |

完整取舍与理由见 `docs/02-模块边界.md`。

---

## 文档索引

分两类：**01–09 与 11 是分模块的施工记录**（每一份都由那一阶段真实的测量结果写成，
包含踩到的坑与负结果），**10 是整体判断**（现在有什么、还缺什么、按什么顺序补）。

| 文档 | 内容 |
|---|---|
| **`docs/10-全局规划.md`** | **整体路线图：五个真缺口、论文主线与三个实验、六阶段计划、不做清单** |
| `docs/01-仓库结构与依赖方向.md` | 目录布局、依赖方向、落地顺序 |
| `docs/02-模块边界.md` | 每个模块的职责 / 明确不做 / 关键取舍 / §19 两个负结果 |
| `docs/03-智能体与插件架构.md` | 工具循环、能力白名单、行为约束的来源 |
| `docs/04-插件与工具管理.md` | 插件作者视角、管理界面、能力授予 |
| `docs/05-内部试用记录.md` | 内部试用的真实问题清单 |
| `docs/06-dbt指标加载.md` | `metrics.yml` 两套规范、表达式与 JOIN 推导、portability |
| `docs/07-语义层表达能力.md` | 缺口清单、四个真坑、四种规划器实测 |
| `docs/08-技能设计草案.md` | 声明式档（`SKILL.md`）、P0/P1 实施记录（引导注入 + 3 份 playbook + 决策场景集对照实验）、P2–P3 待拍板 |
| `docs/09-参考实现-superpowers.md` | 可迁移的五条、不该抄的四条 |
| `docs/11-MySQL适配器与跨引擎等价.md` | 适配器四层、**判分器漂移**、只读账号与 `credential_ref`；C3 缺口由 M3 闭合（C 节 30/30）；**§10 第三引擎 PostgreSQL**（核心零改动、30/30 跨引擎等价、embedded PG 起停与沙箱限制） |
| `docs/12-LangGraph执行器与等价性.md` | 单步状态机、图谱、两执行器等价性、SSE 首字节、五个"数字不对"的坑 |
| `docs/13-语义表达力.md` | 时间粒度与标量比较、`against` 的歧义建模、共享语义模型的顺序依赖 bug、29/30 与 D3 口径说明 |
| `docs/14-实验.md` | 覆盖率扫描（8 档曲线 + "失败不可见"）、口径漂移 13 例、消融、留出集 13/20、四张表与复现命令 |
| `docs/15-观测与试用记录.md` | 观测两表与统计口径、九态失败归因、管理页统计、试用模板与对账（真实试用 **0/20 未开始**） |
| `docs/16-数据开发工作台（M7）.md` | 多项目 / 可视化数据源（含 JDBC 泛用桥）/ SQL 任务（生成·修改·版本·回滚，不写业务库）/ 项目信息注入；验收与已知限制 |
| `golden_set/README.md` | 评测集用法、口径约定、多方言扩展 |
| `方案骨架.md` | 项目定位、分层骨架、决策记录、待定项 |
| `题库分析.md` | 3,378 题题库评估：为何不能当黄金集、改用途建议 |

---

## 下一步

**不再维护"P0 范围"这类阶段性清单**——阶段范围随实测在变，写在 README 里一定会过期。
当前的全局判断、缺口排序与六个月路线图见 **`docs/10-全局规划.md`**。

论文题目已定：**基于LangGraph智能体的NL2SQL数据仓库辅助查询系统设计与实现**（六个月）。

| 月 | 主题 | 硬验收 |
|---|---|---|
| M1 | **已完成**：git + 单一版本号 + pre-commit + MySQL 适配器（+ 2026-10 补 PostgreSQL 第三引擎） | 30 题 SQLite vs MySQL **逐题一致**（M3 后 C 节已 30/30；D 节两端 29/30 同失败集）；**SQLite vs PostgreSQL 30/30 逐题一致、端到端 29/30 同失败集** |
| M2 | **已完成**：LangGraph 智能体（`build_langgraph` + 流式） | 两执行器 6 场景逐字段一致；30 题**录制—重放 30/30 一致**（Prompt 0 偏差）；首字节 <1s |
| M3 | **已完成**：语义层两个原语（时间粒度、标量比较） | 黄金集 **29/30 = 96.7%**（≥28/30）；D3 为黄金集口径不一致 |
| M4 | **已完成**：实验（覆盖率扫描、口径漂移、消融、留出集） | 覆盖率曲线紧贴语义天花板（8 档差 0–1 题）；漂移 13/13 >5%；留出集 **13/20 = 65.0%**（如实记录，未据此调参）；**llm 档 23/20/17、留出集 12/20**——换规划器没有提高上限 |
| M5 | **已完成离线部分**：观测统计 + 九态失败归因 | 统计接口可用、归因文案九态齐全、契约测试 **87 项**；**真实试用 0/20 条未开始**（未跑 ≠ 通过，模板在 `docs/15`） |
| M7 | **已完成（2026-10 追加里程碑）**：数据开发工作台 | 多项目 + 每项目独立引擎；可视化数据源（SQLite/MySQL/PG/JDBC 桥，含测试连接）；SQL 任务 agent 生成/修改带 `-- ai修改` 版本历史 + 回滚；任务只写项目任务库、**不写业务数据**；四个 M7 套件 + 全量离线回归通过 |
| M6 | 成文与答辩演示 | 预答辩通过 |

明确**不做**的：NoSQL 数据源、写操作、多方言转译、dbt `cumulative`/`conversion`、审批流。
理由逐条写在 `docs/10` 第 6 节。
