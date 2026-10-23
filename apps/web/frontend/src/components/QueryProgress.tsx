/** 流式查询的进度反馈与骨架屏。
 *
 * 背景：agent 一次问答 2~10 秒，其中大部分时间花在模型调用上。
 * 之前界面只有一行「正在查询…」——用户不知道卡住了还是在干活。
 * 这里把 SSE 事件翻译成「一步一行」的时间线：看到"第 2 步：调用工具"
 * 比看到转圈更让人相信系统在正常工作。
 */
import { Check, LoaderCircle, TriangleAlert } from 'lucide-react'

export type StageState = 'active' | 'done' | 'warn' | 'error'

export interface StageStep {
  key: string
  label: string
  detail?: string
  state: StageState
}

function StageIcon({ state }: { state: StageState }) {
  if (state === 'done') {
    return <Check className="stage-icon stage-icon-done" size={14} aria-hidden="true" />
  }
  if (state === 'warn') {
    return <TriangleAlert className="stage-icon stage-icon-warn" size={14} aria-hidden="true" />
  }
  if (state === 'error') {
    return <TriangleAlert className="stage-icon stage-icon-error" size={14} aria-hidden="true" />
  }
  return <LoaderCircle className="stage-icon stage-icon-active spin" size={14} aria-hidden="true" />
}

export function StreamTimeline({
  steps,
  elapsedMs,
  compact = false,
}: {
  steps: StageStep[]
  elapsedMs: number
  compact?: boolean
}) {
  if (steps.length === 0) return null
  return (
    <section
      className={compact ? 'stream-timeline compact' : 'stream-timeline'}
      aria-label="查询执行进度"
      aria-live="polite"
    >
      {!compact && (
        <div className="stream-head">
          <span className="stream-title">执行进度</span>
          <span className="spacer" />
          <span className="meta mono">{(elapsedMs / 1000).toFixed(1)}s</span>
        </div>
      )}
      <ol className="stage-list">
        {steps.map((s) => (
          <li key={s.key} className={`stage stage-${s.state}`}>
            <StageIcon state={s.state} />
            <span className="stage-label">{s.label}</span>
            {s.detail && <span className="stage-detail mono">{s.detail}</span>}
          </li>
        ))}
      </ol>
    </section>
  )
}

/** 首轮查询的骨架屏：给出布局轮廓，避免主区只剩一行灰字。 */
export function QuerySkeleton() {
  return (
    <div className="skeleton-card" role="status" aria-live="polite">
      <span className="visually-hidden">正在查询，等待模型返回…</span>
      <div className="skeleton-line skeleton-w60" />
      <div className="skeleton-line skeleton-w90" />
      <div className="skeleton-line skeleton-w75" />
      <div className="skeleton-table" aria-hidden="true">
        {[0, 1, 2, 3].map((r) => (
          <div className="skeleton-row" key={r}>
            {[0, 1, 2, 3].map((c) => (
              <span className="skeleton-cell" key={c} />
            ))}
          </div>
        ))}
      </div>
    </div>
  )
}
