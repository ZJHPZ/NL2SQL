# DESIGN.md — talk-to-sql 设计系统

> 本文件由 impeccable skill 的完成期文档环节生成，记录**已建成**的世界（不是愿望清单）。
> 视觉方向来自方向轮选定的「实验记录本 · 暖纸墨线」（seed:b22ed0be345ccc31，code-led Operate surface）。
> 界面是中文工具界面：一页可用的取数工作台，优先「可信、可核对、可长时间看」，其次才是表达。

## 世界（WORLD）

暖纸底 + 墨字 + 1px 发丝墨线的「实验记录本」：页面像一张铺开的记录纸，左边栏是记录本的索引页，
内容区按「编号 → 标题 → 正文 → 证据」的记录页顺序排。没有渐变、没有玻璃、没有装饰纹理、没有重投影；
层次只来自纸色三档、1px 墨线和字号节奏。橄榄绿只标「当前活跃的」，朱红只做错误，其余一律墨色。

- **主色**：纸 `#f4efe2`，卡片纸 `#f8f3e6`，次级纸 `#efe8d7`，表头/脚注纸 `#e7dfcc`
- **墨色**：`#1b1812`（正文）/ `#4f4838`（次级）/ `#6e6652`（弱）/ 发丝线 `rgba(27,24,18,.10/.16/.30)`
- **语义色**：橄榄 `#4a5a3a`（active / 当前 / 关键 KPI）、朱红 `#a83a2a`（错误 / 不可用）、赭石 `#8a6d33`（提示）
- **色彩纪律**：不再引入第二个强调色；不用蓝色（旧版遗留的蓝只允许在历史注释里出现，不得进 UI）

## 字体（TYPE）

同一世界里三种角色，各司其职，不互借：

| 角色 | 字体 | 用法 |
|---|---|---|
| 标题声 | **IBM Plex Serif**（中文回退 宋体/思源宋体） | 舞台大标题、页面标题、空态标题；只做大字，不进正文 |
| 正文声 | **IBM Plex Sans** | 正文、按钮、表单、导航 |
| 编号与数据 | **JetBrains Mono** | 编号（01/02/03）、KPI、SQL、表头、表数据、行号、脚注标签；tabular-nums |

字号阶（固定 rem，不随手加半像素）：12 / 13 / 14 / 15 / 16 / 17 / 20 / 24 / 28 / 44（px，均为 4px 网格内整数）。
- 舞台标题 44px / 1.15 / serif / letter-spacing -0.02em（display ≤ 6rem 的底线之上）
- 正文 15px / 1.85 / 行宽 68ch；结果表 13px；表头与脚注 10.5–11px mono 大写间距
- 结论标题是 mono 11px letter-spacing .16em 的「标签」，不是散文标题

## 布局（LAYOUT）

- 外壳：`.app` = grid `236px` 左栏 + `1fr` 主列；`.rail` sticky 100vh，纸色底，放品牌、01–03 导航、版本与会话；
  主列顶部是 56px 半透明纸 topbar（项目切换 + 操作），窄屏（≤900px）rail 收起、顶部导航出现。
- 页面：`.page` 最大宽 1240px，padding `34px 40px 72px 96px`——左侧 96px 是**注记槽**，放 `.gutter-tag` 编号（01/02/03）；
  编号在标题左侧 58px、mono 11px，是记录本的签名，不要换成 eyebrow 小标签。
- 舞台（空态）：顶部 88px 起，单列 780px：serif 44px 标题 → 副题 46ch → 墨边 composer → A1–A4 横线索引 → 最近问过/链接/注记。
- 工作区：`.content-grid` = 主列 1fr + 侧栏 300px，gap 44px；结果按「KPI 脚注条 → 结论 → SQL 证据 → 结果表」纵向排；
  侧栏只放历史/知识库/插件，不做第二视觉中心。
