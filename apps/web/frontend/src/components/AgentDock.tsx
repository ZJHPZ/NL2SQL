import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowUpRight, Copy, Send, Square, X } from 'lucide-react'
import { streamQuery } from '../api'
import type { QueryResponse, ResultPayload, StreamEvent } from '../types'
import { Markdown } from './Markdown'
import type { StageStep } from './QueryProgress'

/** 与工作台同一套事件口径的极简翻译：这里只要一行可读进度。 */
function stageOf(ev: StreamEvent): StageStep | null {
  switch (ev.event) {
    case 'start':
      return { key: 'start', label: '已连上查询引擎，开始规划', state: 'done' }
    case 'decision': {
      const d = ev.data
      if (d.kind === 'final') {
        return { key: `d${d.step}`, label: `第 ${d.step} 步：整理最终结论`, state: 'done' }
      }
      if (d.kind === 'clarify') {
        return { key: `d${d.step}`, label: `第 ${d.step} 步：需要补充信息`, state: 'done' }
      }
      return {
        key: `d${d.step}`,
        label: d.tool ? `第 ${d.step} 步：调用 ${d.tool}` : `第 ${d.step} 步：${d.kind}`,
        detail: d.thought ?? undefined,
        state: 'done',
      }
    }
    case 'tool':
      return {
        key: `t${ev.data.step}`,
        label: `执行 ${ev.data.tool}`,
        detail: ev.data.observation ?? undefined,
        state: ev.data.ok ? 'done' : 'warn',
      }
    case 'guard':
      return {
        key: `g${ev.data.step}`,
        label: '模型行为被机制纠偏',
        detail: ev.data.rule,
        state: 'warn',
      }
    case 'clarify':
      return {
        key: `c${ev.data.step}`,
        label: '追问',
        detail: ev.data.question,
        state: 'done',
      }
    case 'model_retry':
    case 'model_repair':
    case 'tool_missing':
      return {
        key: `r${ev.data.step}-${ev.event}`,
        label: '内部修复 / 重试',
        detail: String(ev.data.reason ?? ''),
        state: 'warn',
      }
    default:
      return null
  }
}

const STATUS_LABEL: Record<string, string> = {
  ok: '完成',
  empty: '没有数据',
  truncated: '结果被截断',
  unresolved: '未能解析',
  invalid_sql: 'SQL 不合法',
  blocked: '被安全策略拦截',
  timeout: '超时',
  error: '执行失败',
  need_clarification: '需要补充信息',
}

interface Turn {
  id: number
  role: 'user' | 'agent'
  text?: string
  stages?: StageStep[]
  elapsedMs?: number
  response?: QueryResponse
  error?: string
}

