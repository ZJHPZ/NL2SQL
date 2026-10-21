/** 后端接口封装。
 *
 * 一个刻意的做法：**错误统一转成可展示的对象，不往上抛异常**。
 * 理由与后端一致——这是个给个人用的查询工具，
 * "连接失败""问题无法解析"都是正常业务状态，不是程序异常。
 * 抛异常会让每个调用点都要写 try/catch，且容易漏掉导致白屏。
 */
import type {
  AdminStats,
  HistoryItem,
  InfoResponse,
  KnowledgeHit,
  KnowledgeItem,
  QueryResponse,
  TableInfo,
  ToolsetInfo,
} from './types'

export interface ApiError {
  error: string
  detail: string
  status: number
}

export type ApiResult<T> = { ok: true; data: T } | { ok: false; error: ApiError }

const BASE = ''

async function request<T>(
  path: string,
  init?: RequestInit,
): Promise<ApiResult<T>> {
  try {
    const res = await fetch(BASE + path, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    })
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
          error: String(b.error ?? `http_${res.status}`),
          detail: String(b.detail ?? text.slice(0, 300) ?? ''),
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
}