- 圆角只用 2/3/4/6/8px：按钮 2–3px、卡片 4px、输入 3px；没有大圆角卡片墙。

## 组件（COMPONENTS）

- **composer**：1px `--rule-strong` 墨边、纸底、r4、无影。聚焦时只加橄榄 3px 光环（`--accent-soft`），不移动位置。
- **卡片**（statusbar/answer/sqlpanel/result/stream-box/table-card）：`--card` 纸色 + 1px `--rule` 线 + r4；不用阴影补层次。
- **statusbar**：一行 KPI（badge / planner / 行数 / 耗时 / 缓存 / 数据时间 / fingerprint），其余口径与建议收进
  `.status-notes` 脚注区（上方 1px 发丝线分隔，12.5px 次级墨色）——状态条不再被说明文字撑成大块。
- **SQL 面板**：纸底 mono 编辑器，面板底色用 `--paper-2` 与页面区分，「证据」感；不复用代码高亮主题色。
- **结果表**：横线表，无竖线；表头 mono 10.5px、纸 3 底色；行 hover 只加 6% 橄榄；数字右对齐 tabular-nums。
- **侧栏**：历史/知识库/插件都是无卡片记录索引（行 + 发丝线），tab 用橄榄下划线，不抢结果区。
- **按钮**：主按钮墨底纸字（`--ink` / `--paper`），次按钮透明 + 1px 线，危险按钮赭石文字；尺寸 28–34px 高。
- **浏览器表面**：`::selection` 橄榄、caret/accent-color 橄榄、`scrollbar` 细墨色、`:focus-visible` 2px 橄榄；这些是「做出来」的证据，不是装饰。

## 状态（STATES）

每种状态都有明确词汇，不要新增语气：
- 流式：时间线 + 骨架屏（不是 spinner）；首反馈目标 <200ms
- 空态：教用户怎么用的 3 条示例（A1–A4 索引）+ 最近问过；空结果用 dashed 说明块，不是空白卡
- 错误：statusbar tone-bad + 朱红语义，保留取消/重试路径；取消后回到可再问状态
- clarify：问题以 chips 呈现，答案可点选；口径未验证用 warn 淡底提示
- 成功：KPI 可见、结论 + SQL + 表齐全，可导出 CSV

## 动效（MOTION）

150–250ms、只传达状态；`prefers-reduced-motion` 全关。已移除旧版的入场编排动画（记录页不需要依次浮现）。
hover 只改颜色/下划线，不做位移缩放；focus 环不靠动画。

## 响应式（RESPONSIVE）

- ≥1180：rail 236 + 内容 1240（80 槽 + 40 右距）
- ≤1180：rail 200，content-grid 改单列，侧栏下移
- ≤900：rail 收起，顶部导航出现，topbar 换行（56 → 102）
- ≤640：单列、topbar 142、舞台标题 28、表格横向滚动、示例索引仍可读
三档均无横向溢出（520/900/1280 实测 overflowX=0）。

## 可访问性（A11Y）

axe 四态 0 违规是基线；skip-link 到主 landmark；焦点可见且为实色橄榄；对比度 0 失败（探针 lowContrast=0）；
dark containers 仅剩墨底按钮与 skip-link 两处（有意）；所有交互控件 ≥28px、可键盘操作；中文 UI 无英文装饰标签。

## 纪律（DO / DON'T）

- DO：新组件先查本文件的 token 与字号阶；用 `var(--paper/--ink/--rule/--accent)`；先画纸色层次再加线。
- DON'T：不要引入蓝色/紫色强调色；不要渐变文字、玻璃/模糊、装饰纹理；不要 >1px 彩色单边边框；
  不要卡片墙、不要卡片圆角 >8px、不要给卡片加大软阴影；不要把 mono 当装饰字体；不要用 eyebrow 小标签代替 gutter 编号。
