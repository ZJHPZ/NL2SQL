/** 与后端交互的类型定义。
 *
 * 与 `apps/web/backend/schemas.py` 一一对应。
 * 刻意手写而不做代码生成：字段不多，且"后端的哪个字段对应界面的哪个决策"
 * 是有设计意图的（见字段旁注释），自动生成会把这个意图抹掉。
 */

export type QueryStatus =
  | 'ok'
  | 'empty'
  | 'truncated'
  | 'unresolved'
  | 'invalid_sql'
  | 'blocked'
  | 'timeout'
  | 'error'
  /** 智能体主动追问、等待用户回答。**这是正常状态而非错误** */
  | 'need_clarification'

export interface ResultPayload {
  columns: string[]
  rows: unknown[][]
  row_count: number
  truncated: boolean
  elapsed_ms: number
  data_time: string | null
  cached: boolean
  warnings: string[]
}

export interface QueryResponse {
  status: QueryStatus
  question: string
  source_id: string
  sql: string | null
  result: ResultPayload | null

  error_code: string | null
  message: string | null
  /** 可操作建议 —— 界面的"下一步该做什么"就靠它 */
  suggestions: string[]
  /** 候选字段，列名不存在时给出，可点击替换 */
  candidates: string[]
  /** 生效的过滤条件，结果为空时回显，让用户知道"是条件筛掉了" */
  applied_filters: string[]
  /** 来源标注、口径未验证提示 —— 必须显著展示，不能静默 */
  notes: string[]

  plan_fingerprint: string | null
  cached: boolean
  elapsed_ms: number
  data_time: string | null
  trace: Record<string, number>

  ok: boolean
  planner_mode: string
  /** 本轮会话标识。**下一轮必须带上**，否则 agent 的澄清上下文会断 */
  session_id: string | null
  /** agent 的散文回答（含真实数字）。与 result 互补：散文给结论，表格给明细 */
  answer: string | null
}

export interface SafetyInfo {
  read_only: boolean
  max_rows: number
  timeout_s: number
  forbid_multiple_statements: boolean
  forbid_ddl_dml: boolean
}

/** 执行器信息：同一套判断，两种调度方式。
 *
 * 为什么要在界面上露出：agent 模式下"你这句话是怎么被跑出来的"
 * 直接影响可解释性。用户有权知道是自写循环还是 LangGraph 在调度。
 */
export interface RuntimeInfo {
  name?: string
  /** 请求的执行器（可能被降级成别的） */
  requested?: string
  /** 非空 = 发生了降级，内容是"缺什么"。必须显著提示，不能静默 */
  note?: string
  available?: Record<string, boolean>
  /** 图执行器的节点与边（纯数据，界面可据此画图） */
  graph?: {
    name?: string
    version?: string
    nodes?: { name: string; label?: string }[]
    edges?: {
      source: string
      target: string
      condition?: string | null
    }[]
  }
  error?: string
}

export interface InfoResponse {
  version: string
  planner_mode: string
  llm_available: boolean
  llm_model: string
  sources: string[]
  default_source: string | null
  capabilities: Record<string, unknown>
  safety: SafetyInfo
  cache: Record<string, unknown>
  knowledge: {
    enabled?: boolean
    total?: number
    enabled_count?: number
    by_type?: Record<string, number>
  }
  snapshot: {
    fingerprint?: string
    captured_at?: string
    table_count?: number
  }
  /** 语义层来源与可移植性。用 dbt metrics.yml 时靠它说清"哪部分搬不走" */
  semantic: {
    source?: string
    base_table?: string
    metrics?: number
    dimensions?: number
    /** 口径可移植性 0~1。1 = 拿回 dbt 也能跑出同样的数 */
    portability?: number
    native_metrics?: string[]
    annotated_metrics?: string[]
    /** dbt 里没有等价物的指标（窗口指标等）——界面要标出来 */
    structural_metrics?: string[]
    extension_keys?: string[]
    rejected_metrics?: string[]
    warnings?: string[]
  }
  /** 引擎是否装配成功。false 时看 startup_warnings */
  engine_ready: boolean
  /** 启动期问题（含引擎装配失败的原因）。界面必须显示，否则用户无从下手 */
  startup_warnings: string[]
  /** 实际生效的执行器（agent 模式才有图信息） */
  runtime: RuntimeInfo
}

