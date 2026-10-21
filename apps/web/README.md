# apps/web —— 本地 Web 前后端（首发形态）

## 为什么首发选它

三种形态（Web / SDK / 内部工具）中，Web 最快验证「**SQL + 结果表**」这个核心体验。
SDK 是从核心抽包，几乎零额外成本；内部工具本质是 Web 换外壳。反过来先做 SDK 的话，
没有界面反馈循环，RAG 与语义层的效果很难判断。

## 这不是聊天应用，是查询工作台

大多数 text2sql 界面做成聊天气泡流。那个形态有三个问题：

1. 开发者会反复看结果表、扫字段、复制数字 —— 气泡里塞表格，滚几下就找不到
2. 开发者会改 SQL 再跑 —— 聊天流里没有"当前 SQL"这个稳定对象
3. "最新结果"才是主角，不该被历史消息推走

所以布局是**左结果 / 右历史**：当前结果常驻主区，历史可一键恢复但不抢注意力。

## 六个必须做好的界面决策

因为核心只给「SQL + 结果表」，**错误与边界反馈就是产品体验本身**。
后端 `QueryOutcome` 里的每个字段都对应一个界面动作：

| 界面必须做 | 对应字段 |
|---|---|
| 状态条显示**来源**（哪个规划器）、行数、耗时、是否缓存、数据时间 | `notes` / `cached` / `trace` / `data_time` |
| 结果为空时**回显生效的过滤条件**，让用户知道是条件筛掉了 | `applied_filters` |
| 列名不存在时展示**候选字段**，可点击复制 | `candidates` / `suggestions` |
| 被截断时明示「仅前 N 行」并提供导出 | `truncated` |
| 口径未验证时**显著警告**，不伪装成确定答案 | `notes` 含"未验证" |
| 无法回答时显示建议，**不给假 SQL** | `sql = None` |

## 安全边界：编辑框不是旁路

`POST /api/execute` 用于执行用户手写的 SQL，它**仍然经过 `guard`**：

```
guard.check(sql)   # 只读 / 禁多语句 / 强制 LIMIT
adapter.execute()  # 仅通过校验后才执行
```

若让编辑框直连数据库，界面就成了绕过只读与 LIMIT 的旁路——
用户写 `DROP TABLE` 就执行了。这条路径只是**跳过规划器**
（用户已给出 SQL，不必花模型的钱），不能跳过守门。

## 目录结构

```
apps/web/
├── backend/                 # FastAPI：只做装配与序列化，不写业务逻辑
│   ├── main.py              # 应用装配 + lifespan + 静态托管 + 异常处理
│   ├── deps.py              # 引擎长生命周期挂在 app.state（核心禁止模块级全局）
│   ├── shell_config.py      # 壳层读环境变量 → 组装核心配置
│   ├── schemas.py           # 请求/响应模型（与 QueryOutcome 对齐）
│   ├── serializers.py       # JSON 安全转换（不依赖 FastAPI 自动转换）
│   └── routers/{query,knowledge,info}.py
└── frontend/                # React + Vite（TS）
    ├── src/App.tsx              # 工作台布局
    ├── src/components/          # StatusBar / ResultTable / SqlPanel / SidePanels
    └── src/{api,types}.ts
```

## 启动

### 生产模式（单进程，推荐日常用）

```bash
cd apps/web/frontend && npm install && npm run build   # 只需一次
cd ../../..
uvicorn apps.web.backend.main:app --port 8000
# 打开 http://127.0.0.1:8000
```

### 开发模式（前端热更新）

```bash
# 终端 1
uvicorn apps.web.backend.main:app --reload --port 8000
# 终端 2
cd apps/web/frontend && npm run dev      # http://127.0.0.1:5173，/api 已代理到 8000
```

### 环境变量

| 变量 | 说明 |
|---|---|
| `NL2SQL_DB` | SQLite 路径（默认 `golden_set/ecommerce.db`） |
| `NL2SQL_STATE_DIR` | 状态目录（默认 `data`） |
| `NL2SQL_SOURCE_ID` | 数据源标识（默认 `default`） |
| `NL2SQL_PLANNER` | `auto` / `rule` / `llm` / `hybrid` / `hybrid_verified` |
| `NL2SQL_LLM_API_KEY` | 模型 Key；**不设即自动退化为规则规划器** |
| `NL2SQL_LLM_BASE_URL` / `NL2SQL_LLM_MODEL` | 端点与模型名（默认 DeepSeek） |

> Key 由壳层从环境变量读取后显式注入核心。**核心层刻意不读环境变量**——
> 否则同一个配置在不同机器上行为不同，测试与排障都会变难。

## 自动化验收的边界（重要）

| 层次 | 手段 | 状态 |
|---|---|---|
| 接口契约与安全边界 | `tests/test_s5_web_api.py`（TestClient） | ✅ 自动 |
| 静态托管与真实进程 | `tests/test_s5_web_static.py`（起真 uvicorn） | ✅ 自动 |
| **前端交互** | 需要人在浏览器点 | ❌ **无法自动验证** |

**没有浏览器自动化**，因此排序、过滤、导出 CSV、SQL 重跑、点击候选字段
这些交互只能人工确认。这里如实说明，不假装测过。

## 不做的事（一期）

- 不做自然语言长篇分析、不做图表
- 不做多用户与登录（个人场景；核心无全局状态，后期加不难）
- 不做流式输出（p50 约 1.2s，骨架态即可；`trace` 字段已为将来留好）

