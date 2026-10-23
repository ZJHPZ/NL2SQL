---
version: 1
slug: "apps-web-frontend-src-app-tsx"
primary_target: "apps/web/frontend/src/App.tsx"
related_targets: ["apps/web/frontend/src/styles.css"]
---

# Surface brief — talk-to-sql 数据工作台（整体重设计）

## Surface
- Primary target: `apps/web/frontend/src/App.tsx`（React 单页应用，含问答 / 项目 / 任务三视图）
- Mode: operate（product UI）
- Platform: web
- Audience: 数据分析/工程同学，日常在本地或内网快速取数
- Jobs: 自然语言提问 → 观察流式执行过程 → 结论 / 可编辑 SQL / 结果表 → 排序、过滤、导出 CSV；多项目管理、SQL 任务版本、历史与知识库
- Hard constraints: 功能、数据、路由与交互流程不变；中文界面；键盘可达；axe 四态 0 基线；移动/中间/宽三档可用

## Direction contract

THESIS: 这是一个"能核对的实验记录"，不是聊天应用：屏幕骨架是记录页结构——题头、编号、页边注记、横线表格——把"一次查询 = 一次可复现实验"变成可见的结构。拒绝聊天流、卡片墙、深色数据大屏这三个类别默认。

OWN-WORLD: 暖象牙纸（底 #F4EFE2、墨 #1B1812、次级 #57503F、弱 #8A8069）；一条橄榄绿（#4A5A3A）只标记"当前/活的"，朱红只用于错误；宋体大字标题 + IBM Plex Sans 正文 + JetBrains Mono 编号与数据；1px 发丝墨线（16% 墨）、纸面轻投影只带 1px 位移与模糊、圆角 2–4px；边线上的 mono 索引编号是签名；无渐变、无玻璃、无装饰纹理。

STORY: 用户打开时先看到一张记录题头：这份工作台做什么、数据库已就绪、从哪开始。输入问题后进入记录页——顶部是这次查询的题头与状态，中间是结论与证据（SQL），下面是可排序/过滤/导出的横线结果表，右侧是历史与知识库索引；每一步都有编号，随时回到上一份记录。

FIRST VIEWPORT: 1440×900。左侧 236px 纸色导航栏：顶部衬线 wordmark，竖排索引导航 01 问答 / 02 项目 / 03 任务（active 用印章式小方块 + 墨色），底部会话号与"本地 SQLite · 只读"元信息。主区顶条 56px：当前栏目名（mono 小标）、项目切换、三个图标动作。舞台：内容左边距留出 64px 注记槽，悬着 mono "01" 与一条竖线；宋体 44px 标题「问一句，拿到可核对的 SQL 与结果」，副题 15px 限 46ch；下方墨边 composer（输入 + 时间锚点 + 查询按钮，按钮在右端）；示例问题是无边框横线索引行（mono A1–A4）；再下"最近问过"与文本链接。

FORM: code-led 构建；记录本方向在自排 7 个 grounded 方向中排第 3（roll 分配）。seed: seed:b22ed0be345ccc31

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
