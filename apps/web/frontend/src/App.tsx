/** 查询工作台主界面。
 *
 * 布局主张：**这不是聊天应用，是查询工作台。**
 *   * 左侧是"当前结果"——它是唯一主角，不被历史推走
 *   * 右侧是历史与知识库——可快速恢复，但不与结果抢注意力
 * 理由：开发者会反复扫结果、改 SQL 再跑；聊天流里没有"当前 SQL"
 * 这个稳定对象，滚两下就找不到刚才的结果了。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  MessageSquarePlus,
  RefreshCw,
  Search,
  Table2,
  X,
} from 'lucide-react'
import { api, getProjectId, setProjectId, streamQuery } from './api'
import type {
  HistoryItem,
  InfoResponse,
  KnowledgeHit,
  KnowledgeItem,
  ProjectSummary,
  QueryResponse,
  StreamEvent,
  TableInfo,
} from './types'
import { Markdown } from './components/Markdown'
import { QuerySkeleton, StreamTimeline } from './components/QueryProgress'
import type { StageStep } from './components/QueryProgress'
import { ResultTable } from './components/ResultTable'
import { SqlPanel } from './components/SqlPanel'
import { StatusBar } from './components/StatusBar'
import { HistoryPanel, KnowledgePanel } from './components/SidePanels'
import { PluginAdminPanel } from './components/PluginAdminPanel'
import { ProjectSwitcher } from './components/ProjectSwitcher'
import { ProjectsPanel } from './components/ProjectsPanel'
import { TasksPanel } from './components/TasksPanel'

type SideTab = 'history' | 'knowledge' | 'admin'

/** 顶层三个视图。不引入 router：只有一个页面，切 tab 而已。
 *  项目/任务面板只在切到对应视图时挂载 → 数据按需加载，问答页不多打接口。
 */
type View = 'ask' | 'projects' | 'tasks'

const VIEWS: { id: View; label: string }[] = [
  { id: 'ask', label: '问答' },
  { id: 'projects', label: '项目' },
  { id: 'tasks', label: '任务' },
]

/** 执行器的中文名。
 * 只写"哪个调度"不够：用户要知道**这意味着什么**，
 * 所以 title 里给出图结构或"零依赖的 while 循环"。
 */
const RUNTIME_LABEL: Record<string, string> = {
  loop: '自写循环',
  langgraph: 'LangGraph',
}

/** 护栏规则的中文说法：让用户看懂"模型被机制顶回去了"是什么情况。 */
const GUARD_LABEL: Record<string, string> = {
  nodata: '还没查数据就想下结论，被要求先执行查询',
  narration: '输出像旁白而不是结论，被要求给出最终答案',
  deferred: '把关键步骤推给"下一步"，被要求现在做完',
  repeat: '重复调用同一个工具，被要求换做法',
}

