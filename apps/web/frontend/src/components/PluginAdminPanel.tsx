/** 插件与工具管理面板。
 *
 * 三条需求对应三块界面：
 *   看得见 —— 工具清单（含来源、能力、为什么没注册）+ 插件清单 + 加载错误
 *   管得了 —— 逐个启用/停用（停用主力工具时给出退化警告）
 *   配得了 —— 循环参数、能力授予、配置文件原文
 *
 * 一个刻意的设计：**能力授予不做任何阻拦**，但会把"解锁什么"与
 * "还有哪些防线"摆在旁边。面向个人工作者，用户是自己数据的主人；
 * 我们能做的是让他**知情**，而不是替他决定。
 */
import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type {
  AdminStats, CapabilityInfo, PluginInfo, SkillInfo, ToolInfo, ToolsetInfo,
} from '../types'

type SubTab = 'tools' | 'capabilities' | 'stats' | 'settings'

const pct = (x: number) => `${(x * 100).toFixed(1)}%`

function RiskBadge({ risk }: { risk: string }) {
  const label = risk === 'high' ? '高危' : risk === 'medium' ? '中' : '低'
  const cls = risk === 'high' ? 'badge-bad' : risk === 'medium' ? 'badge-warn' : 'badge-ok'
  return <span className={`badge ${cls}`}>{label}</span>
}

