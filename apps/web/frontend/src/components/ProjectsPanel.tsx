/** 项目管理面板（M7）。
 *
 * 一屏分成三段：左边是项目清单，右边上是项目本身，右边下是它的数据源。
 * 为什么不引入路由：视图只有三个，App 里的一个状态就够；
 * 多加一个依赖（react-router）不解决任何本任务的问题。
 *
 * 两个刻意的处理：
 *   1. **口令只进不出**。界面上没有任何地方回显已保存的密码，
 *      编辑数据源时密码框留空 = 不修改（后端支持 overwrite）。
 *   2. **测试连接是表单的一部分，不是数据源行的按钮**：测试接口收的是
 *      SourceIn，刚填的连接信息在保存前就该能测通，这才是"可视化连接"的意义。
 */
import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type {
  DriverInfo,
  ProjectDetail,
  ProjectSummary,
  SourceIn,
  SourceOut,
  SourceTablePreview,
  SourceTestResult,
  SourceTablesResult,
} from '../types'

const ADAPTERS = ['sqlite', 'mysql', 'postgres', 'jdbc'] as const

const ADAPTER_LABEL: Record<string, string> = {
  sqlite: 'SQLite',
  mysql: 'MySQL',
  postgres: 'PostgreSQL',
  jdbc: 'JDBC 通用',
}

const DEFAULT_PORT: Record<string, string> = {
  sqlite: '',
  mysql: '3306',
  postgres: '5432',
  jdbc: '',
}

/** JDBC kind → 常见驱动类。选 kind 时自动填 driver_class（已有内容不覆盖） */
const JDBC_DRIVER_CLASS: Record<string, string> = {
  sqlite: 'org.sqlite.JDBC',
  mysql: 'com.mysql.cj.jdbc.Driver',
  postgresql: 'org.postgresql.Driver',
  mssql: 'com.microsoft.sqlserver.jdbc.SQLServerDriver',
}

function splitJarPaths(text: string): string[] {
  return text
    .split(/[\n;]+/)
    .map((s) => s.trim())
    .filter(Boolean)
}

function summarizeSource(s: SourceOut): string {
  if (s.adapter === 'sqlite') return s.path || '（未设置数据库文件路径）'
  if (s.adapter === 'jdbc') return s.jdbc_url || '（未设置 JDBC URL）'
  return `${s.host || '?'}:${s.port || '?'}${s.database ? `/${s.database}` : ''}`
}

/** 表预览：测试连接与「查看表」共用；最多渲染 20 张，避免 DOM 爆炸 */
function TablePreview({ tables }: { tables: SourceTablePreview[] }) {
  if (tables.length === 0) {
    return <div className="meta">没有发现任何表（连接是通的，但当前账号看不到表）。</div>
  }
  const shown = tables.slice(0, 20)
  return (
    <>
      <div className="preview-tables">
        {shown.map((t) => (
          <div className="preview-table" key={t.name}>
            <div className="preview-table-head">
              <b className="mono">{t.name}</b>
              <span className="meta">
                {t.row_estimate === null || t.row_estimate === undefined
                  ? '行数未知'
                  : `约 ${t.row_estimate} 行`}
                {' · '}
                {(t.columns ?? []).length} 列
              </span>
            </div>
            <div className="chips">
              {(t.columns ?? []).slice(0, 20).map((c) => (
                <span
                  className="chip mono"
                  key={c.name}
                  title={`${c.data_type}${c.nullable ? ' · 可空' : ' · 非空'}`}
                >
                  {c.name}
                </span>
              ))}
              {(t.columns ?? []).length > 20 && (
                <span className="chip">…共 {(t.columns ?? []).length} 列</span>
              )}
            </div>
          </div>
        ))}
      </div>
      {tables.length > shown.length && (
        <div className="meta" style={{ marginTop: 6 }}>
          仅预览前 {shown.length} 张表，共 {tables.length} 张。
        </div>
      )}
    </>
  )
}

const errText = (r: { error: { error: string; detail: string } }) =>
  `${r.error.error}: ${r.error.detail}`

