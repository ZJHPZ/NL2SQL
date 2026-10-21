/** 查询工作台主界面。
 *
 * 布局主张：**这不是聊天应用，是查询工作台。**
 *   * 左侧是"当前结果"——它是唯一主角，不被历史推走
 *   * 右侧是历史与知识库——可快速恢复，但不与结果抢注意力
 * 理由：开发者会反复扫结果、改 SQL 再跑；聊天流里没有"当前 SQL"
 * 这个稳定对象，滚两下就找不到刚才的结果了。
 */
import { useCallback, useEffect, useState } from 'react'
import { api } from './api'
import type {
  HistoryItem,
  InfoResponse,
  KnowledgeHit,
  KnowledgeItem,
  QueryResponse,
  TableInfo,
} from './types'
import { ResultTable } from './components/ResultTable'
import { SqlPanel } from './components/SqlPanel'
import { StatusBar } from './components/StatusBar'
import { HistoryPanel, KnowledgePanel } from './components/SidePanels'
import { PluginAdminPanel } from './components/PluginAdminPanel'

type SideTab = 'history' | 'knowledge' | 'admin'

export default function App() {
  const [info, setInfo] = useState<InfoResponse | null>(null)
  const [bootError, setBootError] = useState<string | null>(null)

  const [question, setQuestion] = useState('')
  const [now, setNow] = useState('2024-06-30')
  const [busy, setBusy] = useState(false)
  const [data, setData] = useState<QueryResponse | null>(null)
  const [reqError, setReqError] = useState<string | null>(null)

  const [history, setHistory] = useState<HistoryItem[]>([])
  const [tables, setTables] = useState<TableInfo[]>([])
  const [showTables, setShowTables] = useState(false)

  const [kbItems, setKbItems] = useState<KnowledgeItem[]>([])
  const [kbHits, setKbHits] = useState<KnowledgeHit[] | null>(null)
  const [tab, setTab] = useState<SideTab>('history')
  // 会话标识：agent 的澄清是多轮的，必须跨请求保留。
  // 首次提问为空，服务端生成后回传，我们记住并在下一轮带上。
  const [sessionId, setSessionId] = useState<string | null>(null)

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

  // ---------------------------------------------------------------- 动作
  const runQuery = async (override?: string) => {
    const q = (override ?? question).trim()
    if (!q || busy) return
    setBusy(true)
    setReqError(null)
    const r = await api.query({
      question: q,
      source_id: info?.default_source ?? null,
      // agent 模式下时间锚点由会话保留，这里仍传首轮锚点
      now: now || null,
      session_id: sessionId,
    })
    setBusy(false)
    if (!r.ok) {
      setReqError(`${r.error.error}: ${r.error.detail}`)
      return
    }
    setData(r.data)
    // 记住会话：下一轮（尤其澄清的回答）必须带上，否则上下文会断
    if (r.data.session_id) setSessionId(r.data.session_id)
    void reloadSide()
  }

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
        <h2>后端未就绪</h2>
        <pre>{bootError}</pre>
      </div>
    )
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          talk-to-sql
          {info && <span className="meta">v{info.version}</span>}
        </div>
        <div className="topbar-info">
          {info && (
            <>
              <span className="meta" title="实际生效的规划器">
                规划器 <b>{info.planner_mode}</b>
              </span>
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
        <span className="spacer" />
        {sessionId && (
          <span className="meta" title={`当前会话 ${sessionId}`}>
            会话 {sessionId.slice(0, 6)}
          </span>
        )}
        <button className="btn btn-ghost" onClick={newSession}
                title="清空当前会话，开始新的对话（旧上下文不再带入）">
          新会话
        </button>
        <button className="btn btn-ghost" onClick={() => setShowTables((v) => !v)}>
          {showTables ? '隐藏表结构' : '查看表结构'}
        </button>
        <button className="btn btn-ghost" onClick={refreshAll} title="刷新元数据快照并清空结果缓存">
          刷新快照/缓存
        </button>
      </header>

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
        <input
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
        <label className="nowfield" title="相对时间锚点：让「本月/上月」有确定含义，且可复现">
          时间锚点
          <input value={now} onChange={(e) => setNow(e.target.value)} />
        </label>
        <button className="btn btn-primary" disabled={busy || !question.trim()}
                onClick={() => void runQuery()}>
          {busy ? '查询中…' : '查询'}
        </button>
      </div>

      {reqError && <div className="alert alert-bad">{reqError}</div>}

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

          {busy && !data && <div className="loading">正在查询…</div>}

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
                  <div className="answer-body">{data.answer}</div>
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
    </div>
  )
}