export function PluginAdminPanel() {
  const [data, setData] = useState<ToolsetInfo | null>(null)
  const [tab, setTab] = useState<SubTab>('tools')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [cfgText, setCfgText] = useState('')
  const [cfgPath, setCfgPath] = useState('')
  const [stats, setStats] = useState<AdminStats | null>(null)
  const [statsDays, setStatsDays] = useState(30)
  const [loop, setLoop] = useState({ max_steps: 8, max_repeat: 2, temperature: 0 })

  const load = useCallback(async () => {
    const r = await api.adminTools()
    if (!r.ok) {
      setErr(`${r.error.error}: ${r.error.detail}`)
      return
    }
    setData(r.data)
    setLoop({
      max_steps: Number(r.data.loop?.max_steps ?? 8),
      max_repeat: Number(r.data.loop?.max_repeat ?? 2),
      temperature: Number(r.data.loop?.temperature ?? 0),
    })
    const c = await api.adminConfig()
    if (c.ok) {
      setCfgText(c.data.text)
      setCfgPath(c.data.path)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  // 统计按需加载：管理页打开时不必为一个没人看的标签多打一次接口。
  useEffect(() => {
    if (tab !== 'stats' || stats) return
    void api.adminStats(statsDays).then((r) => {
      if (!r.ok) { setErr(`${r.error.error}: ${r.error.detail}`); return }
      setStats(r.data)
    })
  }, [tab, stats, statsDays])

  const reloadStats = (days = statsDays) => {
    setStats(null)
    setStatsDays(days)
  }

  const act = async (fn: () => Promise<void>) => {
    setBusy(true); setErr(null); setMsg(null)
    try { await fn() } finally { setBusy(false) }
  }

  const toggleTool = (name: string, enabled: boolean) =>
    act(async () => {
      const r = await api.adminToggleTool(name, enabled)
      if (!r.ok) { setErr(`${r.error.error}: ${r.error.detail}`); return }
      if (r.data.warning) setMsg(r.data.warning)
      else setMsg(`${name} 已${enabled ? '启用' : '停用'}（下次请求生效）`)
      await load()
    })

  const togglePlugin = (name: string, enabled: boolean) =>
    act(async () => {
      const r = await api.adminTogglePlugin(name, enabled)
      if (!r.ok) { setErr(`${r.error.error}: ${r.error.detail}`); return }
      setMsg(enabled
        ? `${name} 已启用（下次请求生效）。它的工具会出现在模型清单里，重启后依然启用。`
        : `${name} 已停用（下次请求生效）。工具集已收窄，配置文件里也记下了。`)
      await load()
    })

  const toggleCap = (name: string, granted: boolean) =>
    act(async () => {
      const r = granted
        ? await api.adminCapabilities({ grant: [name] })
        : await api.adminCapabilities({ revoke: [name] })
      if (!r.ok) { setErr(`${r.error.error}: ${r.error.detail}`); return }
      if (r.data.notice) setMsg(r.data.notice)
      else setMsg(`${name} 已${granted ? '授予' : '收回'}`)
      if (r.data.reload_required) {
        await api.adminReload()
        setMsg((m) => `${m ?? ''} 工具集已重新装配。`.trim())
      }
      await load()
    })

  const saveLoop = () =>
    act(async () => {
      const r = await api.adminLoop(loop)
      if (!r.ok) { setErr(`${r.error.error}: ${r.error.detail}`); return }
      setMsg('循环参数已保存并热生效（无需重启）')
      await load()
    })

  const saveConfig = () =>
    act(async () => {
      const r = await api.adminConfigSave(cfgText)
      if (!r.ok) { setErr(`${r.error.error}: ${r.error.detail}`); return }
      setMsg('配置已保存并生效（非法内容会被拒绝，原文件不受影响）')
      await load()
    })

  if (!data) {
    return <div className="side-empty">{err ?? '加载中…'}</div>
  }

  return (
    <div className="admin">
      <div className="tabs sub">
        <button className={tab === 'tools' ? 'tab active' : 'tab'}
                onClick={() => setTab('tools')}>
          工具 <span className="meta">{data.tools.length}</span>
        </button>
        <button className={tab === 'capabilities' ? 'tab active' : 'tab'}
                onClick={() => setTab('capabilities')}>
          能力
        </button>
        <button className={tab === 'stats' ? 'tab active' : 'tab'}
                onClick={() => setTab('stats')}>
          使用统计
        </button>
        <button className={tab === 'settings' ? 'tab active' : 'tab'}
                onClick={() => setTab('settings')}>
          参数与配置
        </button>
      </div>

      {msg && <div className="alert alert-info">{msg}</div>}
      {err && <div className="alert alert-bad">{err}</div>}

      {/* ------------------------------------------------------------ 工具 */}
      {tab === 'tools' && (
        <>
          <div className="admin-section">
            <div className="admin-title">
              插件 <span className="meta">{data.plugins.length} 已启用</span>
              <span className="spacer" />
              <span className="meta">
                外部插件默认不加载 —— 插件是可执行代码，显式启用才运行
              </span>
            </div>

            {data.plugins.length === 0 && data.plugins_available.length === 0 && (
              <div className="meta">
                plugins/ 下没有插件。放一个目录（含 plugin.toml 与 plugin.py）
                即可扩展工具集；**新增插件需重启进程**。
              </div>
            )}

            {data.plugins.map((p: PluginInfo) => (
              <div className="plugin-card" key={p.name}>
                <div className="plugin-head">
                  <b>{p.name}</b>
                  <span className="meta">v{p.version}</span>
                  <span className="badge badge-ok">已启用</span>
                  <span className="spacer" />
                  <button className="btn btn-tiny" disabled={busy}
                          onClick={() => void togglePlugin(p.name, false)}>
                    停用
                  </button>
                </div>
                {p.description && <div className="plugin-desc">{p.description}</div>}
                <div className="chips">
                  {p.requires.map((c) => (
                    <span className="chip" key={c} title="插件声明的所需能力">
                      {c}
                    </span>
                  ))}
                </div>
                {p.warnings.length > 0 && (
                  <div className="meta warn-text">{p.warnings.join('；')}</div>
                )}
              </div>
            ))}

            {data.plugins_available.length > 0 && (
              <>
                <div className="admin-hint" style={{ marginTop: 10 }}>
                  磁盘上还有 {data.plugins_available.length} 个**未启用**的插件：
                </div>
                {data.plugins_available.map((p: PluginInfo) => (
                  <div className="plugin-card dim" key={p.name}>
                    <div className="plugin-head">
                      <b>{p.name}</b>
                      <span className="meta">v{p.version}</span>
                      <span className="badge badge-warn">未启用</span>
                      <span className="spacer" />
                      <button className="btn btn-tiny" disabled={busy}
                              onClick={() => void togglePlugin(p.name, true)}>
                        启用
                      </button>
                    </div>
                    {p.description && <div className="plugin-desc">{p.description}</div>}
                    {p.requires.length > 0 && (
                      <div className="chips">
                        {p.requires.map((c) => (
                          <span className="chip" key={c}>{c}</span>
                        ))}
                      </div>
                    )}
                    {p.reason && <div className="meta">{p.reason}</div>}
                  </div>
                ))}
              </>
            )}

            {data.plugin_errors.length > 0 && (
              <div className="alert alert-warn">
                <strong>插件加载问题</strong>
                <ul className="tight">
                  {data.plugin_errors.map((e, i) => <li key={i}>{e}</li>)}
                </ul>
              </div>
            )}
          </div>

          <div className="admin-section">
            <div className="admin-title">
              技能 <span className="meta">{data.skills.length} 已加载</span>
              <span className="spacer" />
              <span className="meta">
                技能是「这类任务该怎么做」的流程；知识库是「这次要用到什么事实」
              </span>
            </div>

            {data.skills.length === 0 && (
              <div className="meta">
                skills/ 下没有技能（机制已就绪，但还没有内容）。
                放一份 <code className="mono">skills/&lt;名字&gt;/SKILL.md</code> 即可；
                格式见 <code className="mono">skills/README.md</code>。
                <br />
                注意：没有技能时 <code className="mono">skill.load</code> 工具
                **不会注册**——把它留在模型清单里只会让它白花一步去试。
              </div>
            )}

            {data.skills.map((s: SkillInfo) => (
              <div className="plugin-card" key={s.name}>
                <div className="plugin-head">
                  <b>{s.name}</b>
                  <span className={`badge ${s.origin === 'project'
                    ? 'badge-ok' : 'badge-warn'}`}>
                    {s.origin === 'project' ? '项目' : s.origin}
                  </span>
                  {s.scope && <span className="chip">仅 {s.scope}</span>}
                  <span className="spacer" />
                  <span className="meta">{s.body_chars} 字</span>
                </div>
                {/* 描述必须显示：它就是"这个技能什么时候被用上"的唯一依据 */}
                <div className="plugin-desc">{s.description}</div>
                {s.requires.length > 0 && (
                  <div className="chips">
                    {s.requires.map((c) => (
                      <span className="chip" key={c} title="技能声明所需能力">
                        {c}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ))}

            {(data.skill_report?.skipped?.length ?? 0) > 0 && (
              <details className="notes">
                <summary>
                  未加载（{data.skill_report.skipped!.length}）
                </summary>
                <ul className="tight">
                  {data.skill_report.skipped!.map((x, i) => (
                    <li key={i}>
                      <span className="mono">{x.name}</span>：{x.reason}
                    </li>
                  ))}
                </ul>
              </details>
            )}

            {(data.skill_report?.rejected?.length ?? 0) > 0 && (
              <div className="alert alert-warn">
                <strong>技能加载失败</strong>
                <ul className="tight">
                  {data.skill_report.rejected!.map((x, i) => (
                    <li key={i}>
                      <span className="mono">{x.name}</span>：{x.reason}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {(data.skill_report?.prompt_issues?.length ?? 0) > 0 && (
              <div className="alert alert-warn">
                <strong>外置 Prompt 没有生效</strong>
                <ul className="tight">
                  {data.skill_report.prompt_issues!.map((w, i) => (
                    <li key={i}>{w}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>

          <div className="admin-section">
            <div className="admin-title">
              工具 <span className="meta">{data.tools.length}</span>
              <span className="spacer" />
              <span className="meta">
                停用后该工具不会出现在模型的工具清单里
              </span>
            </div>
            <ul className="tool-list">
              {data.tools.map((t: ToolInfo) => (
                <li key={t.name} className={t.registered ? '' : 'disabled'}>
                  <div className="tool-head">
                    <label className="switch">
                      <input type="checkbox" checked={t.enabled}
                             disabled={busy}
                             onChange={(e) => void toggleTool(t.name, e.target.checked)} />
                      <span className="mono">{t.name}</span>
                    </label>
                    <RiskBadge risk={t.risk} />
                    <span className="chip">{t.origin}</span>
                    {!t.registered && <span className="badge badge-warn">未注册</span>}
                  </div>
                  <div className="tool-desc">{t.description}</div>
                  <div className="tool-meta">
                    <span className="meta">需要 {t.requires.join('、') || '（无）'}</span>
                    {t.reason && <span className="meta warn-text">· {t.reason}</span>}
                  </div>
                </li>
              ))}
            </ul>
          </div>
        </>
      )}

      {/* ------------------------------------------------------------ 能力 */}
      {tab === 'capabilities' && (
        <div className="admin-section">
          <div className="admin-title">能力授予</div>
          <div className="admin-hint">
            勾选即授予，取消即收回。收回可能让依赖它的工具不再注册，
            我们会提示出来。**这是你的数据，你决定授什么权。**
          </div>

          <div className="safety-box">
            <div className="safety-title">还有几道防线</div>
            <div className="safety-row">
              <span>只读守门（guard.read_only）</span>
              <b className={data.safety.read_only ? 'ok-text' : 'warn-text'}>
                {data.safety.read_only ? '开启' : '已关闭'}
              </b>
            </div>
            <div className="safety-row">
              <span>存在实现写操作的工具</span>
              <b className={data.safety.write_tools_implemented ? 'warn-text' : 'ok-text'}>
                {data.safety.write_tools_implemented ? '是' : '否'}
              </b>
            </div>
            <div className="safety-row">
              <span>强制行数上限</span><b>{data.safety.max_rows}</b>
            </div>
            <div className="safety-note">{data.safety.note}</div>
          </div>

          <ul className="cap-list">
            {data.capabilities.map((c: CapabilityInfo) => (
              <li key={c.name}>
                <label className="switch">
                  <input type="checkbox" checked={c.granted} disabled={busy}
                         onChange={(e) => void toggleCap(c.name, e.target.checked)} />
                  <span className="mono">{c.name}</span>
                </label>
                <RiskBadge risk={c.risk} />
                <div className="cap-desc">
                  {c.description}
                  {c.unlocks && <div className="meta">解锁：{c.unlocks}</div>}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* ------------------------------------------------------------ 统计 */}
      {tab === 'stats' && (
        <div className="admin-section">
          <div className="admin-title">
            使用统计
            <span className="spacer" />
            <span className="meta">
              口径：追问单列，不算失败；状态零填充
            </span>
          </div>
          <div className="admin-hint">
            数据来自观测日志（只追加，不改写）。看趋势用这里，看单条失败
            去查询页的归因区。
          </div>

          <div className="form-row">
            <label>
              回看
              <select value={statsDays} disabled={busy}
                      onChange={(e) => reloadStats(Number(e.target.value))}>
                <option value={1}>最近 1 天</option>
                <option value={7}>最近 7 天</option>
                <option value={30}>最近 30 天</option>
                <option value={90}>最近 90 天</option>
              </select>
              <span className="meta">默认 30 天</span>
            </label>
          </div>

          {!stats && <div className="meta">加载中…</div>}

          {stats && stats.total === 0 && (
            <div className="meta">
              这段时间没有查询记录。先去问一个问题，数据就有了。
            </div>
          )}

          {stats && stats.total > 0 && (
            <>
              <div className="safety-box">
                <div className="safety-title">总览</div>
                <div className="safety-row">
                  <span>问答总数</span><b>{stats.total}</b>
                </div>
                <div className="safety-row">
                  <span>成功率</span><b className="ok-text">{pct(stats.success_rate)}</b>
                </div>
                <div className="safety-row">
                  <span>失败率</span>
                  <b className={stats.failure_rate > 0.1 ? 'warn-text' : ''}>
                    {pct(stats.failure_rate)}
                  </b>
                </div>
                <div className="safety-row">
                  <span>追问（不算失败）</span><b>{stats.clarify}</b>
                </div>
                <div className="safety-row">
                  <span>缓存命中</span><b>{pct(stats.cached_rate)}</b>
                </div>
                <div className="safety-row">
                  <span>延迟 P50 / P95</span>
                  <b>{stats.latency_ms.p50} / {stats.latency_ms.p95} ms</b>
                </div>
                <div className="safety-note">
                  平均延迟 {stats.latency_ms.avg} ms ·
                  规划器 {stats.planner_mode} · 执行器 {stats.runtime || '—'}
                </div>
              </div>

              <div className="admin-title" style={{ marginTop: 12 }}>
                状态分布
              </div>
              <div className="chips">
                {Object.entries(stats.by_status).map(([k, v]) => (
                  <span className="chip mono" key={k} title={k}>
                    {k} {v}
                  </span>
                ))}
              </div>

              {stats.tools.total > 0 && (
                <>
                  <div className="admin-title" style={{ marginTop: 12 }}>
                    工具调用 <span className="meta">{stats.tools.total} 次，失败率 {pct(stats.tools.failure_rate)}</span>
                  </div>
                  <ul className="tool-list">
                    {Object.entries(stats.tools.by_tool).map(([name, t]) => (
                      <li key={name}>
                        <div className="tool-head">
                          <span className="mono">{name}</span>
                          <span className="meta">{t.calls} 次</span>
                          {t.failures > 0 && (
                            <span className="badge badge-warn">
                              失败率 {pct(t.failure_rate)}
                            </span>
                          )}
                          <span className="spacer" />
                          <span className="meta">
                            平均 {t.avg_ms} ms · P95 {t.p95_ms} ms
                          </span>
                        </div>
                      </li>
                    ))}
                  </ul>
                </>
              )}

              <div className="admin-title" style={{ marginTop: 12 }}>
                最近失败 <span className="meta">{stats.recent_failures.length} 条</span>
              </div>
              {stats.recent_failures.length === 0 ? (
                <div className="meta">这段时间没有失败。挺好的。</div>
              ) : (
                <ul className="tool-list">
                  {stats.recent_failures.map((f) => (
                    <li key={`${f.created_at}-${f.question}`}>
                      <div className="tool-head">
                        <span className="badge badge-bad">{f.status}</span>
                        <span className="mono">{f.created_at}</span>
                        <span className="spacer" />
                        <span className="meta">{f.elapsed_ms} ms</span>
                      </div>
                      <div className="tool-desc">{f.question}</div>
                      <div className="tool-meta">
                        <span className="meta">
                          {f.planner} · {f.runtime || '—'}
                          {f.error_code ? ` · ${f.error_code}` : ''}
                        </span>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </div>
      )}

      {/* ------------------------------------------------------------ 参数 */}
      {tab === 'settings' && (
        <>
          <div className="admin-section">
            <div className="admin-title">智能体循环参数</div>
            <div className="admin-hint">
              改完**立即生效**，不需要重启。调大步数更执着但更慢更贵。
            </div>
            <div className="form-row">
              <label>max_steps
                <input type="number" min={1} max={50} value={loop.max_steps}
                       onChange={(e) => setLoop({ ...loop, max_steps: Number(e.target.value) })} />
                <span className="meta">单轮最多几步工具调用</span>
              </label>
            </div>
            <div className="form-row">
              <label>max_repeat
                <input type="number" min={1} max={20} value={loop.max_repeat}
                       onChange={(e) => setLoop({ ...loop, max_repeat: Number(e.target.value) })} />
                <span className="meta">同一调用重复几次算打转</span>
              </label>
            </div>
            <div className="form-row">
              <label>temperature
                <input type="number" min={0} max={2} step={0.1} value={loop.temperature}
                       onChange={(e) => setLoop({ ...loop, temperature: Number(e.target.value) })} />
                <span className="meta">越高越发散；规划类任务建议 0</span>
              </label>
            </div>
            <button className="btn btn-primary" disabled={busy} onClick={saveLoop}>
              保存并生效
            </button>
          </div>

          <div className="admin-section">
            <div className="admin-title">
              配置文件
              <span className="spacer" />
              <span className="meta mono">{cfgPath || '（未设置）'}</span>
            </div>
            <div className="admin-hint">
              可直接编辑。保存前会先校验，**非法内容会被拒绝且原文件不受影响**。
              这个文件可以进 git——你的配置变更应该可追溯。
            </div>
            <textarea className="config-editor mono" value={cfgText}
                      spellCheck={false} rows={16}
                      onChange={(e) => setCfgText(e.target.value)} />
            <button className="btn btn-primary" disabled={busy} onClick={saveConfig}>
              校验并保存
            </button>
          </div>
        </>
      )}
    </div>
  )
}