export interface ColumnInfo {
  name: string
  data_type: string
  nullable: boolean
  comment: string
  is_primary_key: boolean
  /** 生成列不能出现在 INSERT 里，且部分库不允许直接写 —— 界面上要标出来 */
  generated: boolean
}

export interface TableInfo {
  name: string
  comment: string
  row_estimate: number | null
  column_count: number
  columns: ColumnInfo[]
  foreign_keys: string[][]
}

export interface KnowledgeItem {
  key: string
  type: string
  title: string
  body: string
  source_id: string | null
  tags: string[]
  enabled: boolean
  origin: string
  source_ref: string
  version: number
  created_at: string
  updated_at: string
}

export interface KnowledgeHit {
  key: string
  type: string
  title: string
  body: string
  score: number
  /** 命中来源与名次，如 `bm25#1+vector#3` —— 测试检索的价值就在这个字段 */
  why: string
}

export interface HistoryItem {
  id: string
  source_id: string
  question: string
  sql: string
  status: string
  row_count: number
  elapsed_ms: number
  created_at: string
  error_code: string | null
  error_message: string | null
  plan_fingerprint: string
  meta: Record<string, unknown>
}

// ================================================================ M7 数据开发工作台
// 契约正文：_tmp/m7_api_contract.md。字段名与后端保持 snake_case 一致，
// 不做驼峰转换：契约是前后端共同的准绳，少一层映射就少一处对不上的可能。

/** 数据源适配器。`postgres` 是契约里的写法（不是 postgresql） */
export type SourceAdapter = 'sqlite' | 'mysql' | 'postgres' | 'jdbc'

/** 数据源回传信息。
 * **口令永远不回传**：只有 has_password 告诉界面"有没有配过密码"。
 */
export interface SourceOut {
  source_id: string
  adapter: SourceAdapter | string
  label: string
  path: string
  host: string
  port: number
  user: string
  database: string
  has_password: boolean
  driver_class: string
  jdbc_url: string
  jar_paths: string[]
}

/** 新建/覆盖数据源的表单载荷（POST /sources 与 /sources/test 共用） */
export interface SourceIn {
  adapter: SourceAdapter | string
  label?: string
  source_id?: string
  path?: string
  host?: string
  port?: number
  user?: string
  database?: string
  /** 只在保存时发送；编辑已有数据源时留空表示"不改密码" */
  password?: string
  driver_class?: string
  jdbc_url?: string
  jar_paths?: string[]
  read_only?: boolean
}

/** 测试连接返回的列预览 */
export interface SourceColumnPreview {
  name: string
  data_type: string
  nullable: boolean
}

/** 测试连接返回的表预览（最多 100 张 / 每张 200 列） */
export interface SourceTablePreview {
  name: string
  row_estimate: number | null
  columns: SourceColumnPreview[]
}

/** POST /api/projects/{pid}/sources/test 的返回。
 * 连接失败是业务结果（ok:false），HTTP 仍是 200，不能当异常处理。
 */
export interface SourceTestResult {
  ok: boolean
  message: string
  dialect: string
  elapsed_ms: number
  tables: SourceTablePreview[]
}

/** GET /api/projects/{pid}/sources/{sid}/tables */
export interface SourceTablesResult {
  source_id: string
  tables: SourceTablePreview[]
  truncated: boolean
}

/** POST /api/projects/{pid}/sources/{sid}/snapshot */
export interface SourceSnapshotResult {
  table_count: number
  fingerprint: string
  warnings: string[]
}

/** 项目级「受控写」设置：默认关闭，写目标只能是本项目绑定的 SQLite 测试库 */
export interface DataWriteSettings {
  enabled: boolean
  source_id: string
  max_affected: number
}