- 维护：token 在 `apps/web/frontend/src/styles.css` 的 v8 段 `:root`（约 2091 行）；
  新增覆盖规则追加在文件末尾（v8 → v8.1/8.2 → v9 的顺序），不要回改 base/v5/v6 段硬编码色——
  顺序 + 特异性靠后段覆盖；改前先跑 `impeccable detect --json apps/web/frontend/src`。

## 主次与导航（v9，2026-10-23）

「通体一个色调」的修正不是加颜色，而是**分区**：

- **墨色书脊**：左侧 236px rail 用墨色 `#1b1812` 铺底、纸色文字；当前项翻成纸色印章块 +
  朱红小方。这是页面里唯一的大块深色——视线先落在「我在哪」，再落到纸页内容。
  二级导航（项目内概览/任务）同样是墨底、36px 行高、半透明纸底表示 active。
- **纸页主列**：内容区保持纸色；靠 2px 墨线（页头、结论开篇）与墨条表头建立章节，
  一级标题 38px serif、gutter 朱红编号、正文 68ch；KPI 从卡片降为页眉注记（无边框，
  1px 底线）。表头是一条墨条（纸色文字），使数据区在纸页上成为最重的"证据块"。
- **两级导航**：首页只放「工作台 / 项目」；进入项目后 rail 才展开项目内「概览 / 任务」，
  「← 全部项目」收起。移动端 ≤900 同理：先一级导航，进入项目后追加二级。
- **任务结构**：每个项目默认 `ods/dwd/dwm/ads` 四层文件夹；右键文件夹出菜单
  （新建任务 / 新建子文件夹 / 重命名 / 删除）。新建任务先选类型——SQL 可运行；
  PySpark 只保存代码与版本，执行器未接入必须写明。任务树是纸页左侧索引：
  30px 行高、发丝线缩进、类型点（SQL 橄榄方 / PySpark 赭石圆），不做卡片。
- **对话框**：纸底 + 2px 墨顶线 + 深投影，背景压暗（不是玻璃）；右键菜单纸底 +
  1px 墨线 + 轻投影，出现位置贴边收拢。

## 随行智能体（v10，2026-10-23）

- **悬浮印章**：右下 54px 朱红实心方块（`#a83a2a`）+ 纸色 serif「问」，墨边 + 错位投影；
  这是页面里唯二的朱红实心块（另一个是主按钮 hover），闭合时左侧贴一枚 mono 纸签
  「随行智能体」。打开后印章翻成墨色（用 X 收起），抽屉从右侧浮出（416px 纸面 + 2px 墨顶线）。
- **对话抽屉**：轮次 = 墨底问题 + 左墨线回答；进度行 11.5px 带状态点（橄榄完成 /
  赭石纠偏 / 朱红失败）；结论正文 13.5px，SQL 折叠为 `<details>`，结果只给 6 行小表，
  完整明细走「在结果页打开」（回工作台并保留会话）。
- **归属**：抽屉按当前项目工作（同一 `X-Project-Id` 与 source）；切项目即清空对话、开新会话，
  避免把两个库的口径谈混。移动端 ≤900 收窄、≤640 变底部整幅（76vh），印章贴角。

## 数据开发工作台（v11，2026-10-23）

参考外部数据开发平台（DataLeap 深色 IDE 截图）后只借「结构」不借「皮肤」：项目内从卡片式两栏
改成 IDE 工作台，材质仍是暖纸墨线。

- **外壳**：`.page-ide` 占满 `100vh - topbar`，内部竖向 = 44px tab 条 + 1fr 工作区（264px 侧栏 + 编辑器）。
- **打开任务 tab 条**：纸 2 底、44px 行高；「概览」页签常驻最左，任务页签 35px 纸色块 + 2px 内阴影墨边，
  active 用纸色衬出；`×` 收起页签（关当前落到最后一个，全关回概览）；右侧刷新 + ＋新建任务。
