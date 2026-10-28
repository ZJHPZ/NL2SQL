/** 后端接口封装。
 *
 * 一个刻意的做法：**错误统一转成可展示的对象，不往上抛异常**。
 * 理由与后端一致——这是个给个人用的查询工具，
 * "连接失败""问题无法解析"都是正常业务状态，不是程序异常。
 * 抛异常会让每个调用点都要写 try/catch，且容易漏掉导致白屏。
 */
import type {
  AdminStats,
  DriverDownloadResult,
  DriversResponse,
  HistoryItem,
  InfoResponse,
  KnowledgeHit,
  KnowledgeItem,
  ProjectCreateIn,
  ProjectDetail,
  ProjectListResponse,
  ProjectUpdateIn,
  QueryResponse,
  SourceIn,
  SourceOut,
  SourceSnapshotResult,
  SourceTablesResult,
  SourceTestResult,
  StreamEvent,
  SyncCommitResponse,
  SyncInfoResponse,
  SyncPreviewResponse,
  TableInfo,
  TaskCreateResult,
  TaskDetail,
  TaskDetailResponse,
  TaskFolderMutationResult,
  TaskFoldersResponse,
  TaskListResponse,
  TaskMoveResult,
  TaskRevisionsResponse,
  TaskRunResponse,
  TaskUpdateResult,
  ToolsetInfo,
  WorkflowRunDetailResponse,
  WorkflowRunnerResponse,
  WorkflowRunsResponse,
  WorkflowSchedule,
  WriteAuditResponse,
  WriteCommitResponse,
  WriteDryRunResponse,
} from './types'

export interface ApiError {
  error: string
  detail: string
  status: number
}

export type ApiResult<T> = { ok: true; data: T } | { ok: false; error: ApiError }

const BASE = ''

// ---------------------------------------------------------------- 项目上下文
/** 项目选择持久化在 localStorage：刷新后仍停留在同一个项目。
 * 每次请求现读而不是缓存在模块变量里，这样多标签页改选项目也能立刻生效。
 */
const PROJECT_STORAGE_KEY = 'talk-to-sql.project_id'
/** 契约规定的默认项目：任何情况下都必须有可用的兜底值 */
export const DEFAULT_PROJECT_ID = 'default'

export function getProjectId(): string {
  try {
    const saved = window.localStorage.getItem(PROJECT_STORAGE_KEY)
    return saved && saved.trim() ? saved : DEFAULT_PROJECT_ID
  } catch {
    // 隐私模式/被禁用时 localStorage 会抛异常，不能因此让整个界面不可用
    return DEFAULT_PROJECT_ID
  }
}

export function setProjectId(projectId: string): void {
  try {
    const id = projectId.trim() || DEFAULT_PROJECT_ID
    window.localStorage.setItem(PROJECT_STORAGE_KEY, id)
  } catch {
    /* 持久化失败可以接受：本次会话仍按内存中的项目走 */
  }
}

/** 把后端各种形态的错误体压成一句人话。
 * FastAPI 校验错误是 `{"detail":[{"loc":[...],"msg":"..."}]}`，
 * 直接 String() 会变成 “[object Object]”，用户看不懂。
 */
function readableDetail(body: unknown, fallback: string): string {
  if (body === null || body === undefined || body === '') return fallback
  if (typeof body === 'string') return body.trim() || fallback
  if (Array.isArray(body)) {
    const parts = body.map((item) => {
      if (item && typeof item === 'object') {
        const rec = item as Record<string, unknown>
        const loc = Array.isArray(rec.loc) ? rec.loc.map(String).join('.') : ''
        const msg = typeof rec.msg === 'string' ? rec.msg : JSON.stringify(rec)
        return loc ? `${loc}: ${msg}` : msg
      }
      return String(item)
    })
    return parts.filter(Boolean).join('；') || fallback
  }
  if (typeof body === 'object') {
    const rec = body as Record<string, unknown>
    if (typeof rec.detail === 'string') return rec.detail
    if (rec.detail !== undefined) return readableDetail(rec.detail, fallback)
    if (typeof rec.message === 'string') return rec.message
    if (typeof rec.error === 'string') return rec.error
    try {
      return JSON.stringify(rec)
    } catch {
      return fallback
    }
  }
  return String(body)
}

