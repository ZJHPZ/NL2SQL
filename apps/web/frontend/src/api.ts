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
  TableInfo,
  TaskCreateResult,
  TaskDetail,
  TaskDetailResponse,
  TaskListResponse,
  TaskRevisionsResponse,
  TaskRunResponse,
  TaskUpdateResult,
  ToolsetInfo,
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
    payload: { name: string; sql: string; description?: string },
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
}