- **左侧导航**：264px 纸色、右侧 1px 发丝线；顶部项目名与层级注记，搜索框输入即按名称过滤整棵树，
  下面沿用文件夹树（30px 行高、发丝缩进、类型点）；「＋ 新建任务」与搜索等宽、12.5px。
- **中间编辑器**：`.editor-bar`（纸 2 底 + 发丝下线）放任务名（serif 15、无边框，hover 才显纸块）、
  类型 badge、文件夹/版本 chip，右侧删除/运行/保存；下面 54px 行号槽（mono 11.5/22、纸 2 底、右发丝线）
  + 无边框 textarea（mono 12.5/22、padding 12/18），滚动同步行号；底栏 30px 注记条（类型提示、
  未保存朱红点、行数、Ctrl+Enter）。
- **底部面板**：34px 标签条（运行记录带朱红圆点 / 版本历史计数 / 任务信息），默认展开运行记录
  96px 空态，运行后换成结果表（沿用墨条表头）；历史与任务信息复用旧逻辑（回滚 = 用旧内容生成新版本；
  任务信息里可改文件夹/类型）。
- **概览页签**：项目首页——serif 34 项目名、描述、4 格统计（文件夹 / 任务 / SQL / PySpark），
  下方 1.35fr/1fr 两卡：最近任务（30px 行索引）与项目资料 +「打开项目概览 →」。
- **保留**：右键菜单、新建任务/文件夹/重命名/删除对话框、版本回滚、运行结果、错误提示全部沿用；
  后端与 API 未改，PySpark 仍只保存不执行。
- **验证**：axe 七态 + 独立探针（概览/编辑器/移动端）0 违规；编辑器 54px gutter + 886px 正文、22px 行高；
  ≤900 单列（侧栏 38vh）、≤640 紧凑、无横向溢出；像素统计纸色 82%、墨色 16%、暖 R−B≈18、朱红 0.19%。

## SQL 编辑器补全（v12，2026-10-26）

任务编辑器从 textarea 换成 CodeMirror 6（`apps/web/frontend/src/components/SqlEditor.tsx`），
材质仍走暖纸墨线：纸底墨字、mono 12.5/22 行高、54px 纸 2 色行号槽 + 右发丝线；
关键字加粗墨色、字符串橄榄、数字赭石、注释墨 3 斜体，补全浮层为纸面 + 墨边，
选中项墨底纸字。

- **补全**：`Ctrl+Space`（或输入触发）弹出——SQL 关键字/函数、数据源表名
  （detail 显示列数、info 显示表注释）、FROM/JOIN 引用表的列（detail 显示类型、
  info 显示类型/可空/主键/注释）、`别名.` 只列该表列；主键列排序加权。
- **数据来源**：`GET /api/tables`（引擎默认数据源，与任务运行同一源），进入任务
  面板加载一次；不新增后端接口、不调 LLM，补全确定性、零额外费用。
- **行为不变**：Ctrl+Enter 保存、Tab/Shift+Tab 缩进、未保存切换确认、beforeunload
  提醒全部保留；编辑器按需懒加载（`SqlEditor` 独立 chunk，不拖慢首屏）。
- **边界**：高亮不重排文本、不改写用户大小写；PySpark 任务只用高亮/括号闭合，
  不投喂 SQL 补全数据；未知表/列见下节 lint。
- **验证**：tsc 0、build 通过（主包 gzip 109.88kB，编辑器 chunk gzip 126.33kB 懒加载）；
  E2E axe 七态 0、运行时 0、失败请求 0；IDE 探针（CM 几何、gutter 54px、无重叠）、
  dirty_probe 13 项、移动端单列全部通过。

### 二期：DDL 诊断 + 格式化（v12.1，2026-10-26）