export function ProjectsPanel({
  projectId,
  onSelectProject,
  onEnterProject,
  detailOnly = false,
  onProjectsChanged,
}: {
  projectId: string
  onSelectProject: (id: string) => void
  /** 点项目 = 进入项目；缺省时退化为只切换项目（旧行为） */
  onEnterProject?: (id: string, target?: 'project' | 'tasks') => void
  /** 只看当前项目的详情（已进入项目的工作区），不渲染左侧项目清单 */
  detailOnly?: boolean
  /** 项目清单发生变化（新建/删除/改名）时通知顶栏刷新切换器 */
  onProjectsChanged: () => void
}) {
  // ------------------------------------------------------------ 列表与详情
  const [projects, setProjects] = useState<ProjectSummary[] | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [detail, setDetail] = useState<ProjectDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)

  // ------------------------------------------------------------ 新建项目
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [newDesc, setNewDesc] = useState('')
  const [createBusy, setCreateBusy] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)

  // ------------------------------------------------------------ 项目编辑
  const [projectEditing, setProjectEditing] = useState(false)
  const [editName, setEditName] = useState('')
  const [editDesc, setEditDesc] = useState('')
  const [editSemantic, setEditSemantic] = useState('golden')
  const [editMetrics, setEditMetrics] = useState('')
  const [projectBusy, setProjectBusy] = useState(false)

  // ------------------------------------------------------------ 数据源表单
  const [formOpen, setFormOpen] = useState(false)
  const [editingSid, setEditingSid] = useState<string | null>(null)
  const [adapter, setAdapter] = useState<string>('sqlite')
  const [formSourceId, setFormSourceId] = useState('')
  const [label, setLabel] = useState('')
  const [path, setPath] = useState('')
  const [host, setHost] = useState('127.0.0.1')
  const [port, setPort] = useState('')
  const [user, setUser] = useState('')
  const [password, setPassword] = useState('')
  const [database, setDatabase] = useState('')
  const [jdbcUrl, setJdbcUrl] = useState('')
  const [driverClass, setDriverClass] = useState('')
  const [jarPaths, setJarPaths] = useState('')
  const [readOnly, setReadOnly] = useState(true)
  const [formBusy, setFormBusy] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [formMsg, setFormMsg] = useState<string | null>(null)

  // 测试连接
  const [testBusy, setTestBusy] = useState(false)
  const [testResult, setTestResult] = useState<SourceTestResult | null>(null)
  const [testError, setTestError] = useState<string | null>(null)

  // JDBC 驱动
  const [drivers, setDrivers] = useState<DriverInfo[] | null>(null)
  const [driversError, setDriversError] = useState<string | null>(null)
  const [driverKind, setDriverKind] = useState('')
  const [downloading, setDownloading] = useState(false)
  const [driverMsg, setDriverMsg] = useState<string | null>(null)

  // 数据源行内操作
  const [rowBusySid, setRowBusySid] = useState<string | null>(null)
  const [rowMsg, setRowMsg] = useState<{ sid: string; text: string; bad?: boolean } | null>(null)
  const [confirmDeleteSid, setConfirmDeleteSid] = useState<string | null>(null)
  const [previewSid, setPreviewSid] = useState<string | null>(null)
  const [preview, setPreview] = useState<SourceTablesResult | null>(null)
  const [previewBusy, setPreviewBusy] = useState(false)

  // 项目级提示（删除/改名等）
  const [projectMsg, setProjectMsg] = useState<string | null>(null)
  const [projectError, setProjectError] = useState<string | null>(null)
  const [confirmDeleteProject, setConfirmDeleteProject] = useState<string | null>(null)

  const loadList = useCallback(async () => {
    const r = await api.listProjects()
    if (!r.ok) {
      setListError(errText(r))
      return
    }
    setListError(null)
    setProjects(r.data.projects ?? [])
  }, [])

  useEffect(() => {
    void loadList()
  }, [loadList])

  // 详情随当前项目变化重新拉取；cancelled 防止快速切换时旧响应覆盖新详情
  useEffect(() => {
    let cancelled = false
    setDetail(null)
    setDetailLoading(true)
    setDetailError(null)
    setPreviewSid(null)
    setPreview(null)
    setConfirmDeleteSid(null)
    setConfirmDeleteProject(null)
    setProjectMsg(null)
    setProjectError(null)
    ;(async () => {
      const r = await api.getProject(projectId)
      if (cancelled) return
      setDetailLoading(false)
      if (!r.ok) {
        setDetailError(errText(r))
        return
      }
      setDetail(r.data)
      setEditName(r.data.name)
      setEditDesc(r.data.description ?? '')
      setEditSemantic(r.data.semantic || 'golden')
      setEditMetrics(r.data.metrics_path ?? '')
    })()
    return () => {
      cancelled = true
    }
  }, [projectId])

  // ------------------------------------------------------------ 驱动
  const loadDrivers = useCallback(async () => {
    setDriversError(null)
    const r = await api.jdbcDrivers()
    if (!r.ok) {
      setDriversError(errText(r))
      return
    }
    const list = r.data.drivers ?? []
    setDrivers(list)
    setDriverKind((cur) => cur || (list.find((d) => d.downloaded)?.kind ?? list[0]?.kind ?? ''))
  }, [])

  // ------------------------------------------------------------ 项目动作
  const createProject = async () => {
    const name = newName.trim()
    if (!name) {
      setCreateError('项目名称不能为空')
      return
    }
    setCreateBusy(true)
    setCreateError(null)
    const r = await api.createProject({ name, description: newDesc.trim() })
    setCreateBusy(false)
    if (!r.ok) {
      setCreateError(errText(r))
      return
    }
    setCreating(false)
    setNewName('')
    setNewDesc('')
    setProjectMsg(`项目「${r.data.name}」已创建`)
    await loadList()
    onProjectsChanged()
    onSelectProject(r.data.id)
    onEnterProject?.(r.data.id)
  }

  const saveProject = async () => {
    if (!detail) return
    setProjectBusy(true)
    setProjectError(null)
    setProjectMsg(null)
    const r = await api.updateProject(detail.id, {
      name: editName.trim(),
      description: editDesc.trim(),
      semantic: editSemantic,
      metrics_path: editMetrics.trim(),
    })
    setProjectBusy(false)
    if (!r.ok) {
      setProjectError(errText(r))
      return
    }
    setDetail(r.data)
    setProjectEditing(false)
    setProjectMsg('项目信息已保存')
    await loadList()
    onProjectsChanged()
  }

  const deleteProject = async (pid: string) => {
    setProjectBusy(true)
    setProjectError(null)
    const r = await api.deleteProject(pid)
    setProjectBusy(false)
    if (!r.ok) {
      setProjectError(errText(r))
      return
    }
    setConfirmDeleteProject(null)
    setProjectMsg('项目已删除')
    await loadList()
    onProjectsChanged()
    if (pid === projectId) {
      const fallback = projects?.find((p) => p.is_default)?.id ?? 'default'
      onSelectProject(fallback)
      if (detailOnly) onEnterProject?.(fallback)
    }
  }

  // ------------------------------------------------------------ 表单动作
  const resetForm = () => {
    setFormOpen(false)
    setEditingSid(null)
    setFormSourceId('')
    setLabel('')
    setPath('')
    setAdapter('sqlite')
    setHost('127.0.0.1')
    setPort('')
    setUser('')
    setPassword('')
    setDatabase('')
    setJdbcUrl('')
    setDriverClass('')
    setJarPaths('')
    setReadOnly(true)
    setFormError(null)
    setFormMsg(null)
    setTestResult(null)
    setTestError(null)
    setDriverMsg(null)
  }

  const openAddForm = () => {
    resetForm()
    setFormOpen(true)
  }

  const openEditForm = (s: SourceOut) => {
    setFormOpen(true)
    setEditingSid(s.source_id)
    setFormSourceId(s.source_id)
    setLabel(s.label ?? '')
    setAdapter(s.adapter || 'sqlite')
    setPath(s.path ?? '')
    setHost(s.host || '127.0.0.1')
    setPort(s.port ? String(s.port) : DEFAULT_PORT[s.adapter] ?? '')
    setUser(s.user ?? '')
    setDatabase(s.database ?? '')
    setPassword('')
    setJdbcUrl(s.jdbc_url ?? '')
    setDriverClass(s.driver_class ?? '')
    setJarPaths((s.jar_paths ?? []).join('\n'))
    setReadOnly(true)
    setFormError(null)
    setFormMsg(null)
    setTestResult(null)
    setTestError(null)
    setDriverMsg(null)
    if ((s.adapter as string) === 'jdbc') void loadDrivers()
  }

  const changeAdapter = (next: string) => {
    setAdapter(next)
    setPort(DEFAULT_PORT[next] ?? '')
    setTestResult(null)
    setTestError(null)
    if (next === 'jdbc') void loadDrivers()
  }

  /** 按契约拼 SourceIn：只带当前 adapter 有意义的字段 */
  const buildPayload = (): SourceIn => {
    const payload: SourceIn = { adapter, read_only: readOnly }
    const sid = formSourceId.trim()
    if (sid) payload.source_id = sid
    if (label.trim()) payload.label = label.trim()
    if (adapter === 'sqlite') {
      payload.path = path.trim()
    } else if (adapter === 'mysql' || adapter === 'postgres') {
      payload.host = host.trim()
      payload.port = Number(port) || 0
      payload.user = user.trim()
      payload.database = database.trim()
    } else {
      payload.jdbc_url = jdbcUrl.trim()
      payload.driver_class = driverClass.trim()
      payload.jar_paths = splitJarPaths(jarPaths)
      if (user.trim()) payload.user = user.trim()
    }
    // 留空 = 不修改已有口令；新建时留空 = 无口令
    if (password) payload.password = password
    return payload
  }

  /** 前端先挡一遍明显缺字段，省一次往返；最终以后端校验为准 */
  const validate = (p: SourceIn): string | null => {
    if (p.adapter === 'sqlite' && !p.path) return 'SQLite 需要填写数据库文件路径（path）'
    if (p.adapter === 'mysql' || p.adapter === 'postgres') {
      if (!p.host) return '请填写主机地址（host）'
      if (!p.user) return '请填写用户名（user）'
      if (!p.database) return '请填写数据库名（database）'
    }
    if (p.adapter === 'jdbc') {
      if (!p.jdbc_url) return '请填写 JDBC URL（jdbc_url）'
      if (!p.driver_class) return '请填写驱动类名（driver_class）'
      if (!p.jar_paths || p.jar_paths.length === 0) {
        return '请填写驱动 jar 的绝对路径（可点「下载驱动」自动填入）'
      }
    }
    return null
  }

  const testConnection = async () => {
    const payload = buildPayload()
    const bad = validate(payload)
    if (bad) {
      setTestError(bad)
      return
    }
    setTestBusy(true)
    setTestError(null)
    setTestResult(null)
    setFormMsg(null)
    const r = await api.testSource(projectId, payload)
    setTestBusy(false)
    if (!r.ok) {
      setTestError(errText(r))
      return
    }
    // 连接失败是业务结果（ok:false + message），仍按正常返回展示
    setTestResult(r.data)
  }

  const saveSource = async () => {
    const payload = buildPayload()
    const bad = validate(payload)
    if (bad) {
      setFormError(bad)
      return
    }
    setFormBusy(true)
    setFormError(null)
    setFormMsg(null)
    const r = await api.createSource(projectId, payload)
    setFormBusy(false)
    if (!r.ok) {
      setFormError(errText(r))
      return
    }
    const sid = r.data.source_id
    setFormMsg(`数据源「${r.data.label || sid}」已保存（${ADAPTER_LABEL[r.data.adapter] ?? r.data.adapter}）`)
    resetForm()
    setPreviewSid(null)
    const d = await api.getProject(projectId)
    if (d.ok) setDetail(d.data)
    await loadList()
    onProjectsChanged()
    setProjectMsg(`数据源「${sid}」已保存`)
  }

  const deleteSource = async (sid: string) => {
    setRowBusySid(sid)
    setRowMsg(null)
    const r = await api.deleteSource(projectId, sid)
    setRowBusySid(null)
    if (!r.ok) {
      setRowMsg({ sid, text: errText(r), bad: true })
      return
    }
    setConfirmDeleteSid(null)
    if (previewSid === sid) {
      setPreviewSid(null)
      setPreview(null)
    }
    const d = await api.getProject(projectId)
    if (d.ok) setDetail(d.data)
    setProjectMsg(`数据源「${sid}」已删除`)
  }

  const refreshSchema = async (sid: string) => {
    setRowBusySid(sid)
    setRowMsg(null)
    const r = await api.snapshotSource(projectId, sid)
    setRowBusySid(null)
    if (!r.ok) {
      setRowMsg({ sid, text: errText(r), bad: true })
      return
    }
    const warn = r.data.warnings?.length ? `；警告：${r.data.warnings.join('；')}` : ''
    setRowMsg({
      sid,
      text: `schema 已刷新：${r.data.table_count} 张表，指纹 ${r.data.fingerprint}${warn}`,
    })
  }

  const togglePreview = async (sid: string) => {
    if (previewSid === sid) {
      setPreviewSid(null)
      setPreview(null)
      return
    }
    setPreviewSid(sid)
    setPreview(null)
    setPreviewBusy(true)
    setRowMsg(null)
    const r = await api.sourceTables(projectId, sid)
    setPreviewBusy(false)
    if (!r.ok) {
      setRowMsg({ sid, text: errText(r), bad: true })
      setPreviewSid(null)
      return
    }
    setPreview(r.data)
  }

  const downloadDriver = async () => {
    if (!driverKind) return
    setDownloading(true)
    setDriverMsg(null)
    setDriversError(null)
    const r = await api.downloadJdbcDriver(driverKind)
    setDownloading(false)
    if (!r.ok) {
      setDriverMsg(`下载失败：${r.error.detail}`)
      return
    }
    if (!r.data.ok) {
      setDriverMsg(`下载失败：${r.data.message || '未知原因'}`)
      return
    }
    const p = r.data.path ?? ''
    setDriverMsg(p ? `驱动已下载：${p}（已自动填入 jar 路径）` : '驱动已下载')
    if (p) {
      setJarPaths((prev) => {
        const existing = splitJarPaths(prev)
        return existing.includes(p) ? prev : [...existing, p].join('\n')
      })
    }
    await loadDrivers()
  }

  const selectedDriver = (drivers ?? []).find((d) => d.kind === driverKind) ?? null
  const sources = detail?.sources ?? []

  // ------------------------------------------------------------ 渲染
  return (
    <div className={detailOnly ? 'workspace workspace-detail' : 'workspace'}>
      {/* -------------------------------------------------------- 项目列表 */}
      {!detailOnly && (
      <aside className="ws-side">
        <div className="ws-head">
          <b>项目</b>
          <span className="meta">{projects?.length ?? 0}</span>
          <span className="spacer" />
          <button
            className="btn btn-tiny"
            onClick={() => {
              setCreating((v) => !v)
              setCreateError(null)
            }}
          >
            {creating ? '取消' : '＋ 新建'}
          </button>
        </div>

        {creating && (
          <div className="mini-form">
            <label className="field">
              <span>名称</span>
              <input
                value={newName}
                placeholder="例如：电商分析"
                onChange={(e) => setNewName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void createProject()
                }}
              />
            </label>
            <label className="field">
              <span>说明</span>
              <input
                value={newDesc}
                placeholder="可留空"
                onChange={(e) => setNewDesc(e.target.value)}
              />
            </label>
            <button
              className="btn btn-primary btn-tiny"
              disabled={createBusy || !newName.trim()}
              onClick={() => void createProject()}
            >
              {createBusy ? '创建中…' : '创建项目'}
            </button>
            {createError && <div className="alert alert-bad">{createError}</div>}
          </div>
        )}

        {listError && <div className="alert alert-bad">{listError}</div>}
        {projects === null && <div className="side-empty">加载中…</div>}
        {projects !== null && projects.length === 0 && (
          <div className="side-empty">还没有项目，点「＋ 新建」建一个。</div>
        )}

        <ul className="ws-list">
          {(projects ?? []).map((p) => (
            <li key={p.id}>
              <button
                className={p.id === projectId ? 'ws-item active' : 'ws-item'}
                title={p.description || p.name}
                onClick={() => {
                  onSelectProject(p.id)
                  onEnterProject?.(p.id)
                }}
              >
                <div className="ws-item-title">{p.name}</div>
                <div className="ws-item-meta">
                  <span className="mono">{p.id}</span>
                  {p.is_default && <span className="badge badge-warn">默认</span>}
                  <span className="spacer" />
                  <span className="meta">
                    数据源 {p.sources?.length ?? 0} · 任务 {p.task_count ?? 0}
                  </span>
                  {onEnterProject && <span className="ws-enter mono">进入 →</span>}
                </div>
              </button>
            </li>
          ))}
        </ul>
      </aside>
      )}

      {/* -------------------------------------------------------- 项目详情 */}
      <section className="ws-main">
        {detailLoading && <div className="loading">正在加载项目…</div>}
        {detailError && (
          <div className="alert alert-bad">
            <strong>项目加载失败</strong>
            {detailError}
          </div>
        )}

        {detail && (
          <>
            <div className="detail-head">
              <h2 className="detail-title">{detail.name}</h2>
              {detail.is_default && <span className="badge badge-warn">默认项目</span>}
              <span className="chip mono" title="项目 ID">
                {detail.id}
              </span>
              <span className="spacer" />
              {onEnterProject && (
                <button
                  className="btn btn-primary btn-tiny"
                  disabled={detail.is_default}
                  title={
                    detail.is_default
                      ? '默认项目没有任务库，新建项目后才能用任务'
                      : '打开这个项目的任务文件夹'
                  }
                  onClick={() => onEnterProject(detail.id, 'tasks')}
                >
                  打开任务
                </button>
              )}
              {!projectEditing && !detail.is_default && (
                <button
                  className="btn btn-ghost btn-tiny"
                  onClick={() => setProjectEditing(true)}
                >
                  编辑项目
                </button>
              )}
              {!detail.is_default && (
                <button
                  className="btn btn-tiny"
                  disabled={projectBusy}
                  onClick={() => {
                    setConfirmDeleteProject(detail.id)
                    setProjectError(null)
                  }}
                >
                  删除项目
                </button>
              )}
            </div>

            {confirmDeleteProject === detail.id && (
              <div className="alert alert-warn">
                <strong>确认删除项目「{detail.name}」？</strong>
                项目的数据源配置与任务库（含版本历史）会一起消失，不可恢复。
                <div className="form-actions">
                  <button
                    className="btn btn-tiny"
                    disabled={projectBusy}
                    onClick={() => void deleteProject(detail.id)}
                  >
                    {projectBusy ? '删除中…' : '确认删除'}
                  </button>
                  <button
                    className="btn btn-ghost btn-tiny"
                    onClick={() => setConfirmDeleteProject(null)}
                  >
                    取消
                  </button>
                </div>
              </div>
            )}

            {projectMsg && <div className="alert alert-info">{projectMsg}</div>}
            {projectError && <div className="alert alert-bad">{projectError}</div>}

            {detail.is_default ? (
              <div className="notice-box">
                <div className="notice-title">默认项目</div>
                默认项目由环境变量构造（向后兼容），<b>不能改名/删除，也没有任务库</b>。
                要做多数据源、SQL 任务管理，请新建一个项目并切换过去。
              </div>
            ) : projectEditing ? (
              <div className="section-box">
                <div className="section-title">编辑项目</div>
                <div className="field-grid">
                  <label className="field">
                    <span>名称</span>
                    <input value={editName} onChange={(e) => setEditName(e.target.value)} />
                  </label>
                  <label className="field">
                    <span>语义层</span>
                    <select
                      value={editSemantic}
                      onChange={(e) => setEditSemantic(e.target.value)}
                    >
                      <option value="golden">golden（内置指标）</option>
                      <option value="dbt">dbt metrics.yml</option>
                      <option value="none">none（不使用语义层）</option>
                    </select>
                  </label>
                  <label className="field span2">
                    <span>说明</span>
                    <input value={editDesc} onChange={(e) => setEditDesc(e.target.value)} />
                  </label>
                  {editSemantic === 'dbt' && (
                    <label className="field span2">
                      <span>dbt metrics.yml 路径</span>
                      <input
                        className="mono"
                        value={editMetrics}
                        placeholder="例如：semantic/metrics.yml"
                        onChange={(e) => setEditMetrics(e.target.value)}
                      />
                    </label>
                  )}
                </div>
                <div className="form-actions">
                  <button
                    className="btn btn-primary btn-tiny"
                    disabled={projectBusy || !editName.trim()}
                    onClick={() => void saveProject()}
                  >
                    {projectBusy ? '保存中…' : '保存'}
                  </button>
                  <button
                    className="btn btn-ghost btn-tiny"
                    onClick={() => {
                      setProjectEditing(false)
                      setProjectError(null)
                      setEditName(detail.name)
                      setEditDesc(detail.description ?? '')
                      setEditSemantic(detail.semantic || 'golden')
                      setEditMetrics(detail.metrics_path ?? '')
                    }}
                  >
                    取消
                  </button>
                </div>
              </div>
            ) : (
              <div className="detail-desc">
                {detail.description || '（还没有说明）'}
                <div className="chips" style={{ marginTop: 8 }}>
                  <span className="chip">
                    语义层 <b>{detail.semantic || 'golden'}</b>
                  </span>
                  <span className="chip">任务 {detail.task_count ?? 0}</span>
                  <span className="chip">数据源 {sources.length}</span>
                  {detail.updated_at && (
                    <span className="chip mono" title="最近更新时间">
                      {detail.updated_at}
                    </span>
                  )}
                </div>
              </div>
            )}

            {/* -------------------------------------------------- 数据源 */}
            <div className="section-box">
              <div className="section-title">
                数据源 <span className="meta">{sources.length}</span>
                <span className="spacer" />
                <button
                  className="btn btn-primary btn-tiny"
                  onClick={formOpen ? resetForm : openAddForm}
                >
                  {formOpen ? '收起表单' : '＋ 添加数据源'}
                </button>
              </div>

              {sources.length === 0 && (
                <div className="empty-block">
                  <div className="empty-title">这个项目还没有数据源</div>
                  <div className="empty-hint">
                    点右上角「＋ 添加数据源」配置一个连接：SQLite 填数据库文件路径，
                    MySQL / PostgreSQL 填空格与账号，其它数据库用 JDBC（需先下载驱动 jar）。
                    配好并「测试连接」通过后，问答就会连到它。
                  </div>
                </div>
              )}

              {sources.map((s) => (
                <div className="source-card" key={s.source_id}>
                  <div className="source-head">
                    <b>{s.label || s.source_id}</b>
                    <span className="badge">{ADAPTER_LABEL[s.adapter] ?? s.adapter}</span>
                    <span className="chip mono" title="数据源 ID">
                      {s.source_id}
                    </span>
                    {s.adapter === 'sqlite' ? (
                      <span className="badge badge-ok">文件</span>
                    ) : s.has_password ? (
                      <span className="badge badge-ok">已保存口令</span>
                    ) : (
                      <span className="badge badge-warn">无口令</span>
                    )}
                    <span className="spacer" />
                    <button
                      className="btn btn-ghost btn-tiny"
                      disabled={rowBusySid === s.source_id}
                      onClick={() => void togglePreview(s.source_id)}
                    >
                      {previewSid === s.source_id ? '收起表' : '查看表'}
                    </button>
                    <button
                      className="btn btn-ghost btn-tiny"
                      disabled={rowBusySid === s.source_id}
                      title="重新抓取元数据快照（表结构/行数）"
                      onClick={() => void refreshSchema(s.source_id)}
                    >
                      {rowBusySid === s.source_id ? '刷新中…' : '刷新 schema'}
                    </button>
                    <button
                      className="btn btn-ghost btn-tiny"
                      onClick={() => openEditForm(s)}
                    >
                      编辑
                    </button>
                    {confirmDeleteSid === s.source_id ? (
                      <>
                        <button
                          className="btn btn-tiny"
                          disabled={rowBusySid === s.source_id}
                          onClick={() => void deleteSource(s.source_id)}
                        >
                          确认删除
                        </button>
                        <button
                          className="btn btn-ghost btn-tiny"
                          onClick={() => setConfirmDeleteSid(null)}
                        >
                          取消
                        </button>
                      </>
                    ) : (
                      <button
                        className="btn btn-ghost btn-tiny"
                        onClick={() => setConfirmDeleteSid(s.source_id)}
                      >
                        删除
                      </button>
                    )}
                  </div>
                  <div className="source-meta">
                    <span className="mono" title="连接信息">
                      {summarizeSource(s)}
                    </span>
                    {s.user && <span className="meta">用户 {s.user}</span>}
                    {s.adapter === 'jdbc' && s.driver_class && (
                      <span className="meta mono">{s.driver_class}</span>
                    )}
                  </div>
                  {rowMsg?.sid === s.source_id && (
                    <div className={rowMsg.bad ? 'alert alert-bad' : 'alert alert-info'}>
                      {rowMsg.text}
                    </div>
                  )}
                  {previewSid === s.source_id && (
                    <div className="preview-box">
                      {previewBusy && <div className="meta">正在读取表结构…</div>}
                      {!previewBusy && preview && (
                        <TablePreview tables={preview.tables ?? []} />
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>

            {/* -------------------------------------------------- 数据源表单 */}
            {formOpen && (
              <div className="section-box form-box">
                <div className="section-title">
                  {editingSid ? `编辑数据源「${editingSid}」` : '添加数据源'}
                  <span className="spacer" />
                  <span className="meta">口令保存后不会再回显</span>
                </div>

                {formMsg && <div className="alert alert-info">{formMsg}</div>}
                {formError && <div className="alert alert-bad">{formError}</div>}

                <div className="field-grid">
                  <label className="field">
                    <span>类型（adapter）</span>
                    <select value={adapter} onChange={(e) => changeAdapter(e.target.value)}>
                      {ADAPTERS.map((a) => (
                        <option key={a} value={a}>
                          {ADAPTER_LABEL[a]}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="field">
                    <span>数据源 ID</span>
                    <input
                      className="mono"
                      value={formSourceId}
                      placeholder="main（默认）"
                      onChange={(e) => setFormSourceId(e.target.value)}
                      title="同一项目内唯一；重复会覆盖已有配置"
                    />
                  </label>
                  <label className="field">
                    <span>显示名称</span>
                    <input
                      value={label}
                      placeholder="例如：电商库"
                      onChange={(e) => setLabel(e.target.value)}
                    />
                  </label>
                  <label className="field field-check">
                    <span>只读</span>
                    <input
                      type="checkbox"
                      checked={readOnly}
                      onChange={(e) => setReadOnly(e.target.checked)}
                    />
                    <span className="meta">只允许查询，推荐保持勾选</span>
                  </label>

                  {adapter === 'sqlite' && (
                    <label className="field span2">
                      <span>数据库文件路径（path）</span>
                      <input
                        className="mono"
                        value={path}
                        placeholder="例如：golden_set/ecommerce.db（相对仓库根，也可写绝对路径）"
                        onChange={(e) => setPath(e.target.value)}
                      />
                    </label>
                  )}

                  {(adapter === 'mysql' || adapter === 'postgres') && (
                    <>
                      <label className="field">
                        <span>主机 host</span>
                        <input value={host} onChange={(e) => setHost(e.target.value)} />
                      </label>
                      <label className="field">
                        <span>端口 port</span>
                        <input
                          className="mono"
                          value={port}
                          placeholder={DEFAULT_PORT[adapter]}
                          onChange={(e) => setPort(e.target.value)}
                        />
                      </label>
                      <label className="field">
                        <span>用户名 user</span>
                        <input value={user} onChange={(e) => setUser(e.target.value)} />
                      </label>
                      <label className="field">
                        <span>密码 password</span>
                        <input
                          type="password"
                          value={password}
                          placeholder={editingSid ? '留空表示不修改' : '可留空'}
                          onChange={(e) => setPassword(e.target.value)}
                        />
                      </label>
                      <label className="field">
                        <span>数据库 database</span>
                        <input value={database} onChange={(e) => setDatabase(e.target.value)} />
                      </label>
                    </>
                  )}

                  {adapter === 'jdbc' && (
                    <>
                      <label className="field span2">
                        <span>JDBC URL（jdbc_url）</span>
                        <input
                          className="mono"
                          value={jdbcUrl}
                          placeholder="例如：jdbc:mysql://127.0.0.1:3306/shop"
                          onChange={(e) => setJdbcUrl(e.target.value)}
                        />
                      </label>
                      <label className="field span2">
                        <span>驱动类名（driver_class）</span>
                        <input
                          className="mono"
                          value={driverClass}
                          placeholder="例如：com.mysql.cj.jdbc.Driver"
                          onChange={(e) => setDriverClass(e.target.value)}
                        />
                      </label>
                      <label className="field span2">
                        <span>驱动 jar 绝对路径（jar_paths，一行一个或用分号分隔）</span>
                        <textarea
                          className="mono"
                          rows={3}
                          value={jarPaths}
                          placeholder={'例如：C:\\drivers\\mysql-connector-j-8.4.0.jar'}
                          onChange={(e) => setJarPaths(e.target.value)}
                        />
                      </label>
                      <label className="field">
                        <span>用户名 user</span>
                        <input value={user} onChange={(e) => setUser(e.target.value)} />
                      </label>
                      <label className="field">
                        <span>密码 password</span>
                        <input
                          type="password"
                          value={password}
                          placeholder={editingSid ? '留空表示不修改' : '可留空'}
                          onChange={(e) => setPassword(e.target.value)}
                        />
                      </label>

                      {/* JDBC 驱动下载：不替用户猜版本，后端注册表说了算 */}
                      <div className="span2 driver-box">
                        <div className="driver-title">
                          JDBC 驱动
                          <span className="spacer" />
                          <span className="meta">
                            先从 Maven Central 下载对应驱动 jar，再填进 jar_paths
                          </span>
                        </div>
                        {driversError && (
                          <div className="alert alert-warn">{driversError}</div>
                        )}
                        {drivers === null && !driversError && (
                          <div className="meta">驱动列表加载中…</div>
                        )}
                        {drivers !== null && drivers.length === 0 && (
                          <div className="meta">后端没有返回可用驱动。</div>
                        )}
                        {drivers !== null && drivers.length > 0 && (
                          <>
                            <div className="field-grid">
                              <label className="field">
                                <span>驱动 kind</span>
                                <select
                                  value={driverKind}
                                  onChange={(e) => {
                                    const k = e.target.value
                                    setDriverKind(k)
                                    setDriverMsg(null)
                                    const cls = JDBC_DRIVER_CLASS[k]
                                    if (cls && !driverClass.trim()) setDriverClass(cls)
                                  }}
                                >
                                  {drivers.map((d) => (
                                    <option key={d.kind} value={d.kind}>
                                      {d.name}（{d.kind}）
                                    </option>
                                  ))}
                                </select>
                              </label>
                              <label className="field">
                                <span>状态</span>
                                <div className="field-static">
                                  {selectedDriver ? (
                                    selectedDriver.downloaded ? (
                                      <span className="badge badge-ok">已下载</span>
                                    ) : (
                                      <span className="badge badge-warn">未下载</span>
                                    )
                                  ) : (
                                    <span className="meta">—</span>
                                  )}
                                  <button
                                    className="btn btn-tiny"
                                    disabled={downloading || !driverKind}
                                    onClick={() => void downloadDriver()}
                                  >
                                    {downloading ? '下载中…' : '下载驱动'}
                                  </button>
                                </div>
                              </label>
                            </div>
                            {selectedDriver && (
                              <div className="meta mono" title="Maven 坐标 / 本地路径">
                                {selectedDriver.maven}
                                {selectedDriver.path
                                  ? ` · 本地：${selectedDriver.path}`
                                  : ''}
                              </div>
                            )}
                            {driverMsg && (
                              <div
                                className={
                                  driverMsg.startsWith('下载失败')
                                    ? 'alert alert-bad'
                                    : 'alert alert-info'
                                }
                              >
                                {driverMsg}
                              </div>
                            )}
                          </>
                        )}
                      </div>
                    </>
                  )}
                </div>

                <div className="form-actions">
                  <button
                    className="btn"
                    disabled={testBusy}
                    onClick={() => void testConnection()}
                    title="先测试再保存：连接失败不会写入配置"
                  >
                    {testBusy ? '测试中…' : '测试连接'}
                  </button>
                  <button
                    className="btn btn-primary"
                    disabled={formBusy}
                    onClick={() => void saveSource()}
                  >
                    {formBusy ? '保存中…' : '保存数据源'}
                  </button>
                  <button className="btn btn-ghost" onClick={resetForm}>
                    取消
                  </button>
                </div>

                {testError && <div className="alert alert-bad">{testError}</div>}

                {testResult && (
                  <div className="test-result">
                    {testResult.ok ? (
                      <div className="alert alert-info">
                        <strong>连接成功</strong>
                        {testResult.message || '连接成功'}
                        {testResult.dialect ? ` · 方言 ${testResult.dialect}` : ''}
                        {typeof testResult.elapsed_ms === 'number'
                          ? ` · ${testResult.elapsed_ms} ms`
                          : ''}
                      </div>
                    ) : (
                      <div className="alert alert-warn">
                        <strong>连接失败</strong>
                        {testResult.message || '后端没有给出原因'}
                      </div>
                    )}
                    {testResult.ok && (
                      <>
                        <div className="meta" style={{ margin: '8px 0 4px' }}>
                          发现的表（{(testResult.tables ?? []).length} 张）：
                        </div>
                        <TablePreview tables={testResult.tables ?? []} />
                      </>
                    )}
                  </div>
                )}
              </div>
            )}
          </>
        )}
      </section>
    </div>
  )
}
