/** 状态条：把后端的"可操作性"字段摆在最显眼的位置。
 *
 * 设计意图：在"只给 SQL + 结果表"的方案里，用户唯一能判断
 * "这个答案能不能信"的依据就是这里。所以它必须在结果之上、不被折叠。
 *
 * 五类信息按优先级排列：
 *   1. 状态与来源（ok / 空 / 截断 / 无解 / 被拦）+ 规划器 + 缓存
 *   2. 口径未验证警告（最重要，绝不能静默）
 *   3. 可操作建议
 *   4. 生效的过滤条件（空结果时回显）
 *   5. 候选字段（列名不存在时）
 */
import type { QueryResponse } from '../types'

const STATUS_LABEL: Record<string, string> = {
  ok: '成功',
  empty: '无数据',
  truncated: '已截断',
  unresolved: '未能理解',
  invalid_sql: 'SQL 无效',
  blocked: '已拦截',
  timeout: '超时',
  error: '失败',
  need_clarification: '需要你补充信息',
}

function toneOf(status: string): string {
  if (status === 'ok') return 'ok'
  // 追问用 warn 而不是 bad：它是**正常交互**，不是错误。
  // 用红色会让用户以为出问题了。
  if (status === 'empty' || status === 'truncated'
      || status === 'need_clarification') return 'warn'
  return 'bad'
}

const PLANNER_LABEL: Record<string, string> = {
  rule: '规则规划器（无模型）',
  llm: '模型规划器',
  hybrid: '混合（规则优先）',
  hybrid_verified: '混合（含验证）',
  agent: '智能体（工具循环）',
  manual: '手写 SQL',
  'rule(hybrid 降级)': '规则规划器（模型不可用，已降级）',
  'rule(agent 降级：无模型)': '规则规划器（无模型，智能体不可用）',
}

export function StatusBar({ data, onPickOption }: {
  data: QueryResponse
  onPickOption?: (text: string) => void
}) {
  const tone = toneOf(data.status)
  // 口径未验证的提示单独摘出来强调：它决定用户该不该信这个数字
  const unverified = data.notes.filter(
    (n) => n.includes('未验证') || n.includes('未经'),
  )
  const otherNotes = data.notes.filter((n) => !unverified.includes(n))
  const clarifying = data.status === 'need_clarification'

  return (
    <div className={`statusbar tone-${tone}`}>
      <div className="statusbar-main">
        <span className={`badge badge-${tone}`}>
          {STATUS_LABEL[data.status] ?? data.status}
        </span>
        <span className="meta" title="答案由哪个规划器产出">
          {PLANNER_LABEL[data.planner_mode] ?? data.planner_mode}
        </span>
        {data.result && (
          <span className="meta">{data.result.row_count} 行</span>
        )}
        <span className="meta">{data.elapsed_ms} ms</span>
        {data.cached && (
          <span className="meta meta-cached" title="结果来自缓存，非实时查询">
            缓存
          </span>
        )}
        {data.cached && (data.data_time || data.result?.data_time) && (
          <span className="meta">
            数据时间 {(data.data_time ?? data.result?.data_time) as string}
          </span>
        )}
        <span className="spacer" />
        {data.plan_fingerprint && (
          <span className="meta mono" title="逻辑计划指纹（缓存键）">
            {data.plan_fingerprint.slice(0, 10)}
          </span>
        )}
      </div>

      {/* 口径未验证：用醒目样式，不混在普通提示里 */}
      {unverified.length > 0 && (
        <div className="alert alert-warn">
          <strong>口径未经验证</strong>
          {unverified.map((n, i) => (
            <div key={i}>{n}</div>
          ))}
        </div>
      )}

      {data.message && !clarifying && (
        <div className={`alert alert-${tone === 'ok' ? 'info' : tone}`}>
          {data.message}
        </div>
      )}

      {/* 智能体追问：把问题和可选项做成可点的按钮。
          用户不必打字——点一下就是回答，下一轮自动带上会话。 */}
      {clarifying && (
        <div className="alert alert-warn clarify">
          <strong>智能体需要你补充信息</strong>
          <div className="clarify-q">{data.message}</div>
          {data.candidates.length > 0 && (
            <div className="chips">
              {data.candidates.map((c, i) => (
                <button
                  className="chip chip-action"
                  key={i}
                  onClick={() => onPickOption?.(c)}
                  title="点击以此作为回答"
                >
                  {c}
                </button>
              ))}
            </div>
          )}
          <div className="meta">
            也可以直接在下方输入框里回答，或点选项由我代填
          </div>
        </div>
      )}

      {data.suggestions.length > 0 && (
        <div className="alert alert-info">
          <strong>建议</strong>
          <ul className="tight">
            {data.suggestions.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ul>
        </div>
      )}

      {data.applied_filters.length > 0 && (
        <div className="alert alert-info">
          <strong>生效的过滤条件</strong>
          <div className="chips">
            {data.applied_filters.map((f, i) => (
              <span className="chip" key={i}>
                {f}
              </span>
            ))}
          </div>
        </div>
      )}

      {data.candidates.length > 0 && (
        <div className="alert alert-warn">
          <strong>候选字段（点击可复制）</strong>
          <div className="chips">
            {data.candidates.map((c, i) => (
              <button
                className="chip chip-action"
                key={i}
                onClick={() => navigator.clipboard?.writeText(c)}
                title="复制该字段名"
              >
                {c}
              </button>
            ))}
          </div>
        </div>
      )}

      {otherNotes.length > 0 && (
        <details className="notes">
          <summary>其它提示（{otherNotes.length}）</summary>
          <ul className="tight">
            {otherNotes.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        </details>
      )}

      {Object.keys(data.trace).length > 0 && (
        <details className="notes">
          <summary>各阶段耗时</summary>
          <div className="chips">
            {Object.entries(data.trace).map(([k, v]) => (
              <span className="chip mono" key={k}>
                {k.replace(/_ms$/, '')} {v}ms
              </span>
            ))}
          </div>
        </details>
      )}
    </div>
  )
}
