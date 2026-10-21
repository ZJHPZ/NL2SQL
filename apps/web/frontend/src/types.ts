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
