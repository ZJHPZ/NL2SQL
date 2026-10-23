/** 结果表：客户端排序 + 过滤 + 导出。
 *
 * 为什么排序放在客户端而不是重新查库：
 *   结果已经在浏览器里了，重查一遍要等 1 秒以上（模型/数据库往返），
 *   而个人场景的数据量（上限 1000 行）完全能客户端排。
 *   "点表头立刻排序"是开发者高频动作，延迟必须接近零。
 *
 * 排序的两个细节：
 *   1. 数值列按数值排（后端刻意把数值保持为 number 类型，见 serializers.py）
 *   2. 排序稳定：相等时保留原顺序，避免同一份数据两次点击顺序不同
 */
import { useMemo, useState } from 'react'
import type { ResultPayload } from '../types'

type SortState = { col: number; desc: boolean } | null

function isNumericColumn(rows: unknown[][], col: number): boolean {
  let numeric = 0
  let total = 0
  for (const r of rows.slice(0, 50)) {
    const v = r[col]
    if (v === null || v === undefined) continue
    total++
    if (typeof v === 'number') numeric++
  }
  return total > 0 && numeric / total > 0.8
}

function cmp(a: unknown, b: unknown): number {
  if (a === null || a === undefined) return b === null || b === undefined ? 0 : 1
  if (b === null || b === undefined) return -1
  if (typeof a === 'number' && typeof b === 'number') return a - b
  return String(a).localeCompare(String(b), 'zh-CN', { numeric: true })
}

function toCsv(columns: string[], rows: unknown[][]): string {
  const esc = (v: unknown) => {
    if (v === null || v === undefined) return ''
    const s = String(v)
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }
  const lines = [columns.map(esc).join(',')]
  for (const r of rows) lines.push(r.map(esc).join(','))
  // 加 BOM，否则 Excel 打开中文会乱码
  return '\ufeff' + lines.join('\n')
}

export function ResultTable({ result }: { result: ResultPayload }) {
  const [sort, setSort] = useState<SortState>(null)
  const [filter, setFilter] = useState('')

  const numericCols = useMemo(
    () => result.columns.map((_, i) => isNumericColumn(result.rows, i)),
    [result],
  )

  const view = useMemo(() => {
    let rows = result.rows
    const q = filter.trim().toLowerCase()
    if (q) {
      rows = rows.filter((r) =>
        r.some((v) => String(v ?? '').toLowerCase().includes(q)),
      )
    }
    if (sort) {
      const { col, desc } = sort
      rows = [...rows].sort((a, b) => {
        const c = cmp(a[col], b[col])
        return desc ? -c : c
      })
    }
    return rows
  }, [result, filter, sort])

  const toggleSort = (col: number) => {
    setSort((s) =>
      s && s.col === col ? { col, desc: !s.desc } : { col, desc: true },
    )
  }

  const download = () => {
    const blob = new Blob([toCsv(result.columns, view)], {
      type: 'text/csv;charset=utf-8',
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `result_${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '')}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  if (result.row_count === 0) {
    return (
      <div className="empty-block">
        <div className="empty-title">查询执行成功，但没有匹配的数据</div>
        <div className="empty-hint">
          这不是错误——是过滤条件筛掉了所有行。可放宽条件或检查时间范围。
        </div>
      </div>
    )
  }

  return (
    <div className="result">
      <div className="result-toolbar">
        <input
          className="filter-input"
          placeholder="在结果中过滤…"
          aria-label="在结果中过滤"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <span className="meta">
          显示 {view.length} / {result.row_count} 行
          {filter && view.length !== result.row_count ? '（已过滤）' : ''}
        </span>
        {result.truncated && (
          <span className="badge badge-warn" title="结果被安全上限截断">
            仅前 {result.row_count} 行
          </span>
        )}
        <span className="spacer" />
        <button className="btn btn-ghost" onClick={download}>
          导出 CSV
        </button>
      </div>

      <div className="table-scroll">
        <table className="grid">
          <thead>
            <tr>
              <th className="rownum">#</th>
              {result.columns.map((c, i) => (
                <th
                  key={i}
                  scope="col"
                  className={numericCols[i] ? 'num sortable' : 'sortable'}
                  onClick={() => toggleSort(i)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault()
                      toggleSort(i)
                    }
                  }}
                  tabIndex={0}
                  aria-sort={
                    sort?.col === i
                      ? sort.desc
                        ? 'descending'
                        : 'ascending'
                      : 'none'
                  }
                  title="点击或按回车/空格排序"
                >
                  {c}
                  {sort?.col === i && (
                    <span className="sortmark">{sort.desc ? '▾' : '▴'}</span>
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {view.map((r, ri) => (
              <tr key={ri}>
                <td className="rownum">{ri + 1}</td>
                {r.map((v, ci) => (
                  <td
                    key={ci}
                    className={numericCols[ci] ? 'num mono' : 'mono'}
                    title={v === null ? 'NULL' : String(v)}
                  >
                    {v === null || v === undefined ? (
                      <span className="null">NULL</span>
                    ) : (
                      String(v)
                    )}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
