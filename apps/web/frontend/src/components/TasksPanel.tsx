/** SQL 任务面板（M7）。
 *
 * 任务是"项目里保存的只读 SQL"——用户在界面上点「运行」才执行。
 * 因此有两个界面约束：
 *   1. 运行的是**已保存**的 SQL（run 接口不带 SQL 参数）；
 *      SQL 改过但没保存时禁止运行，避免"我明明改了怎么结果还是旧的"。
 *   2. 每次保存 / agent 修改都会生成一条版本记录；
 *      author="ai" 的记录必须有醒目的「AI 修改」角标——用户要能一眼看出
 *      这段 SQL 是模型改的，不是自己改的。
 */
import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type {
  ResultPayload,
  TaskDetailResponse,
  TaskRunResponse,
  TaskRunResultPayload,
  TaskSummary,
} from '../types'
import { ResultTable } from './ResultTable'

const errText = (r: { error: { error: string; detail: string } }) =>
  `${r.error.error}: ${r.error.detail}`

/** 任务返回值补成 ResultTable 认识的 ResultPayload。
 * ResultTable 只用到 columns/rows/row_count/truncated，
 * 其余字段补齐即可，不改原组件的行为。
 */
function toResultPayload(r: TaskRunResultPayload): ResultPayload {
  const rows = r.rows ?? []
  return {
    columns: r.columns ?? [],
    rows,
    row_count: r.row_count ?? rows.length,
    truncated: r.truncated ?? false,
    elapsed_ms: r.elapsed_ms ?? 0,
    data_time: null,
    cached: false,
    warnings: [],
  }
}

function authorLabel(author: string): string {
  if (author === 'ai') return 'AI'
  if (author === 'user') return '手动'
  return author || '未知'
}