async function request<T>(
  path: string,
  init?: RequestInit,
): Promise<ApiResult<T>> {
  try {
    const headers = new Headers(init?.headers)
    headers.set('Content-Type', 'application/json')
    // 统一注入项目上下文：项目/任务接口以路径参数为准，其余接口靠这个头
    headers.set('X-Project-Id', getProjectId())
    const res = await fetch(BASE + path, { ...init, headers })
    const text = await res.text()
    let body: unknown = null
    try {
      body = text ? JSON.parse(text) : null
    } catch {
      body = null
    }
    if (!res.ok) {
      const b = (body ?? {}) as Record<string, unknown>
      return {
        ok: false,
        error: {
          error: typeof b.error === 'string' ? b.error : `http_${res.status}`,
          detail: readableDetail(
            b.detail ?? b.message ?? (body ?? text.slice(0, 300)),
            text.slice(0, 300) || `HTTP ${res.status}`,
          ),
          status: res.status,
        },
      }
    }
    return { ok: true, data: body as T }
  } catch (e) {
    // 网络层失败：后端没起来，或浏览器拒绝连接
    return {
      ok: false,
      error: {
        error: 'network_error',
        detail: e instanceof Error ? e.message : String(e),
        status: 0,
      },
    }
  }
}

export const api = {
  info: () => request<InfoResponse>('/api/info'),

  query: (payload: {
    question: string
    source_id?: string | null
    now?: string | null
    use_cache?: boolean | null
    session_id?: string | null
  }) =>
    request<QueryResponse>('/api/query', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  execute: (payload: { sql: string; source_id?: string | null }) =>
    request<QueryResponse>('/api/execute', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  tables: (sourceId?: string | null) =>
    request<TableInfo[]>(
      `/api/tables${sourceId ? `?source_id=${encodeURIComponent(sourceId)}` : ''}`,
    ),

  ddl: (sourceId?: string | null, maxTables = 20) =>
    request<{ ddl: string }>(
      `/api/ddl?max_tables=${maxTables}` +
        (sourceId ? `&source_id=${encodeURIComponent(sourceId)}` : ''),
    ),

  refreshSnapshot: (sourceId?: string | null, force = false) =>
    request<Record<string, unknown>>('/api/snapshot', {
      method: 'POST',
      body: JSON.stringify({ source_id: sourceId, force }),
    }),

  cacheStats: () => request<Record<string, unknown>>('/api/cache'),

  refreshCache: () =>
    request<{ removed: number }>('/api/cache/refresh', { method: 'POST' }),

  history: (sourceId?: string | null, limit = 50) =>
    request<HistoryItem[]>(
      `/api/history?limit=${limit}` +
        (sourceId ? `&source_id=${encodeURIComponent(sourceId)}` : ''),
    ),

  knowledgeList: (opts?: {
    sourceId?: string | null
    type?: string | null
    includeDisabled?: boolean
  }) => {
    const p = new URLSearchParams()
    if (opts?.sourceId) p.set('source_id', opts.sourceId)
    if (opts?.type) p.set('type', opts.type)
    if (opts?.includeDisabled) p.set('include_disabled', 'true')
    const qs = p.toString()
    return request<KnowledgeItem[]>(`/api/knowledge${qs ? `?${qs}` : ''}`)
  },

  knowledgeUpsert: (payload: {
    key?: string | null
    type: string
    title: string
    body: string
    source_id?: string | null
    tags?: string[]
    note?: string
  }) =>
    request<KnowledgeItem>('/api/knowledge', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  knowledgeToggle: (key: string, enabled: boolean) =>
    request<KnowledgeItem>(
      `/api/knowledge/${encodeURIComponent(key)}/toggle`,
      { method: 'POST', body: JSON.stringify({ enabled }) },
    ),

  knowledgeDelete: (key: string) =>
    request<{ deleted: string }>(
      `/api/knowledge/${encodeURIComponent(key)}`,
      { method: 'DELETE' },
    ),

  knowledgeTest: (payload: {
    text: string
    source_id?: string | null
    top_k?: number
  }) =>
    request<KnowledgeHit[]>('/api/knowledge/test', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  // ------------------------------------------------------------ 插件与工具管理
  adminTools: () => request<ToolsetInfo>('/api/admin/tools'),

  adminToggleTool: (name: string, enabled: boolean) =>
    request<{
      ok: boolean; name: string; enabled: boolean
      disabled: string[]; warning: string; restart_required: boolean; note: string
    }>('/api/admin/tools/toggle', {
      method: 'POST',
      body: JSON.stringify({ name, enabled }),
    }),

  adminTogglePlugin: (name: string, enabled: boolean) =>
    request<{
      ok: boolean; name: string; enabled: boolean
      plugins_enabled: string[]; warning: string; note: string
    }>('/api/admin/plugins/toggle', {
      method: 'POST',
      body: JSON.stringify({ name, enabled }),
    }),

  adminCapabilities: (payload: { grant?: string[]; revoke?: string[] }) =>
    request<{
      ok: boolean; capabilities: string[]; notice: string
      reload_required: boolean
    }>('/api/admin/capabilities', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  adminLoop: (payload: {
    max_steps?: number; max_repeat?: number; temperature?: number
  }) =>
    request<{ ok: boolean; loop: Record<string, number> }>('/api/admin/loop', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  adminConfig: () =>
    request<{ path: string; exists: boolean; text: string }>('/api/admin/config'),

  adminConfigSave: (text: string) =>
    request<{
      ok: boolean; path: string; applied: Record<string, unknown>; note: string
    }>('/api/admin/config', {
      method: 'POST',
      body: JSON.stringify({ text }),
    }),

  adminReload: () =>
    request<{
      ok: boolean; tools: string[]; plugin_errors: string[]; note: string
    }>('/api/admin/reload', { method: 'POST', body: '{}' }),

  /** 使用统计（M5）：回看最近 days 天 */
  adminStats: (days = 30) =>
    request<AdminStats>(`/api/admin/stats?days=${days}`),

  // ============================================================ M7 项目与数据源
  // 路径参数 {pid} 是准绳；X-Project-Id 头由 request() 统一注入。

  listProjects: () => request<ProjectListResponse>('/api/projects'),

  createProject: (payload: ProjectCreateIn) =>
    request<ProjectDetail>('/api/projects', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  getProject: (projectId: string) =>
    request<ProjectDetail>(`/api/projects/${encodeURIComponent(projectId)}`),

  updateProject: (projectId: string, payload: ProjectUpdateIn) =>
    request<ProjectDetail>(
      `/api/projects/${encodeURIComponent(projectId)}`,
      { method: 'PUT', body: JSON.stringify(payload) },
    ),

  deleteProject: (projectId: string) =>
    request<{ ok: boolean }>(
      `/api/projects/${encodeURIComponent(projectId)}`,
      { method: 'DELETE' },
    ),

  /** 新建或覆盖数据源（同 source_id 重复 → 覆盖，后端行为） */
  createSource: (projectId: string, payload: SourceIn) =>
    request<SourceOut>(
      `/api/projects/${encodeURIComponent(projectId)}/sources`,
      { method: 'POST', body: JSON.stringify(payload) },
    ),

  deleteSource: (projectId: string, sourceId: string) =>
    request<{ ok: boolean }>(
      `/api/projects/${encodeURIComponent(projectId)}/sources/` +
        encodeURIComponent(sourceId),
      { method: 'DELETE' },
    ),

  /** 测试连接（可不保存）。失败时 ok:false + message，HTTP 仍是 200 */
  testSource: (projectId: string, payload: SourceIn) =>
    request<SourceTestResult>(
      `/api/projects/${encodeURIComponent(projectId)}/sources/test`,
      { method: 'POST', body: JSON.stringify(payload) },
    ),

  sourceTables: (projectId: string, sourceId: string) =>
    request<SourceTablesResult>(
      `/api/projects/${encodeURIComponent(projectId)}/sources/` +
        `${encodeURIComponent(sourceId)}/tables`,
    ),

  /** 刷新 schema（重新抓元数据快照） */
  snapshotSource: (projectId: string, sourceId: string) =>
    request<SourceSnapshotResult>(
      `/api/projects/${encodeURIComponent(projectId)}/sources/` +
        `${encodeURIComponent(sourceId)}/snapshot`,
      { method: 'POST', body: '{}' },
    ),

  // ------------------------------------------------------------ JDBC 驱动
  jdbcDrivers: () => request<DriversResponse>('/api/jdbc/drivers'),

  downloadJdbcDriver: (kind: string) =>
    request<DriverDownloadResult>('/api/jdbc/drivers/download', {
      method: 'POST',
      body: JSON.stringify({ kind }),
    }),

  // ============================================================ M7 SQL 任务
  listTasks: (projectId: string) =>
    request<TaskListResponse>(
      `/api/projects/${encodeURIComponent(projectId)}/tasks`,
    ),

  createTask: (
    projectId: string,
    payload: {
      name: string
      sql: string
      description?: string
      folder?: string
      kind?: 'sql' | 'pyspark' | 'write' | 'workflow' | 'sync'
    },
  ) =>
    request<TaskCreateResult>(
      `/api/projects/${encodeURIComponent(projectId)}/tasks`,
      { method: 'POST', body: JSON.stringify(payload) },
    ),

  /** 详情接口一并返回版本历史：进详情不必再单拉一次 revisions */
  getTask: (projectId: string, taskId: string) =>
    request<TaskDetailResponse>(
      `/api/projects/${encodeURIComponent(projectId)}/tasks/` +
        encodeURIComponent(taskId),
    ),

  updateTask: (
    projectId: string,
    taskId: string,
    payload: {
      name?: string
      sql?: string
      description?: string
      folder?: string
      kind?: 'sql' | 'pyspark' | 'write' | 'workflow' | 'sync'
      note?: string
    },
  ) =>
    request<TaskUpdateResult>(
      `/api/projects/${encodeURIComponent(projectId)}/tasks/` +
        encodeURIComponent(taskId),
      { method: 'PUT', body: JSON.stringify(payload) },
    ),

  deleteTask: (projectId: string, taskId: string) =>
    request<{ ok: boolean }>(
      `/api/projects/${encodeURIComponent(projectId)}/tasks/` +
        encodeURIComponent(taskId),
      { method: 'DELETE' },
    ),

  taskRevisions: (projectId: string, taskId: string) =>
    request<TaskRevisionsResponse>(
      `/api/projects/${encodeURIComponent(projectId)}/tasks/` +
        `${encodeURIComponent(taskId)}/revisions`,
    ),

  /** 回滚 = 用旧版内容生成一个新版本（历史只增不改） */
  restoreTaskRevision: (
    projectId: string,
    taskId: string,
    revision: number,
  ) =>
    request<{ task: TaskDetail; revision: number; restored_from: number }>(
      `/api/projects/${encodeURIComponent(projectId)}/tasks/` +
        `${encodeURIComponent(taskId)}/revisions/${revision}/restore`,
      { method: 'POST', body: '{}' },
    ),

  /** 运行任务（只读守卫在服务端；被拒也是 200 + ok:false） */
  runTask: (projectId: string, taskId: string) =>
    request<TaskRunResponse>(
      `/api/projects/${encodeURIComponent(projectId)}/tasks/` +
        `${encodeURIComponent(taskId)}/run`,
      { method: 'POST', body: '{}' },
    ),

  /** 把任务挪到另一个文件夹：只改归属，不生成新版本 */
  moveTask: (projectId: string, taskId: string, folder: string) =>
    request<TaskMoveResult>(
      `/api/projects/${encodeURIComponent(projectId)}/tasks/` +
        `${encodeURIComponent(taskId)}/move`,
      { method: 'POST', body: JSON.stringify({ folder }) },
    ),

  // ------------------------------------------------------------ 受控写（P1）
  /** 预演写入：服务端在事务里真执行再回滚，返回前后样本与一次性凭据 */
  writeDryRun: (
    projectId: string,
    taskId: string,
    payload: { source_id?: string } = {},
  ) =>
    request<WriteDryRunResponse>(
      `/api/projects/${encodeURIComponent(projectId)}/tasks/` +
        `${encodeURIComponent(taskId)}/write/dry-run`,
      { method: 'POST', body: JSON.stringify(payload) },
    ),

  /** 凭预演凭据提交（confirm 必须显式 true） */
  writeCommit: (projectId: string, taskId: string, dryRunId: string) =>
    request<WriteCommitResponse>(
      `/api/projects/${encodeURIComponent(projectId)}/tasks/` +
        `${encodeURIComponent(taskId)}/write/commit`,
      {
        method: 'POST',
        body: JSON.stringify({ dry_run_id: dryRunId, confirm: true }),
      },
    ),

  writeAudit: (projectId: string, limit = 50) =>
    request<WriteAuditResponse>(
      `/api/projects/${encodeURIComponent(projectId)}/write-audit?limit=${limit}`,
    ),

  // ------------------------------------------------------------ 调度编排（P1）
  /** 手动触发一次工作流（后台线程按依赖顺序执行，返回 run_id 供轮询） */
  runWorkflow: (projectId: string, taskId: string) =>
    request<WorkflowRunnerResponse>(
      `/api/projects/${encodeURIComponent(projectId)}/tasks/` +
        `${encodeURIComponent(taskId)}/workflow/run`,
      { method: 'POST', body: '{}' },
    ),

  /** 重跑某次实例（复制同一版工作流定义，trigger=rerun） */
  rerunWorkflow: (projectId: string, taskId: string, runId: string) =>
    request<WorkflowRunnerResponse>(
      `/api/projects/${encodeURIComponent(projectId)}/tasks/` +
        `${encodeURIComponent(taskId)}/runs/${encodeURIComponent(runId)}/rerun`,
      { method: 'POST', body: '{}' },
    ),

  workflowRuns: (projectId: string, taskId: string, limit = 20) =>
    request<WorkflowRunsResponse>(
      `/api/projects/${encodeURIComponent(projectId)}/tasks/` +
        `${encodeURIComponent(taskId)}/runs?limit=${limit}`,
    ),

  workflowRun: (projectId: string, taskId: string, runId: string) =>
    request<WorkflowRunDetailResponse>(
      `/api/projects/${encodeURIComponent(projectId)}/tasks/` +
        `${encodeURIComponent(taskId)}/runs/${encodeURIComponent(runId)}`,
    ),

  workflowSchedule: (projectId: string, taskId: string) =>
    request<WorkflowSchedule>(
      `/api/projects/${encodeURIComponent(projectId)}/tasks/` +
        `${encodeURIComponent(taskId)}/schedule`,
    ),

  /** cron 用 5 段（分 时 日 月 周），服务端用 APScheduler 校验并算下次触发 */
  updateWorkflowSchedule: (
    projectId: string,
    taskId: string,
    payload: { cron: string; enabled: boolean },
  ) =>
    request<WorkflowSchedule>(
      `/api/projects/${encodeURIComponent(projectId)}/tasks/` +
        `${encodeURIComponent(taskId)}/schedule`,
      { method: 'PUT', body: JSON.stringify(payload) },
    ),

  // ------------------------------------------------------------ 原生同步（P1）
  /** 预演一次同步：事务里读源表算行数/样本，不写目标库 */
  syncPreview: (projectId: string, taskId: string) =>
    request<SyncPreviewResponse>(
      `/api/projects/${encodeURIComponent(projectId)}/tasks/` +
        `${encodeURIComponent(taskId)}/sync/preview`,
      { method: 'POST', body: '{}' },
    ),

  /** 确认执行：按预演凭据重读源表，行数/水位一致才写入目标库 */
  syncCommit: (projectId: string, taskId: string, previewId: string) =>
    request<SyncCommitResponse>(
      `/api/projects/${encodeURIComponent(projectId)}/tasks/` +
        `${encodeURIComponent(taskId)}/sync/commit`,
      {
        method: 'POST',
        body: JSON.stringify({ preview_id: previewId, confirm: true }),
      },
    ),

  syncInfo: (projectId: string, taskId: string) =>
    request<SyncInfoResponse>(
      `/api/projects/${encodeURIComponent(projectId)}/tasks/` +
        `${encodeURIComponent(taskId)}/sync/info`,
    ),

  // ------------------------------------------------------------ 任务文件夹
  taskFolders: (projectId: string) =>
    request<TaskFoldersResponse>(
      `/api/projects/${encodeURIComponent(projectId)}/task-folders`,
    ),

  createTaskFolder: (projectId: string, path: string) =>
    request<TaskFolderMutationResult>(
      `/api/projects/${encodeURIComponent(projectId)}/task-folders`,
      { method: 'POST', body: JSON.stringify({ path }) },
    ),

  renameTaskFolder: (projectId: string, path: string, newPath: string) =>
    request<TaskFolderMutationResult>(
      `/api/projects/${encodeURIComponent(projectId)}/task-folders`,
      { method: 'PUT', body: JSON.stringify({ path, new_path: newPath }) },
    ),

  deleteTaskFolder: (projectId: string, path: string) =>
    request<TaskFolderMutationResult>(
      `/api/projects/${encodeURIComponent(projectId)}/task-folders` +
        `?path=${encodeURIComponent(path)}`,
      { method: 'DELETE' },
    ),
}

// ================================================================ 流式问答（SSE）
/** 解析一个 SSE 帧（帧内若干 `event:` / `data:` 行，调用方已按空行切好）。 */
function parseSseFrame(raw: string): { event: string; data: unknown } | null {
  let event = 'message'
  const dataLines: string[] = []
  for (const line of raw.split(/\r?\n/)) {
    if (!line || line.startsWith(':')) continue // 注释行（心跳）忽略
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).replace(/^ /, '')) // 规范允许一个前导空格
    }
  }
  if (dataLines.length === 0) return null
  const text = dataLines.join('\n')
  try {
    return { event, data: JSON.parse(text) }
  } catch {
    return { event, data: text }
  }
}

export interface StreamQueryPayload {
  question: string
  source_id?: string | null
  now?: string | null
  use_cache?: boolean | null
  session_id?: string | null
}

export interface StreamQueryOptions {
  /** 每个进度事件回调一次；抛出异常不会影响主流程（这里兜住） */
  onEvent?: (ev: StreamEvent) => void
  /** 取消：abort() 后立即停止读取。后端查询线程仍会跑完（见后端注释），
   *  但界面不再等待结果——对用户来说"取消"就是立刻可继续操作。 */
  signal?: AbortSignal
}

/** 流式问答。
 *
 * 与 `request()` 的区别：响应是 `text/event-stream`，要自己读 body 逐帧解析，
 * 不能 `res.text()` 一次性等完（那样等于退化成非流式）。
 * 返回值与 POST /api/query 同构（`result` 帧的载荷），失败同样返回 ApiResult。
 */
export async function streamQuery(
  payload: StreamQueryPayload,
  opts: StreamQueryOptions = {},
): Promise<ApiResult<QueryResponse>> {
  try {
    const params = new URLSearchParams({ question: payload.question })
    if (payload.source_id) params.set('source_id', payload.source_id)
    if (payload.now) params.set('now', payload.now)
    if (payload.session_id) params.set('session_id', payload.session_id)
    if (payload.use_cache === false) params.set('use_cache', 'false')

    const res = await fetch(`${BASE}/api/query/stream?${params.toString()}`, {
      headers: { 'X-Project-Id': getProjectId() },
      signal: opts.signal,
    })
    if (!res.ok) {
      const text = await res.text().catch(() => '')
      return {
        ok: false,
        error: {
          error: `http_${res.status}`,
          detail: readableDetail(text.slice(0, 300), `HTTP ${res.status}`),
          status: res.status,
        },
      }
    }
    if (!res.body) {
      return {
        ok: false,
        error: {
          error: 'stream_unsupported',
          detail: '当前浏览器不支持流式响应（response.body 为空）',
          status: 0,
        },
      }
    }

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    let final: QueryResponse | null = null
    let streamError: string | null = null

    const dispatch = (frame: string) => {
      const parsed = parseSseFrame(frame)
      if (!parsed) return
      const ev = { event: parsed.event, data: parsed.data } as StreamEvent
      if (ev.event === 'result') final = ev.data
      else if (ev.event === 'error') {
        streamError = (ev.data as { message?: string } | undefined)?.message
          ?? 'stream_error'
      }
      try {
        opts.onEvent?.(ev)
      } catch {
        /* 观察者出错不影响结果解析 */
      }
    }

    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      let idx: number
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        dispatch(buf.slice(0, idx))
        buf = buf.slice(idx + 2)
      }
    }
    // 末尾可能没有空行分隔（服务端总是带 \n\n，但不要依赖它）
    if (buf.trim()) dispatch(buf)
    void reader.cancel().catch(() => undefined)

    if (streamError) {
      return {
        ok: false,
        error: { error: 'stream_error', detail: streamError, status: 200 },
      }
    }
    if (!final) {
      return {
        ok: false,
        error: {
          error: 'stream_incomplete',
          detail: '流式响应结束但未收到 result 帧（后端可能中途断开）',
          status: 200,
        },
      }
    }
    return { ok: true, data: final }
  } catch (e) {
    const aborted = e instanceof DOMException && e.name === 'AbortError'
    return {
      ok: false,
      error: {
        error: aborted ? 'aborted' : 'network_error',
        detail: aborted
          ? '查询已取消'
          : e instanceof Error ? e.message : String(e),
        status: 0,
      },
    }
  }
}
