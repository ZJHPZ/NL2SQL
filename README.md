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
| **M1** | **第二个引擎：MySQL 适配器 + 跨引擎等价验收** | ✅ 见 `docs/11` |
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

**散文结论 + 结构化结果表同时返回**：agent 的回答是自然语言（给判断），
同时把工具拿到的数据提升为 `QueryOutcome.result`（给可排序、可导出的明细）。
刻意**不解析散文还原表格**——结构化数据本来就在工具返回值里，只是之前没往下传。

设计细节与实测数据见 **`docs/03-智能体与插件架构.md`**。
加新工具照着 `plugins/demo/` 抄即可。

> agent 模式默认**关闭**（默认仍是 `llm` 单次规划）：它多步、更慢更贵，
> 应由用户按需开启——`cfg.agent.planner = "agent"`。

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
> 详见 **`docs/11-MySQL适配器与跨引擎等价.md`**。

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

---

## 仓库结构

```
talk-to-sql/
├── nl2sql/              ★ 核心引擎（可独立 pip install，无 Web 依赖）
│   ├── contracts/       接口与数据模型（仅标准库）
│   │   ├── tool.py      工具契约 + 能力清单（智能体的手脚与权限）
│   │   └── session.py   会话契约（回合制澄清需要）
│   ├── storage/         元数据 / 知识 / 缓存 / 会话（默认全 SQLite）
│   ├── sources/         数据源适配器（SQLite / MySQL，共用同一套能力声明）
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
│   ├── agent/           **AgentRuntime 工具循环**（图契约已定型；LangGraph 执行器在 M2）
│   │   ├── runtime.py   循环、观测回灌、防打转、运行时硬约束
│   │   └── builtin.py   9 个内置工具
│   ├── config.py        配置（演示 pydantic-free 的显式配置）
│   ├── registry.py      组件注册表（实例级，非全局）
│   └── api.py           门面 `NL2SQL`
│
├── plugins/             **用户插件目录（一切皆插件）**
│   └── demo/            示例插件：数据质量探查工具（可当模板抄）
│
├── skills/              **技能目录（声明式档：只写文本，不写代码）**
│   └── README.md        格式说明（P0 只通了机制，P1 才放项目自己的 playbook）
│
├── apps/                三个壳：只做适配，不含业务逻辑
│   ├── web/             壳① 本地 Web 工作台（首发，已完成）
│   │   ├── backend/     FastAPI：装配 + 序列化 + 静态托管
│   │   └── frontend/    React + Vite（TS）：查询工作台
│   ├── sdk/             壳② 对外 Python SDK
│   └── internal/        壳③ 内部工具
│
├── golden_set/          黄金评测集（30 题 + 判分器）
│   └── metrics.yml      黄金语义模型的 **dbt 语法版本**（等价性验收物）
├── scripts/             开发工具
│   ├── check_layers.py  层依赖检查（架构约束可执行化）
│   └── scaffold.py      模块骨架生成
├── docs/                设计与边界文档
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

# 5b3) 插件作者视角试炼（换个身份写一个真插件，验证"可插拔"不是自说自话）
python scripts/plugin_dev_trial.py --no-llm    # 离线 1–5 节
python scripts/plugin_dev_trial.py             # 加第 6 节：模型是否真的选中新工具（需 Key）

# 5d) 跨引擎等价验收（需本机 MySQL + .env 里的凭据；A–E 节只读，不写库）
#     F 节验证"改一个环境变量就换引擎"，D 节跑两边黄金集并逐题比对
python tests/test_m1_mysql.py

# 5c) 智能体真实模型验收（需 Key）
#     $env:NL2SQL_LLM_API_KEY='sk-xxx'
python tests/test_t7_agent_live.py
python tests/test_t8_agent_http.py       # agent 模式的 HTTP 契约

# 6) 启动 Web 工作台
#    cd apps/web/frontend && npm install && npm run build
#    cd ../../.. && uvicorn apps.web.backend.main:app --port 8000
#    打开 http://127.0.0.1:8000

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
| `docs/08-技能设计草案.md` | 声明式档（`SKILL.md`）、P0 实施记录、P1–P3 待拍板 |
| `docs/09-参考实现-superpowers.md` | 可迁移的五条、不该抄的四条 |
| `docs/11-MySQL适配器与跨引擎等价.md` | 适配器四层、29/30 等价、**判分器漂移**、只读账号与 `credential_ref` |
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
| M1 | **已完成**：git + 单一版本号 + pre-commit + MySQL 适配器 | 30 题 SQLite vs MySQL **逐题一致**（实测 29/30，差的 C3 是计划内方言缺口） |
| M2 | LangGraph 智能体（`build_langgraph` + 流式） | 两执行器 outcome **逐题一致**，首字节 <1s |
| M3 | 语义层两个原语（时间粒度、标量比较） | 黄金集 ≥28/30 |
| M4 | 实验（覆盖率扫描、口径漂移、消融） | 四张表 + 一张曲线 |
| M5 | 体验、统计、真实试用 | ≥20 条真实问题 |
| M6 | 成文与答辩演示 | 预答辩通过 |

明确**不做**的：NoSQL 数据源、写操作、多方言转译、dbt `cumulative`/`conversion`、审批流。
理由逐条写在 `docs/10` 第 6 节。