/** 项目摘要与详情同构：详情只是 sources 更完整。 */
export interface ProjectSummary {
  id: string
  name: string
  description: string
  /** "golden" | "dbt" | "none" */
  semantic: string
  /** semantic=dbt 时是 dbt metrics.yml 路径 */
  metrics_path: string
  sources: SourceOut[]
  /** 受控写设置（详情接口才有；列表可能是默认值） */
  data_write?: DataWriteSettings
  created_at: string
  updated_at: string
  /** default 项目不可删除/改名（后端 400 兜底，界面也置灰） */
  is_default: boolean
  task_count: number
}

export type ProjectDetail = ProjectSummary

export interface ProjectListResponse {
  default_project_id: string
  projects: ProjectSummary[]
}

export interface ProjectCreateIn {
  name: string
  description?: string
}

export interface ProjectUpdateIn {
  name?: string
  description?: string
  semantic?: string
  metrics_path?: string
  data_write?: DataWriteSettings
}

/** JDBC 驱动状态。kind ∈ sqlite / mysql / postgresql / mssql */
export interface DriverInfo {
  kind: string
  name: string
  maven: string
  downloaded: boolean
  /** 已下载时的路径（相对仓库根或绝对路径，原样展示） */
  path: string
}

export interface DriversResponse {
  drivers: DriverInfo[]
}

export interface DriverDownloadResult {
  ok: boolean
  path?: string
  message?: string
}

/** 任务列表项 */
export interface TaskSummary {
  task_id: string
  name: string
  description: string
  /** 所属文件夹路径（如 ods/原始层），空串 = 根目录 */
  folder: string
  /** "sql" | "pyspark" | "write" | "workflow" | "sync"；PySpark 任务只能保存/版本，暂不支持运行 */
  kind: string
  status: string
  /** 前若干字符，列表里做一行预览 */
  sql_preview: string
  revision: number
  updated_at: string
  updated_by: string
}

/** 任务详情 */
export interface TaskDetail {
  task_id: string
  project_id: string
  name: string
  description: string
  sql: string
  folder: string
  kind: string
  status: string
  created_by: string
  updated_by: string
  created_at: string
  updated_at: string
  revision: number
}

/** 任务文件夹（项目内是一棵树，路径用 '/' 分段） */
export interface TaskFolder {
  path: string
  created_by: string
  created_at: string
  task_count: number
}

export interface TaskFoldersResponse {
  project_id?: string
  folders: TaskFolder[]
  /** 首次进入自动创建的四层目录：ods / dwd / dwm / ads */
  defaults?: string[]
}

export interface TaskFolderMutationResult {
  ok: boolean
  folders: TaskFolder[]
  folder?: { path: string; renamed_from?: string }
  /** 删除时被一并移除的空子目录数量 */
  removed?: number
}

/** POST /api/projects/{pid}/tasks/{tid}/move 的返回：只改归属，不生成新版本 */
export interface TaskMoveResult {
  ok: boolean
  task: TaskDetail
}

/** 版本记录。AI 修改（author="ai"）要在界面上有角标 */
export interface TaskRevision {
  revision: number
  /** "user" | "ai" */
  author: string
  note: string
  created_at: string
  sql: string
}

export type RevisionOut = TaskRevision

export interface TaskListResponse {
  tasks: TaskSummary[]
}

/** GET /api/projects/{pid}/tasks/{tid} 的返回：详情 + 版本历史一次给全 */
export interface TaskDetailResponse {
  task: TaskDetail
  revisions: TaskRevision[]
}

export interface TaskCreateResult {
  task: TaskDetail
  revision: number
}

export interface TaskUpdateResult {
  task: TaskDetail
  revision: number
}

export interface TaskRevisionsResponse {
  revisions: TaskRevision[]
}

/** 任务运行的结果集。
 * 比 QueryResponse.result 窄：没有 data_time/cached/warnings，
 * 展示前由 TasksPanel 补齐成 ResultPayload 再交给 ResultTable。
 */
export interface TaskRunResultPayload {
  columns: string[]
  rows: unknown[][]
  row_count: number
  truncated?: boolean
  elapsed_ms?: number
}

/** POST /api/projects/{pid}/tasks/{tid}/run 的返回。
 * 只读守卫拒绝时 ok:false / status:"blocked"，HTTP 仍是 200。
 */
