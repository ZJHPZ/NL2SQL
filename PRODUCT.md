# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

数据分析 / 数据工程同学，在本地或内网环境中日常取数、核对指标、写与改只读 SQL；希望用自然语言替代手写 SQL，追求一次问对、结果可信、可导出。

## Product Purpose

本地部署的自然语言数据问答工作台：把「问题 → 语义映射 → 只读 SQL → 结果表」做成一条可观察、可取消、可复现的路径。成功标准是用户敢把它的结论与 SQL 直接拿去用。

## Positioning

语义层优先、拒绝编造：问题无法解析时明确说明并给出建议，而不是生成一条看似合理实则答非所问的 SQL；每条查询的 SQL 可编辑、可复跑，并保留执行过程。

## Operating Context

多项目 SQLite 数据源；表快照与语义口径；SQL 任务保存、运行与版本记录；结果表排序 / 过滤 / 导出 CSV；用户会在浏览器里长时间停留、反复查询、对比结果并切换项目。

## Capabilities and Constraints

- 能力：自然语言提问；流式工具调用过程；结论 + SQL + 结果表三件套；结果排序 / 过滤 / 导出；多项目数据源；SQL 任务与版本；历史、知识库、插件与表结构面板。
- 技术：前端 React 18 + TypeScript + Vite（`apps/web/frontend`）；后端 Python FastAPI；单用户本地服务，无登录与多租户；界面中文。
- 未决：多用户/权限模型、托管部署形态均未定，不得在界面中暗示已具备。

## Brand Commitments

名称 talk-to-sql 已存在；中文界面；不虚构产品能力、数据或客户证据。

## Evidence on Hand

仓库可运行（前端 `apps/web/frontend`、后端服务、README）；无真实客户证言、基准或案例数据，不得虚构。

## Product Principles

1. 可信优先于流畅：不确定就明说，绝不编造。
2. 工作是主线：提问 → 结论 → 证据（SQL）→ 明细，一条路径到底。
3. 可观察：执行过程、错误与状态始终可见、可取消。
4. 工具感克制：不卖弄，不为装饰牺牲阅读与操作。
5. 高频可复用：重复查询、切项目、对照结果都应该快。

## Accessibility & Inclusion

键盘可达、焦点可见、对比度满足 WCAG AA；现有产品 E2E 以 axe 零违规为基线。