export function TasksPanel({
  projectId,
  isDefaultProject,
}: {
  projectId: string
  /** 默认项目没有任务库：不请求任务接口，直接给引导 */
  isDefaultProject: boolean
}) {
  // ------------------------------------------------------------ 列表
  const [tasks, setTasks] = useState<TaskSummary[] | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [listBusy, setListBusy] = useState(false)

  // ------------------------------------------------------------ 详情
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<TaskDetailResponse | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)

  // 编辑器
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [sql, setSql] = useState('')
  const [note, setNote] = useState('')
  const [saveBusy, setSaveBusy] = useState(false)
  const [saveMsg, setSaveMsg] = useState<string | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)

  // 新建
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [newDesc, setNewDesc] = useState('')
  const [newSql, setNewSql] = useState('')
  const [createBusy, setCreateBusy] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)

  // 运行
  const [runBusy, setRunBusy] = useState(false)
  const [runResult, setRunResult] = useState<TaskRunResponse | null>(null)
  const [runError, setRunError] = useState<string | null>(null)

  // 版本历史展开
  const [expandedRev, setExpandedRev] = useState<number | null>(null)

  // 删除
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [deleteBusy, setDeleteBusy] = useState(false)

  const loadList = useCallback(async () => {
    if (isDefaultProject) {
      setTasks([])
      setListError(null)
      return
    }
    setListBusy(true)
    const r = await api.listTasks(projectId)
    setListBusy(false)
    if (!r.ok) {
      setListError(errText(r))
      return
    }
    setListError(null)
    setTasks(r.data.tasks ?? [])
  }, [projectId, isDefaultProject])

  useEffect(() => {
    setSelectedId(null)
    setDetail(null)
    setCreating(false)
    void loadList()
  }, [loadList])

  // 详情随选中任务变化加载；取消过期响应
  useEffect(() => {
    if (isDefaultProject || !selectedId) {
      setDetail(null)
      return
    }
    let cancelled = false
    setDetailLoading(true)
    setDetailError(null)
    setRunResult(null)
    setRunError(null)
    setExpandedRev(null)
    ;(async () => {
      const r = await api.getTask(projectId, selectedId)
      if (cancelled) return
      setDetailLoading(false)
      if (!r.ok) {
        setDetailError(errText(r))
        return
      }
      setDetail(r.data)
      const t = r.data.task
      setName(t.name)
      setDescription(t.description ?? '')
      setSql(t.sql ?? '')
      setNote('')
      setSaveMsg(null)
      setSaveError(null)
    })()
    return () => {
      cancelled = true
    }
  }, [projectId, selectedId, isDefaultProject])

  /** 保存后重新拉详情：PUT 只回 task+revision，版本历史要从详情接口拿 */
  const reloadDetail = async () => {
    if (!selectedId) return
    const r = await api.getTask(projectId, selectedId)
    if (!r.ok) {
      setDetailError(errText(r))
      return
    }
    setDetail(r.data)
    setSaveMsg(null)
  }

  // ------------------------------------------------------------ 动作
  const createTask = async () => {
    if (!newName.trim()) {
      setCreateError('任务名称不能为空')
      return
    }
    if (!newSql.trim()) {
      setCreateError('SQL 不能为空')
      return
    }
    setCreateBusy(true)
    setCreateError(null)
    const r = await api.createTask(projectId, {
      name: newName.trim(),
      sql: newSql,
      description: newDesc.trim() || undefined,
    })
    setCreateBusy(false)
    if (!r.ok) {
      setCreateError(errText(r))
      return
    }
    setCreating(false)
    setNewName('')
    setNewDesc('')
    setNewSql('')
    await loadList()
    setSelectedId(r.data.task.task_id)
  }

  const saveTask = async () => {
    if (!detail) return
    const tid = detail.task.task_id
    const sqlDirty = sql !== (detail.task.sql ?? '')
    const nameDirty = name.trim() !== (detail.task.name ?? '')
    const descDirty = description.trim() !== (detail.task.description ?? '')
    if (!sqlDirty && !nameDirty && !descDirty) {
      setSaveMsg('没有改动，无需保存')
      return
    }
    if (!sql.trim()) {
      setSaveError('SQL 不能为空')
      return
    }
    setSaveBusy(true)
    setSaveError(null)
    setSaveMsg(null)
    const r = await api.updateTask(projectId, tid, {
      name: name.trim(),
      description: description.trim(),
      sql: sqlDirty ? sql : undefined,
      note: note.trim() || undefined,
    })
    setSaveBusy(false)
    if (!r.ok) {
      setSaveError(errText(r))
      return
    }
    setNote('')
    setName(r.data.task.name)
    setDescription(r.data.task.description ?? '')
    setSql(r.data.task.sql ?? '')
    setSaveMsg(`已保存（当前版本 v${r.data.revision}）`)
    await reloadDetail()
    await loadList()
  }

  /** 回滚 = 用旧版内容生成新版本；历史保留，编辑器同步到新版本内容 */
  const restoreRevision = async (revision: number) => {
    if (!detail) return
    if (
      !window.confirm(
        `把任务回滚到 v${revision}？会用这一版内容生成一个新版本，历史不会丢失。`,
      )
    ) {
      return
    }
    setSaveBusy(true)
    setSaveError(null)
    setSaveMsg(null)
    const r = await api.restoreTaskRevision(
      projectId,
      detail.task.task_id,
      revision,
    )
    setSaveBusy(false)
    if (!r.ok) {
      setSaveError(errText(r))
      return
    }
    const fresh = await api.getTask(projectId, detail.task.task_id)
    if (fresh.ok) {
      setDetail(fresh.data)
      setName(fresh.data.task.name)
      setDescription(fresh.data.task.description ?? '')
      setSql(fresh.data.task.sql ?? '')
      setExpandedRev(null)
    }
    setSaveMsg(`已回滚到 v${revision}（生成新版本 v${r.data.revision}）`)
    await loadList()
  }

  const runTask = async () => {
    if (!detail) return
    setRunBusy(true)
    setRunError(null)
    setRunResult(null)
    const r = await api.runTask(projectId, detail.task.task_id)
    setRunBusy(false)
    if (!r.ok) {
      setRunError(errText(r))
      return
    }
    setRunResult(r.data)
  }

  const deleteTask = async () => {
    if (!detail) return
    setDeleteBusy(true)
    const r = await api.deleteTask(projectId, detail.task.task_id)
    setDeleteBusy(false)
    if (!r.ok) {
      setSaveError(errText(r))
      return
    }
    setConfirmDelete(false)
    setSelectedId(null)
    setDetail(null)
    await loadList()
  }

  // ------------------------------------------------------------ 派生状态
  const sqlDirty = detail ? sql !== (detail.task.sql ?? '') : false
  const dirty = detail
    ? sqlDirty ||
      name.trim() !== (detail.task.name ?? '') ||
      description.trim() !== (detail.task.description ?? '')
    : false

  // ------------------------------------------------------------ 默认项目
  if (isDefaultProject) {
    return (
      <div className="workspace">
        <aside className="ws-side">
          <div className="ws-head">
            <b>任务</b>
          </div>
          <div className="side-empty">默认项目没有任务库</div>
        </aside>
        <section className="ws-main">
          <div className="notice-box">
            <div className="notice-title">默认项目没有任务库</div>
            默认项目是为兼容旧配置（环境变量里直接挂数据源）而保留的，
            不保存 SQL 任务。请在上方项目切换器里新建 / 选择一个自建项目，
            再回来管理任务。
          </div>
        </section>
      </div>
    )
  }

  // ------------------------------------------------------------ 渲染
  return (
    <div className="workspace">
      {/* -------------------------------------------------------- 任务列表 */}
      <aside className="ws-side">
        <div className="ws-head">
          <b>任务</b>
          <span className="meta">{tasks?.length ?? 0}</span>
          <span className="spacer" />
          <button
            className="btn btn-ghost btn-tiny"
            disabled={listBusy}
            title="刷新列表：agent 在对话里创建/修改的任务也会出现在这里"
            onClick={() => void loadList()}
          >
            {listBusy ? '…' : '刷新'}
          </button>
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
                placeholder="例如：月度实收金额"
                onChange={(e) => setNewName(e.target.value)}
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
            <label className="field">
              <span>SQL（单条只读 SELECT / WITH）</span>
              <textarea
                className="mono"
                rows={5}
                value={newSql}
                spellCheck={false}
                placeholder={'SELECT ...\nFROM ...'}
                onChange={(e) => setNewSql(e.target.value)}
              />
            </label>
            <button
              className="btn btn-primary btn-tiny"
              disabled={createBusy || !newName.trim() || !newSql.trim()}
              onClick={() => void createTask()}
            >
              {createBusy ? '创建中…' : '创建任务'}
            </button>
            {createError && <div className="alert alert-bad">{createError}</div>}
          </div>
        )}

        {listError && <div className="alert alert-bad">{listError}</div>}
        {tasks === null && <div className="side-empty">加载中…</div>}
        {tasks !== null && tasks.length === 0 && (
          <div className="side-empty">
            还没有任务。点「＋ 新建」保存一条只读 SQL；
            也可以直接在问答里让 agent 帮你建（切回来点「刷新」可见）。
          </div>
        )}

        <ul className="ws-list">
          {(tasks ?? []).map((t) => (
            <li key={t.task_id}>
              <button
                className={t.task_id === selectedId ? 'ws-item active' : 'ws-item'}
                title={t.sql_preview || t.description || t.name}
                onClick={() => setSelectedId(t.task_id)}
              >
                <div className="ws-item-title">
                  {t.name}
                  {t.updated_by === 'ai' && <span className="badge badge-ai">AI</span>}
                </div>
                <div className="ws-item-preview mono">{t.sql_preview}</div>
                <div className="ws-item-meta">
                  <span className="meta">
                    v{t.revision} · {authorLabel(t.updated_by)} · {t.updated_at}
                  </span>
                </div>
              </button>
            </li>
          ))}
        </ul>
      </aside>

      {/* -------------------------------------------------------- 任务详情 */}
      <section className="ws-main">
        {!selectedId && !detailLoading && (
          <div className="empty-block">
            <div className="empty-title">选择左侧的一个任务</div>
            <div className="empty-hint">
              任务里保存的是单条只读 SQL。选中后可以改 SQL、保存（生成新版本）、
              运行看结果；agent 在对话里修改任务时，这里会出现「AI」角标。
            </div>
          </div>
        )}

        {detailLoading && <div className="loading">正在加载任务…</div>}
        {detailError && (
          <div className="alert alert-bad">
            <strong>任务加载失败</strong>
            {detailError}
          </div>
        )}

        {detail && (
          <>
            <div className="detail-head">
              <h2 className="detail-title">{detail.task.name}</h2>
              <span className="badge">{detail.task.status || 'ready'}</span>
              <span className="chip mono">v{detail.task.revision}</span>
              <span className="spacer" />
              <span className="meta">
                更新于 {detail.task.updated_at} · {authorLabel(detail.task.updated_by)}
              </span>
              {confirmDelete ? (
                <>
                  <button
                    className="btn btn-tiny"
                    disabled={deleteBusy}
                    onClick={() => void deleteTask()}
                  >
                    {deleteBusy ? '删除中…' : '确认删除'}
                  </button>
                  <button
                    className="btn btn-ghost btn-tiny"
                    onClick={() => setConfirmDelete(false)}
                  >
                    取消
                  </button>
                </>
              ) : (
                <button
                  className="btn btn-ghost btn-tiny"
                  onClick={() => setConfirmDelete(true)}
                >
                  删除任务
                </button>
              )}
            </div>

            {saveMsg && <div className="alert alert-info">{saveMsg}</div>}
            {saveError && <div className="alert alert-bad">{saveError}</div>}
            {dirty && (
              <div className="alert alert-warn">
                有未保存的修改。运行执行的是<b>已保存</b>的 SQL，先保存再运行。
              </div>
            )}

            <div className="field-grid">
              <label className="field">
                <span>名称</span>
                <input value={name} onChange={(e) => setName(e.target.value)} />
              </label>
              <label className="field">
                <span>说明</span>
                <input
                  value={description}
                  placeholder="可留空"
                  onChange={(e) => setDescription(e.target.value)}
                />
              </label>
            </div>

            <div className="section-box">
              <div className="section-title">
                SQL
                {sqlDirty && <span className="badge badge-warn">未保存</span>}
                <span className="spacer" />
                <span className="meta">
                  单条只读 SELECT / WITH · Ctrl/Cmd + Enter 保存
                </span>
              </div>
              <textarea
                className="sql-editor mono"
                value={sql}
                spellCheck={false}
                rows={Math.min(24, Math.max(6, sql.split('\n').length))}
                onChange={(e) => setSql(e.target.value)}
                onKeyDown={(e) => {
                  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
                    e.preventDefault()
                    void saveTask()
                  }
                }}
              />
              <div className="field-grid" style={{ marginTop: 8 }}>
                <label className="field span2">
                  <span>保存备注（可选，写进版本历史）</span>
                  <input
                    value={note}
                    placeholder="例如：把时间范围改到上半年"
                    onChange={(e) => setNote(e.target.value)}
                  />
                </label>
              </div>
              <div className="form-actions">
                <button
                  className="btn btn-primary"
                  disabled={saveBusy || !dirty}
                  onClick={() => void saveTask()}
                >
                  {saveBusy ? '保存中…' : '保存修改'}
                </button>
                <button
                  className="btn"
                  disabled={runBusy || sqlDirty}
                  title={
                    sqlDirty
                      ? 'SQL 有未保存的修改：先保存，运行才会执行新 SQL'
                      : '执行已保存的 SQL（只读守卫在后端）'
                  }
                  onClick={() => void runTask()}
                >
                  {runBusy ? '运行中…' : '运行'}
                </button>
              </div>
            </div>

            {/* -------------------------------------------------- 运行结果 */}
            {runError && <div className="alert alert-bad">{runError}</div>}
            {runResult && !runResult.ok && (
              <div className="alert alert-warn">
                <strong>执行被拒绝</strong>
                {runResult.message || `状态：${runResult.status}`}
              </div>
            )}
            {runResult?.ok && runResult.result && (
              <div className="section-box">
                <div className="section-title">
                  运行结果
                  <span className="spacer" />
                  <span className="meta">
                    {runResult.status} · {(runResult.result.row_count ?? 0)} 行
                    {runResult.message ? ` · ${runResult.message}` : ''}
                  </span>
                </div>
                <ResultTable result={toResultPayload(runResult.result)} />
              </div>
            )}
            {runResult?.ok && !runResult.result && (
              <div className="empty-block">
                <div className="empty-title">执行成功，但没有结果表</div>
                <div className="empty-hint">
                  状态：{runResult.status}
                  {runResult.message ? ` · ${runResult.message}` : ''}
                </div>
              </div>
            )}

            {/* -------------------------------------------------- 版本历史 */}
            <div className="section-box">
              <div className="section-title">
                版本历史 <span className="meta">{(detail.revisions ?? []).length} 条</span>
                <span className="spacer" />
                <span className="meta">每次保存 / AI 修改都会追加一条</span>
              </div>
              {(detail.revisions ?? []).length === 0 ? (
                <div className="meta">还没有版本记录。</div>
              ) : (
                <ol className="rev-list">
                  {(detail.revisions ?? []).map((rev) => {
                    const isAi = rev.author === 'ai'
                    const open = expandedRev === rev.revision
                    return (
                      <li className={isAi ? 'rev-item rev-ai' : 'rev-item'} key={rev.revision}>
                        <div className="rev-head">
                          <b className="mono">v{rev.revision}</b>
                          {isAi ? (
                            <span className="badge badge-ai">AI 修改</span>
                          ) : (
                            <span className="badge">{authorLabel(rev.author)}</span>
                          )}
                          <span className="meta mono">{rev.created_at}</span>
                          <span className="spacer" />
                          <button
                            className="btn btn-ghost btn-tiny"
                            onClick={() => setExpandedRev(open ? null : rev.revision)}
                          >
                            {open ? '收起 SQL' : '查看 SQL'}
                          </button>
                          <button
                            className="btn btn-ghost btn-tiny"
                            title="把这一版 SQL 载入编辑器（不会自动保存）"
                            onClick={() => {
                              setSql(rev.sql)
                              setExpandedRev(null)
                            }}
                          >
                            载入编辑器
                          </button>
                          <button
                            className="btn btn-ghost btn-tiny"
                            title="用这一版内容生成新版本（历史保留，可再回滚）"
                            disabled={saveBusy || rev.revision === detail.task.revision}
                            onClick={() => void restoreRevision(rev.revision)}
                          >
                            回滚到此版本
                          </button>
                        </div>
                        {rev.note && <div className="rev-note">{rev.note}</div>}
                        {open && <pre className="rev-sql mono">{rev.sql}</pre>}
                      </li>
                    )
                  })}
                </ol>
              )}
            </div>
          </>
        )}
      </section>
    </div>
  )
}