export interface TaskRunResponse {
  ok: boolean
  status: string
  sql: string
  result: TaskRunResultPayload | null
  message: string
}

// ---------------------------------------------------------------- 受控写（P1）
/** dry-run / commit 共用的前后样本行（键值来自数据表列名） */
export type WriteSampleRow = Record<string, unknown>

/** POST .../write/dry-run 的返回。
 * status: preview（拿到一次性凭据）/ blocked（守门拒绝）/ failed（执行失败）。
 * 后两者 HTTP 仍是 200，reason/rule 说明原因。
 */
export interface WriteDryRunResponse {
  status: 'preview' | 'blocked' | 'failed'
  reason?: string
  rule?: string
  sql?: string
  kind?: string
  table?: string
  rows?: number
  before?: WriteSampleRow[]
  after?: WriteSampleRow[]
  dry_run_id?: string
  expires_at?: string
  audit_id?: number
  task?: TaskSummary
  max_affected?: number
}

/** POST .../write/commit 的返回 */
export interface WriteCommitResponse {
  status: 'committed' | 'blocked' | 'failed'
  reason?: string
  rule?: string
  rows?: number
  kind?: string
  table?: string
  before?: WriteSampleRow[]
  audit_id?: number
  task?: TaskSummary
}

/** 写入审计记录（GET /api/projects/{pid}/write-audit） */
export interface WriteAuditEntry {
  audit_id: number
  project_id: string
  task_id: string
  task_name: string
  revision: number
  source_id: string
  /** insert / update / delete */
  kind: string
  table_name: string
  sql: string
  /** preview | committed | rejected | failed */
  status: string
  rows: number
  error: string
  created_at: string
}

export interface WriteAuditResponse {
  project_id: string
  audit: WriteAuditEntry[]
}

// ---------------------------------------------------------------- 调度编排（P1）
/** 工作流里的一个步骤：引用一个只读 SQL 任务，按 depends_on 决定先后 */
export interface WorkflowStep {
  /** 本工作流内唯一，如 s1 / s2；只用于依赖引用 */
  key: string
  /** 被引用的任务 ID（必须是本项目的只读 SQL 任务） */
  task_id: string
  /** 依赖的前置步骤 key；空数组 = 可直接开始 */
  depends_on: string[]
}

/** GET/PUT .../schedule：工作流的 cron 调度配置 */
export interface WorkflowSchedule {
  task_id: string
  /** 5 段 cron（分 时 日 月 周），本地时区 */
  cron: string
  enabled: boolean
  /** 下一次自动触发时间（ISO 字符串，空 = 未排期） */
  next_run_at: string
  last_run_at: string
  /** 当前是否有实例在跑 */
  running: boolean
  running_run_id: string
}

/** 实例里的一个步骤 */
export interface WorkflowRunStep {
  run_id: string
  step_key: string
  task_id: string
  task_name: string
  depends_on: string[]
  /** pending | running | success | failed | skipped */
  status: string
  rows: number
  error: string
  started_at: string
  finished_at: string
}

/** 一次工作流运行实例 */
export interface WorkflowRun {
  run_id: string
  task_id: string
  project_id: string
  /** manual | cron | rerun */
  trigger: string
  /** running | success | failed */
  status: string
  started_at: string
  finished_at: string
  error: string
  total_steps: number
  succeeded_steps: number
  failed_steps: number
  steps?: WorkflowRunStep[]
}

export interface WorkflowRunsResponse {
  task_id: string
  running: boolean
  running_run_id: string
  runs: WorkflowRun[]
}

export interface WorkflowRunDetailResponse {
  task_id: string
  running: boolean
  running_run_id: string
  runs: WorkflowRun[]
  run: WorkflowRun
}

/** POST .../workflow/run 与 .../runs/{run_id}/rerun 的返回 */
export interface WorkflowRunnerResponse {
  status: string
  run_id: string
  task_id: string
  task: TaskSummary
}