- **DDL 诊断**（`apps/web/frontend/src/components/sqlLint.ts`，`@codemirror/lint`
  的 `linter(callback, { delay: 500 })`）：按 SQL 语法树逐个 `Statement` 扫
  `FROM/JOIN` 表引用（支持 `schema.表`）与「别名/表名.列」限定列；字符串、`--`
  与 `/* */` 注释先等长遮罩，避免误报；`WITH` 定义的 CTE 视为「列不可知」跳过；
  裸列、函数、子查询一律不报（宁少报不误报）。表结构为空时关闭。
- **呈现**：未知表 / 未知限定列用赭石波浪线（`text-decoration: underline wavy`），
  悬停浮层为纸面 + 墨边，左侧 3px 赭石条，文案「未知表「x」：当前数据源里没有
  这张表」「未知列「a.b」：t 没有这一列」；错误级保留朱红（本期不产生）。
- **格式化**：工具条「格式化」按钮或 `Ctrl+Shift+F`，按需 `import('sql-formatter')`
  （sqlite 方言、关键字大写、2 空格缩进、`expressionWidth 56`、多语句空行），
  格式化后 `setSql` + 标脏并提示「已格式化，保存后生效」；PySpark 任务按钮禁用并
  提示「暂不支持格式化」。
- **验证**：`_tmp/e2e/lint_probe.mjs`——未知表 + 未知列各 1 条波浪线、悬停文案正确、
  格式化后 `SELECT/FROM` 大写 + 多行缩进 + 脏标记，axe 0、运行时 0；全量 E2E
  axe 七态 0、ide_probe/dirty_probe/completion_probe 全绿；构建主包 gzip 110.27kB、
  编辑器 chunk gzip 134.25kB、sql-formatter chunk gzip 76.33kB（均按需懒加载）。


## 受控写（P1，2026-10-26）

写入任务不是「SQL 控制台」：只允许单表 INSERT / UPDATE / DELETE，且必须先预演、确认后才落库。
写权限默认关闭，写目标只能是项目自己绑定的 SQLite 测试库；问答链路、只读 SQL 任务、
随行智能体全部保持原样。

### 四层防线

1. **守门** `nl2sql/guard/write.py`：单条、≤20000 字符；禁 DDL / TRUNCATE / 多语句 /
   恒真 WHERE 等；INSERT 只认 `INSERT INTO 表 (列) VALUES …`，UPDATE / DELETE 必须带
   WHERE；`sqlite_` 前缀与 blocked_tables 拒绝。保存任务与预演两端都过这道门（双层）。
2. **两阶段执行** `apps/web/backend/write_runner.py`：`BEGIN IMMEDIATE` 事务里真执行，
   统计影响行数并在回滚前取样 before / after；超过项目 `max_affected` 直接回滚并报
   `affected_limit`；预演结束 ROLLBACK，提交才 COMMIT。
3. **一次性凭据**：预演凭据存任务库 `write_dry_run`（`wd-*`，5 分钟过期）；提交时校验
   项目 / 任务 / revision / SQL 未变（变过 409 并作废），提交成功即删除；任务库新增
   `write_audit` 只追加审计。
4. **读写分离**：写执行器用独立 `sqlite3.connect`，绕开只读适配器；项目设置
   `[data_write] enabled / source_id / max_affected`（上限 1–1000，默认 100），开启需
   在界面上输入项目名二次确认。

### 界面（暖纸墨线）

- 任务类型新增「写入（受控写）」：编辑条类型 badge 用朱红 `.badge-write`，树里的点与
  标签为朱红 `wr`，概览统计从 4 格变 5 格（新增写入任务）。
- 运行按钮变「预演写入」（SQL 未保存时禁用）→ 底部面板展示「预演通过 · 将影响 N 行 ·
  上限 M」+ 执行前 / 执行后两栏样本（mono 表、sticky 表头）+ 朱红「确认提交」；被拒 /
  超限用 `.alert-bad` 给出原因与规则代码。
- 底部面板新增「写入审计」tab（仅写任务）：时间 / 状态 / 操作 / 表 / 行数 / 任务 /
  SQL 或原因；状态 badge 预演 = 赭石、已提交 = 橄榄、拒绝 / 失败 = 朱红。