/** QueryStatus 的中文短名（进度时间线与状态条共用口径）。 */
const OUTCOME_LABEL: Record<string, string> = {
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

/** 把一条 SSE 事件翻译成一行可读进度；result/done 不产生进度行。 */
function streamStage(ev: StreamEvent): StageStep | null {
  switch (ev.event) {
    case 'start':
      return { key: 'start', label: '已连上查询引擎，开始规划', state: 'done' }
    case 'decision': {
      const { step, kind, tool, thought } = ev.data
      if (kind === 'tool') {
        return {
          key: `decision-${step}`,
          label: `第 ${step} 步：决定调用工具`,
          detail: tool ?? '',
          state: 'done',
        }
      }
      if (kind === 'final') {
        return {
          key: `decision-${step}`,
          label: `第 ${step} 步：整理最终结论`,
          state: 'done',
        }
      }
      return {
        key: `decision-${step}`,
        label: `第 ${step} 步：${kind}`,
        detail: thought ?? undefined,
        state: 'done',
      }
    }
    case 'tool':
      return {
        key: `tool-${ev.data.step}`,
        label: `执行 ${ev.data.tool}`,
        detail: `${ev.data.ok ? '成功' : '失败'} · ${(ev.data.elapsed_ms / 1000).toFixed(1)}s`,
        state: ev.data.ok ? 'done' : 'warn',
      }
    case 'guard':
      return {
        key: `guard-${ev.data.step}-${ev.data.rule}`,
        label: '护栏纠正模型行为',
        detail: GUARD_LABEL[ev.data.rule] ?? ev.data.rule,
        state: 'warn',
      }
    case 'clarify':
      return {
        key: `clarify-${ev.data.step}`,
        label: '需要你补充信息',
        detail: ev.data.question,
        state: 'warn',
      }
    case 'outcome': {
      const notes = ev.data.notes ?? []
      return {
        key: 'outcome',
        label: `执行结束：${OUTCOME_LABEL[ev.data.status] ?? ev.data.status}`,
        detail: notes.length > 0 ? notes[notes.length - 1] : undefined,
        state: ev.data.status === 'ok' ? 'done' : 'warn',
      }
    }
    case 'model_retry':
      return {
        key: `retry-${ev.data.step}-${ev.data.attempt ?? ''}`,
        label: '模型调用重试',
        detail: `第 ${ev.data.attempt ?? '?'} 次尝试`,
        state: 'warn',
      }
    case 'model_repair':
      return { key: `repair-${ev.data.step}`, label: '模型输出格式修复', state: 'warn' }
    case 'tool_missing':
      return {
        key: `missing-${ev.data.step}`,
        label: '模型漏了工具名，要求重新选择',
        detail: ev.data.tool ?? undefined,
        state: 'warn',
      }
    case 'error':
      return { key: 'error', label: '运行时异常', detail: ev.data.message, state: 'error' }
    case 'result':
    case 'done':
      return null
  }
}

export default function App() {
  const [info, setInfo] = useState<InfoResponse | null>(null)
  const [bootError, setBootError] = useState<string | null>(null)

  const [question, setQuestion] = useState('')
  const [now, setNow] = useState('2024-06-30')
  const [busy, setBusy] = useState(false)
  const [data, setData] = useState<QueryResponse | null>(null)
  const [reqError, setReqError] = useState<string | null>(null)
  // 流式进度：steps 从 SSE 事件累积而来，elapsedMs 由计时器推进
  const [stages, setStages] = useState<StageStep[]>([])
  const [elapsedMs, setElapsedMs] = useState(0)
  const abortRef = useRef<AbortController | null>(null)

  const [history, setHistory] = useState<HistoryItem[]>([])
  const [tables, setTables] = useState<TableInfo[]>([])
  const [showTables, setShowTables] = useState(false)

  const [kbItems, setKbItems] = useState<KnowledgeItem[]>([])
  const [kbHits, setKbHits] = useState<KnowledgeHit[] | null>(null)
  const [tab, setTab] = useState<SideTab>('history')
  // 会话标识：agent 的澄清是多轮的，必须跨请求保留。
  // 首次提问为空，服务端生成后回传，我们记住并在下一轮带上。
  const [sessionId, setSessionId] = useState<string | null>(null)

  // ---------------------------------------------------------------- 视图与项目（M7）
  const [view, setView] = useState<View>('ask')
  // ref 与 state 同步保存当前项目：loadProjects 的依赖为空，
  // 避免切换项目 -> 重载项目列表 -> 列表回调再触发切项目的循环。
  const projectIdRef = useRef<string>(getProjectId())
  const [projectId, setProjectIdState] = useState<string>(projectIdRef.current)
  const [projects, setProjects] = useState<ProjectSummary[]>([])
  const [defaultProjectId, setDefaultProjectId] = useState<string>('default')
  const [projectsLoading, setProjectsLoading] = useState(false)
  const [projectsError, setProjectsError] = useState<string | null>(null)

  // ---------------------------------------------------------------- 启动
  useEffect(() => {
    ;(async () => {
      const r = await api.info()
      if (!r.ok) {
        setBootError(
          `无法连接后端（${r.error.error}）: ${r.error.detail}。` +
            `请确认 FastAPI 已启动：uvicorn apps.web.backend.main:app --port 8000`,
        )
        return
      }
      setInfo(r.data)
    })()
  }, [])

  const reloadSide = useCallback(async () => {
    const [h, k, t] = await Promise.all([
      api.history(info?.default_source ?? null, 40),
      api.knowledgeList({ includeDisabled: true }),
      api.tables(info?.default_source ?? null),
    ])
    if (h.ok) setHistory(h.data)
    if (k.ok) setKbItems(k.data)
    if (t.ok) setTables(t.data)
  }, [info?.default_source])

  useEffect(() => {
    if (info) void reloadSide()
  }, [info, reloadSide])

  // ---------------------------------------------------------------- 项目（M7）
  /** 拉项目列表。失败只记错误不白屏：后端没实现 /api/projects 时，
   *  问答页必须照常可用，只是顶栏显示"项目列表加载失败"。 */
  const loadProjects = useCallback(async () => {
    setProjectsLoading(true)
    const r = await api.listProjects()
    setProjectsLoading(false)
    if (!r.ok) {
      setProjectsError(`${r.error.error}: ${r.error.detail}`)
      return
    }
    setProjectsError(null)
    const def = r.data.default_project_id || 'default'
    setDefaultProjectId(def)
    const list = r.data.projects ?? []
    setProjects(list)
    // 当前项目已被删除 / 不在本机时回退默认项目，否则所有接口都会 404
    const cur = projectIdRef.current
    if (!list.some((p) => p.id === cur) && cur !== def) {
      projectIdRef.current = def
      setProjectId(def)
      setProjectIdState(def)
    }
  }, [])

  useEffect(() => {
    void loadProjects()
  }, [loadProjects])

  /** 切换项目：持久化到 localStorage，并清掉上一个项目的问答上下文。
   *  旧结果属于旧项目的数据源，留在屏幕上会让人把两组数据搞混。
   */
  const switchProject = useCallback(
    (id: string) => {
      if (!id || id === projectIdRef.current) return
      projectIdRef.current = id
      setProjectId(id)
      setProjectIdState(id)
      setData(null)
      setSessionId(null)
      setReqError(null)
      setShowTables(false)
      // 历史 / 表结构 / 知识库都是项目维度（请求带 X-Project-Id），换项目后重拉
      void reloadSide()
    },
    [reloadSide],
  )

  // ---------------------------------------------------------------- 动作
  /** 提问：走 SSE 流式接口。
   *
   * 为什么不用一次性 POST：agent 一次问答 2~10 秒，界面必须一连接上就有反馈。
   * 事件按顺序推到 `stages`，用户能看到"选工具 → 执行 → 整理"每一步，
   * 而且首字节是后端**立即**发的（不等模型），空窗期从数秒降到几十毫秒。
   */
  const runQuery = async (override?: string) => {
    const q = (override ?? question).trim()
    if (!q || busy) return
    setBusy(true)
    setReqError(null)
    setStages([])
    setElapsedMs(0)

    // 本地数组累积后再整体 set：避免连续事件下多次浅拷贝打断 React 批处理
    const local: StageStep[] = []
    const push = (s: StageStep) => {
      const i = local.findIndex((x) => x.key === s.key)
      if (i >= 0) local[i] = s
      else local.push(s)
      setStages([...local])
    }

    const controller = new AbortController()
    abortRef.current = controller
    const startedAt = performance.now()
    const timer = window.setInterval(() => {
      setElapsedMs(performance.now() - startedAt)
    }, 120)

    const r = await streamQuery(
      {
        question: q,
        source_id: info?.default_source ?? null,
        // agent 模式下时间锚点由会话保留，这里仍传首轮锚点
        now: now || null,
        session_id: sessionId,
      },
      {
        signal: controller.signal,
        onEvent: (ev) => {
          const stage = streamStage(ev)
          if (stage) push(stage)
        },
      },
    )

    window.clearInterval(timer)
    abortRef.current = null
    setElapsedMs(performance.now() - startedAt)
    setBusy(false)

    if (!r.ok) {
      if (r.error.error === 'aborted') {
        // 用户主动取消不是错误：不弹红色告警，只在进度里说明
        push({
          key: 'canceled',
          label: '已取消（后端线程仍会跑完，本次结果不进入界面）',
          state: 'warn',
        })
        return
      }
      setReqError(`${r.error.error}: ${r.error.detail}`)
      return
    }
    setData(r.data)
    // 记住会话：下一轮（尤其澄清的回答）必须带上，否则上下文会断
    if (r.data.session_id) setSessionId(r.data.session_id)
    void reloadSide()
  }

  /** 取消当前流式查询：立刻恢复可操作，不再等结果。 */
  const cancelQuery = () => {
    abortRef.current?.abort()
  }

  // 卸载时中断进行中的流，避免对已卸载组件 setState
  useEffect(() => () => abortRef.current?.abort(), [])

  /** 从澄清选项里选一个作为回答。 */
  const answerClarification = (text: string) => {
    setQuestion(text)
    void runQuery(text)
  }

  /** 清空会话，开始新的一轮对话（避免旧上下文干扰新问题）。 */
  const newSession = () => {
    setSessionId(null)
    setData(null)
    setQuestion('')
    setReqError(null)
  }

  const rerunSql = async (sql: string) => {
    if (busy) return
    setBusy(true)
    setReqError(null)
    const r = await api.execute({
      sql,
      source_id: info?.default_source ?? null,
    })
    setBusy(false)
    if (!r.ok) {
      setReqError(`${r.error.error}: ${r.error.detail}`)
      return
    }
    setData(r.data)
    void reloadSide()
  }

  const restore = (h: HistoryItem) => {
    setQuestion(h.question)
    if (h.sql) {
      // 历史里带 SQL：直接把它当作"当前结果"的依据重跑一次，
      // 这样用户能看到当时的表格，而不是只有一句问题
      void rerunSql(h.sql)
    } else {
      void runQuery()
    }
  }

  const testKnowledge = async (text: string) => {
    const r = await api.knowledgeTest({
      text,
      source_id: info?.default_source ?? null,
      top_k: 6,
    })
    setKbHits(r.ok ? r.data : [])
  }

  const toggleKnowledge = async (key: string, enabled: boolean) => {
    await api.knowledgeToggle(key, enabled)
    const k = await api.knowledgeList({ includeDisabled: true })
    if (k.ok) setKbItems(k.data)
  }

  const refreshAll = async () => {
    await api.refreshSnapshot(info?.default_source ?? null, false)
    await api.refreshCache()
    void reloadSide()
  }

  // ---------------------------------------------------------------- 渲染
  if (bootError) {
    return (
      <div className="boot-error">
        <h1>后端未就绪</h1>
        <pre>{bootError}</pre>
      </div>
    )
  }

  return (
    <div className="app">
      <a className="skip-link" href="#main-content">
        跳到主内容
      </a>
      <header className="topbar">
        <div className="brand">
          talk-to-sql
          {info && <span className="meta">v{info.version}</span>}
        </div>
        <nav className="topnav">
          {VIEWS.map((v) => (
            <button
              key={v.id}
              className={view === v.id ? 'navbtn active' : 'navbtn'}
              onClick={() => setView(v.id)}
            >
              {v.label}
            </button>
          ))}
        </nav>
        <div className="topbar-info">
          {info && (
            <>
              <span className="meta" title="实际生效的规划器">
                规划器 <b>{info.planner_mode}</b>
              </span>
              {info.runtime?.name && (
                <span
                  className="meta"
                  title={
                    (info.runtime.note ? `⚠ ${info.runtime.note}\n` : '')
                    + (info.runtime.graph?.nodes?.length
                      ? `图执行器：${info.runtime.graph.nodes
                        .map((n) => n.name)
                        .join(' → ')}（${info.runtime.graph.edges?.length ?? 0} 条边）`
                      : '顺序执行器：自己写的 while 循环，零第三方依赖')
                  }
                >
                  执行器{' '}
                  <b>{RUNTIME_LABEL[info.runtime.name] ?? info.runtime.name}</b>
                  {/* 降级必须显式标注：否则用户以为在跑图执行器 */}
                  {info.runtime.note && (
                    <span className="badge badge-warn"> 已降级</span>
                  )}
                </span>
              )}
              <span className="meta" title={info.llm_available ? info.llm_model : '未配置 API Key'}>
                模型 <b>{info.llm_available ? info.llm_model : '未接入'}</b>
              </span>
              <span className="meta" title="强制行数上限">
                上限 <b>{info.safety.max_rows}</b>
              </span>
              {info.safety.read_only && (
                <span className="badge badge-ok" title="所有查询均为只读">
                  只读
                </span>
              )}
              <span className="meta" title="元数据快照">
                表 <b>{info.snapshot.table_count ?? 0}</b>
              </span>
              {info.semantic?.source && (
                <span
                  className="meta"
                  title={
                    `指标 ${info.semantic.metrics ?? 0} 个 / 维度 `
                    + `${info.semantic.dimensions ?? 0} 个。`
                    + (info.semantic.structural_metrics?.length
                      ? `dbt 里没有等价物的指标：`
                        + info.semantic.structural_metrics.join('、')
                      : '全部指标都能在 dbt 里表达')
                  }
                >
                  口径 <b>{info.semantic.source}</b>
                  {typeof info.semantic.portability === 'number' && (
                    <> {Math.round(info.semantic.portability * 100)}%</>
                  )}
                </span>
              )}
            </>
          )}
        </div>
        <ProjectSwitcher
          projectId={projectId}
          projects={projects}
          defaultProjectId={defaultProjectId}
          loading={projectsLoading}
          error={projectsError}
          onChange={switchProject}
          onReload={() => void loadProjects()}
        />
        <span className="spacer" />
        {sessionId && (
          <span className="meta" title={`当前会话 ${sessionId}`}>
            会话 {sessionId.slice(0, 6)}
          </span>
        )}
        <button className="btn btn-ghost" onClick={newSession}
                title="清空当前会话，开始新的对话（旧上下文不再带入）">
          <MessageSquarePlus size={14} aria-hidden="true" />
          新会话
        </button>
        <button className="btn btn-ghost" onClick={() => setShowTables((v) => !v)}
                aria-expanded={showTables}>
          <Table2 size={14} aria-hidden="true" />
          {showTables ? '隐藏表结构' : '查看表结构'}
        </button>
        <button className="btn btn-ghost" onClick={refreshAll} title="刷新元数据快照并清空结果缓存">
          <RefreshCw size={14} aria-hidden="true" />
          刷新快照/缓存
        </button>
      </header>

      {view === 'ask' && (
        <>
      <main id="main-content" className="ask-main" aria-busy={busy}>
      <h1 className="visually-hidden">talk-to-sql 查询工作台</h1>
      {/* 引擎装配失败：必须把**原因原文**摆出来。
          早先这种情况只显示"核心引擎未就绪"，用户完全不知道是哪个文件写错了
          （比如 dbt 指标文件路径写错），只能去翻服务端日志。 */}
      {info && info.engine_ready === false && (
        <div className="alert alert-bad boot-fail">
          <strong>核心引擎没有装配成功</strong>
          {info.startup_warnings.length > 0 ? (
            <ul className="tight">
              {info.startup_warnings.map((w, i) => <li key={i}>{w}</li>)}
            </ul>
          ) : (
            <div>没有更多信息，请查看服务端启动日志。</div>
          )}
          <div className="meta">
            常见原因：数据源文件路径不对、dbt 指标文件（NL2SQL_METRICS /
            semantic_path）不存在或写错了。
          </div>
        </div>
      )}

      <div className="askbar">
        <label className="visually-hidden" htmlFor="ask-input">自然语言问题</label>
        <input
          id="ask-input"
          className="ask-input"
          value={question}
          placeholder="用自然语言问一句，例如：2024年6月，数码品类的实收金额是多少？"
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              void runQuery()
            }
          }}
        />
        <label className="nowfield" htmlFor="now-input"
               title="相对时间锚点：让「本月/上月」有确定含义，且可复现">
          时间锚点
          <input id="now-input" value={now} onChange={(e) => setNow(e.target.value)} />
        </label>
        {busy ? (
          <button className="btn btn-cancel" onClick={cancelQuery}
                  title="取消本次查询（后端线程仍会跑完，结果不再进入界面）">
            <X size={14} aria-hidden="true" />
            取消
          </button>
        ) : (
          <button className="btn btn-primary" disabled={!question.trim()}
                  onClick={() => void runQuery()}>
            <Search size={14} aria-hidden="true" />
            查询
          </button>
        )}
      </div>

      {reqError && (
        <div className="alert alert-bad" role="alert">
          {reqError}
        </div>
      )}

      {showTables && (
        <div className="tables-panel">
          {tables.map((t) => (
            <div className="table-card" key={t.name}>
              <div className="table-card-head">
                <b className="mono">{t.name}</b>
                <span className="meta">
                  {t.row_estimate ?? '?'} 行 · {t.column_count} 列
                </span>
              </div>
              <div className="chips">
                {t.columns.map((c) => (
                  <span
                    className="chip mono"
                    key={c.name}
                    title={`${c.data_type}${c.is_primary_key ? ' · 主键' : ''}${
                      c.generated ? ' · 生成列' : ''
                    }`}
                  >
                    {c.name}
                    {c.is_primary_key && ' 🔑'}
                    {c.generated && ' ƒ'}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="main">
        <section className="work">
          {!data && !busy && (
            <div className="welcome">
              <h2>查询工作台</h2>
              <p>
                问一句自然语言，得到 <b>SQL + 结果表</b>。
                下面这些问题语义层已经覆盖，可以直接试：
              </p>
              <ul className="examples">
                {[
                  '2024年6月，数码品类的实收金额是多少？',
                  '2024年上半年，各渠道的实收金额和有效订单数是多少？按实收金额降序排列。',
                  '2024年上半年，每月的有效订单数和实收金额是多少？按月份升序排列。',
                  '2024年上半年，消费金额最高的10位客户是谁？',
                ].map((q) => (
                  <li key={q}>
                    <button className="linkbtn" onClick={() => setQuestion(q)}>
                      {q}
                    </button>
                  </li>
                ))}
              </ul>
              <p className="meta">
                提示：无法解析时工具会明确说「未能理解」，而不会编一条看起来对的 SQL。
              </p>
            </div>
          )}

          {/* 屏幕阅读器播报：视觉进度条本身 aria-live，但结果状态也要读出来 */}
          <div className="visually-hidden" role="status" aria-live="polite">
            {busy
              ? '正在查询'
              : data
                ? `查询完成：${OUTCOME_LABEL[data.status] ?? data.status}`
                : ''}
          </div>

          {busy && (
            <div className="stream-box">
              <StreamTimeline steps={stages} elapsedMs={elapsedMs} />
              {!data && <QuerySkeleton />}
            </div>
          )}

          {!busy && stages.length > 0 && (
            <details className="stream-done">
              <summary>
                执行过程（{stages.length} 步 · {(elapsedMs / 1000).toFixed(1)}s）
              </summary>
              <StreamTimeline steps={stages} elapsedMs={elapsedMs} compact />
            </details>
          )}

          {data && (
            <>
              <StatusBar data={data} onPickOption={answerClarification} />
              {data.answer && (
                <div className="answer">
                  <div className="answer-head">
                    <span className="answer-title">结论</span>
                    <span className="spacer" />
                    <span className="meta">
                      智能体的自然语言回答；下方结果表是同一份数据的明细
                    </span>
                  </div>
                  <Markdown className="answer-body" text={data.answer} />
                </div>
              )}
              <SqlPanel sql={data.sql} onRerun={rerunSql} busy={busy} />
              {data.result ? (
                <ResultTable result={data.result} />
              ) : (
                data.status !== 'need_clarification' && (
                  <div className="empty-block">
                    <div className="empty-title">
                      {data.status === 'unresolved'
                        ? '没能把这个问题映射到语义层'
                        : '本次没有可展示的结果表'}
                    </div>
                    <div className="empty-hint">
                      {data.status === 'unresolved'
                        ? '看上方提示里的建议——工具不会用一条看起来合理但答非所问的 SQL 来掩盖它没听懂。'
                        : '上方结论里可能已经有答案；没有结果表说明这次没有可结构化的数据。'}
                    </div>
                  </div>
                )
              )}
            </>
          )}
        </section>

        <aside className="side">
          <div className="tabs">
            <button
              className={tab === 'history' ? 'tab active' : 'tab'}
              onClick={() => setTab('history')}
            >
              历史 <span className="meta">{history.length}</span>
            </button>
            <button
              className={tab === 'knowledge' ? 'tab active' : 'tab'}
              onClick={() => setTab('knowledge')}
            >
              知识库 <span className="meta">{kbItems.length}</span>
            </button>
            <button
              className={tab === 'admin' ? 'tab active' : 'tab'}
              onClick={() => setTab('admin')}
              title="插件、工具、能力与运行参数"
            >
              插件与工具
            </button>
          </div>
          {tab === 'history' && <HistoryPanel items={history} onRestore={restore} />}
          {tab === 'knowledge' && (
            <KnowledgePanel
              items={kbItems}
              hits={kbHits}
              onTest={testKnowledge}
              onToggle={toggleKnowledge}
              busy={busy}
            />
          )}
          {tab === 'admin' && <PluginAdminPanel />}
        </aside>
      </div>
      </main>
        </>
      )}

      {view === 'projects' && (
        <main id="main-content" className="main">
          <h1 className="visually-hidden">项目管理与数据源</h1>
          <ProjectsPanel
            key={projectId}
            projectId={projectId}
            onSelectProject={switchProject}
            onProjectsChanged={() => void loadProjects()}
          />
        </main>
      )}

      {view === 'tasks' && (
        <main id="main-content" className="main">
          <h1 className="visually-hidden">SQL 任务</h1>
          <TasksPanel
            key={projectId}
            projectId={projectId}
            isDefaultProject={projectId === defaultProjectId}
          />
        </main>
      )}
    </div>
  )
}