// ---------------------------------------------------------------- 原生同步（P1）
/** 同步任务正文：把源库的一张表搬到项目写目标（SQLite） */
export interface SyncConfig {
  /** 读源（项目里绑定的数据源，只读连接） */
  source_id: string
  /** 源表名 */
  table: string
  /** 目标表名；缺省与源表同名 */
  target_table?: string
  /** full = 全量覆盖；incremental = 按水位列只搬新行 */
  mode: 'full' | 'incremental'
  /** 增量水位列（如 id / updated_at），incremental 必填 */
  watermark?: string
}

/** GET .../sync/info 的累计状态 */
export interface SyncState {
  task_id: string
  watermark: string
  rows: number
  updated_at: string
}

/** POST .../sync/preview：预演结果（不写目标库） */
export interface SyncPreviewResponse {
  /** preview | failed */
  status: string
  reason?: string
  rule?: string
  preview_id?: string
  expires_at?: string
  rows?: number
  columns?: string[]
  sample?: Record<string, unknown>[]
  source_id?: string
  source_table?: string
  target_table?: string
  target_exists?: boolean
  target_rows?: number
  watermark_before?: string
  watermark_after?: string
  mode?: string
  audit_id?: number
  task?: TaskSummary
  max_affected?: number
}

/** POST .../sync/commit */
export interface SyncCommitResponse {
  /** committed | failed */
  status: string
  reason?: string
  rule?: string
  rows?: number
  target_rows?: number
  target_table?: string
  created_table?: boolean
  watermark_after?: string
  audit_id?: number
  task?: TaskSummary
}

/** 一条同步审计记录 */
export interface SyncEventEntry {
  event_id: number
  project_id: string
  task_id: string
  task_name: string
  revision: number
  mode: string
  source_id: string
  source_table: string
  target_table: string
  /** preview | committed | failed */
  status: string
  rows: number
  error: string
  watermark_before: string
  watermark_after: string
  created_at: string
}

export interface SyncInfoResponse {
  task_id: string
  state: SyncState
  events: SyncEventEntry[]
}

// ---------------------------------------------------------------- 插件与工具管理
export interface ToolInfo {
  name: string
  description: string
  kind: string
  requires: string[]
  requires_approval: boolean
  /** "core" 或 "plugin:<name>" —— 让用户知道这个工具从哪来 */
  origin: string
  /** 用户是否启用（可切换） */
  enabled: boolean
  /** 是否真的注册进了运行时（取决于启用状态与能力是否齐全） */
  registered: boolean
  risk: string
  /** 缺哪些能力，导致它没被注册 */
  missing_capabilities: string[]
  /** 为什么没注册（给人看的一句话） */
  reason: string
}

export interface PluginInfo {
  name: string
  version: string
  description: string
  author: string
  path: string
  requires: string[]
  tools: string[]
  warnings: string[]
  /** 是否已加载（启用） */
  enabled: boolean
  /** 未启用时的原因 */
  reason?: string
}

export interface CapabilityInfo {
  name: string
  description: string
  risk: string
  /** 授予后解锁什么 —— 用户知情才能授权 */
  unlocks: string
  granted: boolean
}

/** 一份已加载的技能。`description` 是**选择依据**，必须显示出来 */
export interface SkillInfo {
  name: string
  description: string
  /** playbook（按需加载的流程）| planner-hint（改 system prompt，暂未启用） */
  kind: string
  requires: string[]
  /** 绑定的数据源；null = 全局适用 */
  scope: string | null
  /** project（随仓库）| external（默认不加载）| plugin:<name> */
  origin: string
  path: string
  applies_to: string[]
  body_chars: number
}

export interface SkillReport {
  roots?: string[]
  external_roots?: string[]
  loaded?: string[]
  skipped?: { name: string; reason: string }[]
  rejected?: { name: string; reason: string }[]
  warnings?: string[]
  /** 外置 Prompt 的加载问题（原先静默回退，现在透出） */
  prompt_issues?: string[]
}