- 项目详情新增「受控写」section：默认关闭；开启要输入项目名，写目标只列 SQLite 数据源，
  影响行数上限 1–1000，保存后顶部显示「已开启」朱红 badge 与绑定值。

### 边界

- 不含 DDL / 事务脚本 / 存储过程；SQLite 以外的引擎暂不支持写入（接口 400 明确说明）。
- PySpark 仍只保存不执行；写任务不能点「运行」，必须预演。
- 审计表在项目任务库内，随项目一起删除；不含业务库审计。

### 验证

- `tests/test_write_guard.py` 62 checks；`tests/test_write_api.py` 全 PASS（默认关闭 403、
  预演不落库、提交落库、一次性凭据、改 SQL 后 409、affected_limit、审计 preview /
  committed / failed、写入目标不能是任务库）。
- `_tmp/e2e/write_probe.mjs`（临时项目 + 临时 sqlite）：项目设置面板、写入任务树标、
  预演前后样本、确认提交（DB 中 2 行变 paid）、写入审计表、无 WHERE 保存被拒、超上限
  affected_limit；axe 五态 0 违规、运行时 0。全量 E2E（product / ide / dirty /
  completion / lint）保持全绿；tsc 0、build 通过（主包 gzip 113.22kB、编辑器 chunk
  gzip 134.25kB、sql-formatter chunk gzip 76.33kB）。

## 原生调度编排（P1，2026-10-28）

工作流是「任务」的第四种类型（`kind='workflow'`）。正文仍是任务表里的 `sql`
字段，但语义换成一段 JSON：`{"steps":[{"key","task_id","depends_on"}]}`。
服务端把「步骤」当唯一事实：保存时校验结构、键唯一、依赖存在、无环，
最多 20 步；执行时按拓扑排序串行跑，失败即停，且只允许引用本项目里的只读
SQL 任务（write / PySpark / 另一个工作流都不能进步骤）。

**模型与存储**：项目自己的 `state/tasks.db` 里新增三张表——
`workflow_schedule`（cron / enabled / next_run_at / last_run_at）、
`workflow_run`（run_id / trigger / status / 起止时间 / error）、
`workflow_run_step`（每步 task_id / status / rows / error）。实例与调度跟随
项目库，不引入外部调度器或消息队列。

**触发**：手动（编辑器上方「运行工作流」）、重跑（实例展开里的「重跑这次
实例」，trigger=rerun）、cron（后台 `WorkflowScheduler` 线程每 15s 扫
`next_run_at <= now` 的启用调度，先推进下次再触发）。同一工作流用进程内
集合 + 库里的 running 双去重，同一时间只跑一个实例。cron 用 APScheduler 的
`CronTrigger.from_crontab` 校验与算下次时间（5 段、本地时区），只当解析器，
不用它的调度器/存储。

**界面**（暖纸墨线不变）：新建任务类型里加「工作流（调度编排，先加步骤）」；
编辑器换成步骤列表——每行是「步骤键 + 任务下拉 + 依赖 chip」，依赖用 chip
切换，正文由组件序列化，不用手写 JSON；底栏提示「运行实例 / 调度」入口。
底部面板为工作流改成「运行实例 / 调度 / 任务信息」：运行实例列表带状态
徽章、触发方式、时间、成功步数，点行展开每步明细；调度页填 cron、勾启用、
保存后显示下次/上次触发。概览统计变为 6 格（多了工作流计数）。

**边界**：这一期只编排「只读 SQL 任务」，不包含同步/Shell/PySpark 执行器，
也不做并行、补数区间、DAG 画布；写任务与工作流不能互相嵌套。

