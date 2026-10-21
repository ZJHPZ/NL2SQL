/** SQL 面板：可编辑、可重跑。
 *
 * 这是面向开发者的**刚需**，不是可选功能：
 *   生成的 SQL 几乎总有一点要改（换个时间范围、加个过滤），
 *   若不能就地改再跑，用户只能复制到别的客户端——
 *   那就等于这个工具只做了一半。
 *
 * 关键：重跑走 `/api/execute`，它**仍然经过 guard**
 *   （只读 / 禁多语句 / 强制 LIMIT）。
 *   编辑框不能成为绕过安全边界的旁路。
 */
import { useEffect, useState } from 'react'

export function SqlPanel({
  sql,
  onRerun,
  busy,
}: {
  sql: string | null
  onRerun: (sql: string) => void
  busy: boolean
}) {
  const [text, setText] = useState(sql ?? '')
  const [copied, setCopied] = useState(false)
  const [dirty, setDirty] = useState(false)

  // 新结果到达时同步编辑器内容；用户改过（dirty）就不覆盖，
  // 否则正在编辑的内容会被一次新查询冲掉。
  useEffect(() => {
    if (!dirty) setText(sql ?? '')
  }, [sql, dirty])

  if (!sql) return null

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1200)
    } catch {
      /* 剪贴板不可用（非安全上下文）时静默忽略 */
    }
  }

  const reset = () => {
    setText(sql)
    setDirty(false)
  }

  return (
    <div className="sqlpanel">
      <div className="sqlpanel-head">
        <span className="sqlpanel-title">SQL</span>
        {dirty && <span className="badge badge-warn">已修改</span>}
        <span className="spacer" />
        {dirty && (
          <button className="btn btn-ghost" onClick={reset}>
            还原
          </button>
        )}
        <button className="btn btn-ghost" onClick={copy}>
          {copied ? '已复制' : '复制'}
        </button>
        <button
          className="btn"
          disabled={busy}
          onClick={() => onRerun(text)}
          title="执行编辑框中的 SQL（Ctrl/Cmd + Enter）"
        >
          {busy ? '执行中…' : '重跑'}
        </button>
      </div>
      <textarea
        className="sqlpanel-editor mono"
        value={text}
        spellCheck={false}
        rows={Math.min(18, Math.max(3, text.split('\n').length))}
        onChange={(e) => {
          setText(e.target.value)
          setDirty(true)
        }}
        onKeyDown={(e) => {
          if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
            e.preventDefault()
            onRerun(text)
          }
        }}
      />
      <div className="sqlpanel-foot">
        手写 SQL 仍受只读与行数上限保护 · Ctrl/Cmd + Enter 执行
      </div>
    </div>
  )
}