/** 抽屉里的小结果表：只放前几行，完整明细交给「在结果页打开」。 */
function MiniTable({ result }: { result: ResultPayload }) {
  const cols = result.columns.slice(0, 6)
  const rows = result.rows.slice(0, 6)
  const cell = (v: unknown) => {
    if (v === null || v === undefined) return '—'
    if (typeof v === 'object') return JSON.stringify(v)
    return String(v)
  }
  return (
    <div className="mini-table-wrap">
      <table className="mini-table">
        <thead>
          <tr>
            {cols.map((c) => (
              <th key={c}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {cols.map((_, j) => (
                <td key={j}>{cell(r[j])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mini-table-note mono">
        {result.row_count} 行{result.truncated ? ' · 已截断' : ''} · 最多显示 6 行
      </p>
    </div>
  )
}

export interface AgentDockProps {
  projectId: string
  projectName?: string
  sourceId: string | null
  now: string
  onOpenInWorkbench: (resp: QueryResponse) => void
}

/** 随行智能体：悬浮印章常驻所有页面，点开是当前项目的对话抽屉。 */
export function AgentDock({ projectId, projectName, sourceId, now, onOpenInWorkbench }: AgentDockProps) {
  const [open, setOpen] = useState(false)
  const [turns, setTurns] = useState<Turn[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  const abortRef = useRef<AbortController | null>(null)
  const bodyRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
  const idRef = useRef(0)
  const nextId = () => (idRef.current += 1)

  // 对话只在当前项目里成立：切项目就开一份新会话，别把两个库的口径混在一起。
  useEffect(() => {
    setTurns([])
    setSessionId(null)
    setBusy(false)
    abortRef.current?.abort()
  }, [projectId])

  useEffect(() => {
    if (!open) return
    const t = window.setTimeout(() => inputRef.current?.focus(), 60)
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => {
      window.clearTimeout(t)
      window.removeEventListener('keydown', onKey)
    }
  }, [open])

  useEffect(() => {
    const el = bodyRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [turns, open])

  useEffect(() => () => abortRef.current?.abort(), [])

  const patchAgent = useCallback((id: number, patch: Partial<Turn>) => {
    setTurns((prev) => prev.map((t) => (t.id === id ? { ...t, ...patch } : t)))
  }, [])

  const send = useCallback(
    async (raw?: string) => {
      const question = (raw ?? input).trim()
      if (!question || busy) return
      const agentId = nextId()
      setInput('')
      setBusy(true)
      setTurns((prev) => [
        ...prev,
        { id: nextId(), role: 'user', text: question },
        { id: agentId, role: 'agent', stages: [], elapsedMs: 0 },
      ])

      const controller = new AbortController()
      abortRef.current = controller
      const startedAt = performance.now()
      const timer = window.setInterval(() => {
        patchAgent(agentId, { elapsedMs: performance.now() - startedAt })
      }, 140)
      const stages: StageStep[] = []

      const r = await streamQuery(
        { question, source_id: sourceId, now: now || null, session_id: sessionId },
        {
          signal: controller.signal,
          onEvent: (ev) => {
            const stage = stageOf(ev)
            if (!stage) return
            const i = stages.findIndex((s) => s.key === stage.key)
            if (i >= 0) stages[i] = stage
            else stages.push(stage)
            patchAgent(agentId, { stages: [...stages] })
          },
        },
      )

      window.clearInterval(timer)
      abortRef.current = null
      setBusy(false)
      const elapsedMs = performance.now() - startedAt

      if (!r.ok) {
        patchAgent(agentId, {
          elapsedMs,
          error:
            r.error.error === 'aborted'
              ? '已取消（后端线程仍会跑完，本次结果不进入抽屉）'
              : `${r.error.error}: ${r.error.detail}`,
        })
        return
      }
      patchAgent(agentId, { elapsedMs, response: r.data })
      if (r.data.session_id) setSessionId(r.data.session_id)
      setTurns((prev) => prev)
    },
    [busy, input, now, patchAgent, sessionId, sourceId],
  )

  const cancel = () => abortRef.current?.abort()

  const newChat = () => {
    abortRef.current?.abort()
    setBusy(false)
    setTurns([])
    setSessionId(null)
    setInput('')
  }

  const copySql = async (sql: string) => {
    try {
      await navigator.clipboard.writeText(sql)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1600)
    } catch {
      /* 剪贴板不可用时静默失败，SQL 本身在抽屉里可见可选中 */
    }
  }

  const prompts = [
    '这个项目里有哪些数据表？',
    '最近一次查询查到了什么？',
    '把 2024 年各渠道实收金额按降序排一下',
  ]

  return (
    <>
      <button
        type="button"
        className={open ? 'agent-seal is-open' : 'agent-seal'}
        aria-label={open ? '收起随行智能体' : '问 · 随行智能体'}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        {open ? <X size={20} aria-hidden="true" /> : <span className="agent-seal-glyph">问</span>}
        {!open && <span className="agent-seal-tag mono">随行智能体</span>}
      </button>

      {open && (
        <section className="agent-dock" role="dialog" aria-label="随行智能体">
          <div className="agent-dock-head">
            <div className="agent-dock-heading">
              <div className="agent-dock-kicker mono">随行 · 记录</div>
              <h2 className="agent-dock-title">智能体</h2>
            </div>
            <span className="agent-dock-project mono">{projectName || projectId}</span>
            <div className="agent-dock-tools">
              <button type="button" className="btn btn-ghost btn-tiny" onClick={newChat}>
                新会话
              </button>
              <button
                type="button"
                className="btn btn-icon"
                aria-label="收起随行智能体"
                onClick={() => setOpen(false)}
              >
                <X size={16} aria-hidden="true" />
              </button>
            </div>
          </div>

          <div className="agent-dock-body" ref={bodyRef}>
            {turns.length === 0 && (
              <div className="agent-dock-intro">
                <p>
                  在这里直接问当前项目的数据。和首页工作台是同一套只读查询引擎，
                  结果可以一键送回结果页看完整明细。
                </p>
                <div className="agent-prompts">
                  {prompts.map((p) => (
                    <button
                      key={p}
                      type="button"
                      className="agent-prompt"
                      onClick={() => void send(p)}
                    >
                      {p}
                    </button>
                  ))}
                </div>
                <p className="agent-dock-hint mono">Enter 发送 · Shift+Enter 换行 · Esc 收起</p>
              </div>
            )}

            {turns.map((t) =>
              t.role === 'user' ? (
                <div className="turn-user" key={t.id}>
                  {t.text}
                </div>
              ) : (
                <div className="turn-agent" key={t.id}>
                  {t.stages && t.stages.length > 0 && (
                    <ol className="turn-stages">
                      {t.stages.map((s) => (
                        <li key={s.key} className={`turn-stage stage-${s.state}`}>
                          <span className="turn-stage-dot" aria-hidden="true" />
                          <span className="turn-stage-label">{s.label}</span>
                          {s.detail && <span className="turn-stage-detail">{s.detail}</span>}
                        </li>
                      ))}
                    </ol>
                  )}
                  {!t.response && !t.error && (
                    <p className="turn-running mono">
                      正在处理 · {((t.elapsedMs ?? 0) / 1000).toFixed(1)}s
                      {busy && <span className="turn-caret" aria-hidden="true" />}
                    </p>
                  )}
                  {t.error && <p className="turn-error">{t.error}</p>}
                  {t.response && (
                    <>
                      <div className="turn-status mono">
                        {STATUS_LABEL[t.response.status] ?? t.response.status}
                        {t.response.result ? ` · ${t.response.result.row_count} 行` : ''}
                        {` · ${((t.elapsedMs ?? 0) / 1000).toFixed(1)}s`}
                        {t.response.cached ? ' · 命中缓存' : ''}
                      </div>
                      {t.response.answer && (
                        <Markdown className="turn-answer" text={t.response.answer} />
                      )}
                      {t.response.status === 'need_clarification' && (
                        <div className="turn-clarify">
                          {t.response.message && <p>{t.response.message}</p>}
                          {t.response.suggestions.length > 0 && (
                            <div className="agent-prompts">
                              {t.response.suggestions.slice(0, 4).map((s) => (
                                <button
                                  key={s}
                                  type="button"
                                  className="agent-prompt"
                                  onClick={() => void send(s)}
                                >
                                  {s}
                                </button>
                              ))}
                            </div>
                          )}
                        </div>
                      )}
                      {t.response.sql && (
                        <details className="turn-sql">
                          <summary className="mono">SQL · 可复制</summary>
                          <pre>{t.response.sql}</pre>
                        </details>
                      )}
                      {t.response.result && <MiniTable result={t.response.result} />}
                      <div className="turn-actions">
                        {t.response.result && (
                          <button
                            type="button"
                            className="btn btn-tiny"
                            onClick={() => onOpenInWorkbench(t.response as QueryResponse)}
                          >
                            <ArrowUpRight size={13} aria-hidden="true" /> 在结果页打开
                          </button>
                        )}
                        {t.response.sql && (
                          <button
                            type="button"
                            className="btn btn-ghost btn-tiny"
                            onClick={() => void copySql(t.response?.sql ?? '')}
                          >
                            <Copy size={13} aria-hidden="true" /> {copied ? '已复制' : '复制 SQL'}
                          </button>
                        )}
                      </div>
                    </>
                  )}
                </div>
              ),
            )}
          </div>

          <form
            className="agent-dock-composer"
            onSubmit={(e) => {
              e.preventDefault()
              void send()
            }}
          >
            <label className="visually-hidden" htmlFor="agent-input">
              向随行智能体提问
            </label>
            <textarea
              id="agent-input"
              ref={inputRef}
              className="agent-input"
              rows={2}
              placeholder="问当前项目的数据，例如：各渠道实收金额是多少？"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault()
                  void send()
                }
              }}
            />
            <div className="agent-dock-foot">
              <span className="mono">只读 · 带当前项目口径</span>
              {busy ? (
                <button type="button" className="btn btn-tiny btn-cancel" onClick={cancel}>
                  <Square size={12} aria-hidden="true" /> 取消
                </button>
              ) : (
                <button type="submit" className="btn btn-primary btn-tiny" disabled={!input.trim()}>
                  <Send size={12} aria-hidden="true" /> 发送
                </button>
              )}
            </div>
          </form>
        </section>
      )}
    </>
  )
}