**验证**：`tests/test_workflow.py`（定义校验、手动/重跑、调度、去重、假时钟
tick 触发）；`_tmp/e2e/workflow_probe.mjs` 覆盖编辑器/依赖切换/保存/cron/手动
运行/实例步骤明细，axe 三态 0 违规；全量 E2E 与既有任务流程保持 0 冲突。

## 原生同步（P1，2026-10-28；P1.1 支持 MySQL 目标）

目标：在本地单用户工作台里落地「把 A 库一张表搬到 B 库」的最小闭环，复用只读适配器与
受控写防线，不做自由 SQL 控制台。

**模型**：任务类型再加一种「数据同步」（`kind=sync`），正文是 JSON 搬运配置：
`{source_id, table, target_table?, mode: "full"|"incremental", watermark?}`。配置只描述
表与模式，不存 SQL；`target_table` 缺省与 `table` 同名；增量模式必须有水位列。

**存储**（项目 tasks.db）：
- `sync_state(task_id, watermark, rows, updated_at)`：上次提交的水位与行数；
- `sync_preview(...)`：5 分钟一次性预演凭据（含 revision），提交时校验任务未被改动；
- `sync_event(...)`：预演 / 提交 / 失败审计，含 mode、源表、目标表、行数、水位前后、错误。

**流程**：
1. 预演：用只读适配器读源表（`SELECT *`；增量加 `WHERE wm > 上次水位 ORDER BY wm`），
   统计行数与样本（最多 20 行），判断目标表是否存在、目标库当前行数，不写目标库；
   行数超过项目 `data_write.max_affected` 即拒绝（`affected_limit`）。
2. 确认提交：重新读源表核对行数/水位未变（`source_changed`），`BEGIN IMMEDIATE`
   事务内按源列类型自动建表（`INTEGER/REAL/TEXT`）、全量先清空目标表、批量写入，
   超上限回滚；成功后保存水位与行数并写审计。
3. 目标库为项目「受控写」绑定的数据源：本地 SQLite 测试库（`BEGIN IMMEDIATE` 事务），
   或 **MySQL 实验库**（pymysql + 项目 secrets 里的独立账号，`CREATE TABLE IF NOT EXISTS`
   + 全量 `DELETE` / 批量 `INSERT` 同一事务；系统库与任务库直接拒绝）。读取可以是
   SQLite / MySQL / PostgreSQL，读取永远只读。注意：SQL 写入任务（write）仍只支持
   SQLite——同步是本期唯一放开到 MySQL 的写路径。
   增量首跑（水位为空）时若目标表已有数据，拒绝并提示先全量初始化
   （`incremental_needs_init`），避免把整表追加成重复行。

**界面**：同步任务的编辑器不是 SQL textarea，而是 `SyncEditor` 表单（源数据源 /
源表 / 目标表 / 模式 / 水位列 + 写目标提示与「查看配置 JSON」）；编辑器条按钮为
「预演同步」；底部面板「同步记录」显示预演结果（行数、模式、水位、源表样本、目标库
现状）与「确认提交」，另有「同步审计」表；概览统计变为 7 格。

**边界**：只做表级全量 / 增量搬运；目标只能是 SQLite 写目标或专用 MySQL 实验库；
不做字段映射、Upsert、DDL 变更同步与多表事务；同步不会改变只读查询防线，问答链路
永远只读。

**验证**：`tests/test_sync.py` 覆盖配置校验、全量、增量水位、防线（未开写 / 超上限 /
预演后改任务 409 / 目标源不存在）与审计；`tests/test_sync_mysql.py`（需要
`NL2SQL_TEST_MYSQL_*` 环境变量，缺省 SKIP、不提交密码）覆盖 MySQL 自动建表、全量覆盖、
增量累积与首跑重复保护、防线；`_tmp/e2e/sync_probe.mjs` 覆盖树标识、表单、预演、确认
提交、审计与 axe 三态（sync_editor / sync_preview / sync_audit 均 0 违规），并在
MySQL 实验库目标上实跑通过；全量浏览器回归七探针保持一致。