export interface ToolsetInfo {
  tools: ToolInfo[]
  /** 已加载（启用）的插件 */
  plugins: PluginInfo[]
  /** 磁盘上存在但**未启用**的插件 —— 界面靠它提供"启用"入口 */
  plugins_available: PluginInfo[]
  plugins_enabled: string[]
  plugins_allow_all: boolean
  plugin_errors: string[]
  /** 第四块：已加载的技能 */
  skills: SkillInfo[]
  /** 技能的加载报告：谁没加载、为什么 */
  skill_report: SkillReport
  capabilities: CapabilityInfo[]
  disabled_tools: string[]
  safety: {
    read_only: boolean
    max_rows: number
    timeout_s: number
    /** 当前是否存在实现写操作的工具。假 ⇒ 授予 source.write 也不会解锁写库 */
    write_tools_implemented: boolean
    note: string
  }
  loop: Record<string, number>
  planner_mode: string
  version: string
}

// ---------------------------------------------------------------- 使用统计（M5）
/** 单个工具/技能的调用聚合 */
export interface ToolCallStat {
  calls: number
  failures: number
  failure_rate: number
  avg_ms: number
  p95_ms: number
}

/** 最近一次失败的归因（管理页用来看"现在坏在哪"） */
export interface RecentFailure {
  created_at: string
  source_id: string
  status: string
  question: string
  error_code: string | null
  elapsed_ms: number
  planner: string
  runtime: string
}

/** `GET /api/admin/stats` 的返回。口径见 docs/15：
 *  need_clarification 单列不算失败；by_status 九态零填充。 */
export interface AdminStats {
  total: number
  /** 九种 QueryStatus 零填充的计数 */
  by_status: Record<string, number>
  success: number
  clarify: number
  failure: number
  success_rate: number
  failure_rate: number
  cached: number
  cached_rate: number
  latency_ms: { p50: number; p95: number; avg: number }
  tools: {
    total: number
    failures: number
    failure_rate: number
    by_tool: Record<string, ToolCallStat>
  }
  recent_failures: RecentFailure[]
  since: string | null
  planner_mode: string
  runtime: string
}

// ================================================================ 流式问答（SSE）
/* `GET /api/query/stream` 的进度事件。
 *
 * 与 `nl2sql/agent/machine.py` 里的 emit 调用一一对应；`result` 帧的载荷
 * 就是 POST /api/query 的响应体（QueryResponse），因此两种调用可以共用渲染。
 * 事件不保证每轮都出现（例如纯主链路一步到位时只有 start/result/done）。
 */
export interface StreamStartEvent {
  question: string
  session_id: string | null
}

export interface StreamDecisionEvent {
  step: number
  /** tool = 调用工具；final = 给出结论；clarify = 追问 */
  kind: string
  tool?: string | null
  thought?: string | null
  elapsed_ms: number
}

export interface StreamToolEvent {
  step: number
  tool: string
  ok: boolean
  elapsed_ms: number
  /** 工具观察值摘要（后端截断到 400 字符） */
  observation?: string | null
}

export interface StreamGuardEvent {
  step: number
  /** nodata / narration / deferred / repeat —— 模型行为被机制顶回去 */
  rule: string
}

export interface StreamClarifyEvent {
  step: number
  question: string
}

export interface StreamOutcomeEvent {
  status: QueryStatus
  elapsed_ms: number
  notes: string[]
}

/** model_retry / model_repair / tool_missing 这类内部修复事件的公共形状 */
export interface StreamRepairEvent {
  step: number
  attempt?: number
  tool?: string
  reason?: string
  [key: string]: unknown
}

export type StreamEvent =
  | { event: 'start'; data: StreamStartEvent }
  | { event: 'decision'; data: StreamDecisionEvent }
  | { event: 'tool'; data: StreamToolEvent }
  | { event: 'guard'; data: StreamGuardEvent }
  | { event: 'clarify'; data: StreamClarifyEvent }
  | { event: 'outcome'; data: StreamOutcomeEvent }
  | { event: 'model_retry'; data: StreamRepairEvent }
  | { event: 'model_repair'; data: StreamRepairEvent }
  | { event: 'tool_missing'; data: StreamRepairEvent }
  | { event: 'result'; data: QueryResponse }
  | { event: 'done'; data: Record<string, unknown> }
  | { event: 'error'; data: { message: string } }
