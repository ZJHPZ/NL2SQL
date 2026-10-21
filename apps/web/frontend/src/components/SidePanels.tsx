/** 历史侧栏与知识库面板。
 *
 * 历史的价值：个人用户的真实行为是「同一类问题反复问」，
 * 所以"一键恢复上次的问题+SQL"比"重新打一遍"重要得多。
 *
 * 知识库面板的重点是**测试检索**：输入一个问题，看到实际命中了哪几条、
 * 为什么命中（`why` 字段）。没有它，上传的知识是否生效只能靠猜。
 */
import { useState } from 'react'
import type { HistoryItem, KnowledgeHit, KnowledgeItem } from '../types'

export function HistoryPanel({
  items,
  onRestore,
}: {
  items: HistoryItem[]
  onRestore: (q: HistoryItem) => void
}) {
  if (items.length === 0) {
    return <div className="side-empty">还没有查询记录</div>
  }
  return (
    <ul className="history-list">
      {items.map((h) => (
        <li key={h.id}>
          <button
            className="history-item"
            onClick={() => onRestore(h)}
            title={h.sql || h.error_message || ''}
          >
            <div className="history-q">{h.question}</div>
            <div className="history-meta">
              <span className={`dot dot-${h.status === 'ok' ? 'ok' : 'bad'}`} />
              <span className="meta">
                {h.row_count} 行 · {h.elapsed_ms}ms
              </span>
              {h.meta && (h.meta as Record<string, unknown>).cached ? (
                <span className="meta">缓存</span>
              ) : null}
            </div>
          </button>
        </li>
      ))}
    </ul>
  )
}

export function KnowledgePanel({
  items,
  hits,
  onTest,
  onToggle,
  busy,
}: {
  items: KnowledgeItem[]
  hits: KnowledgeHit[] | null
  onTest: (text: string) => void
  onToggle: (key: string, enabled: boolean) => void
  busy: boolean
}) {
  const [probe, setProbe] = useState('')

  return (
    <div className="kb">
      <div className="kb-section">
        <div className="kb-title">测试检索</div>
        <div className="kb-hint">
          输入一个问题，看知识库实际命中了什么、为什么命中。
          这是确认「上传的知识有没有生效」的唯一可靠方式。
        </div>
        <div className="kb-test">
          <input
            value={probe}
            placeholder="例如：fact_order 有哪些字段"
            onChange={(e) => setProbe(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && probe.trim()) onTest(probe.trim())
            }}
          />
          <button
            className="btn"
            disabled={busy || !probe.trim()}
            onClick={() => onTest(probe.trim())}
          >
            检索
          </button>
        </div>
        {hits !== null && (
          <div className="kb-hits">
            {hits.length === 0 ? (
              <div className="side-empty">
                没有命中任何条目——说明该问题检索不到已有知识
              </div>
            ) : (
              hits.map((h) => (
                <div className="kb-hit" key={h.key}>
                  <div className="kb-hit-head">
                    <span className={`tag tag-${h.type}`}>{h.type}</span>
                    <span className="kb-hit-title">{h.title || h.key}</span>
                    <span className="spacer" />
                    <span className="mono meta" title="命中来源与名次">
                      {h.why}
                    </span>
                  </div>
                  <div className="kb-hit-body">{h.body.slice(0, 160)}</div>
                </div>
              ))
            )}
          </div>
        )}
      </div>

      <div className="kb-section">
        <div className="kb-title">
          知识条目 <span className="meta">{items.length}</span>
        </div>
        <div className="kb-hint">
          停用某条后它立即退出检索与示例召回——怀疑某条污染结果时用它，
          不必删除（历史保留）。
        </div>
        <ul className="kb-list">
          {items.map((e) => (
            <li key={e.key} className={e.enabled ? '' : 'disabled'}>
              <span className={`tag tag-${e.type}`}>{e.type}</span>
              <span className="kb-item-title" title={e.body}>
                {e.title || e.key}
              </span>
              <span className="spacer" />
              <span className="meta mono" title="版本号">
                v{e.version}
              </span>
              <button
                className="btn btn-tiny"
                onClick={() => onToggle(e.key, !e.enabled)}
                title={e.enabled ? '停用' : '启用'}
              >
                {e.enabled ? '停用' : '启用'}
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
