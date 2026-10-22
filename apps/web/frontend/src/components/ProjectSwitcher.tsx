/** 顶栏项目切换器（M7）。
 *
 * 项目决定「以后端 API 看到的数据源和任务」：每个请求都会带 X-Project-Id。
 * 这里只负责选择，不负责加载项目详情——详情在项目面板里按需拉取。
 *
 * 容错：项目列表拉取失败（例如后端还没实现 /api/projects）时不能让顶栏白屏，
 * 问答页必须照常可用，因此错误只以一行小字 + tooltip 展示。
 */
import type { ProjectSummary } from '../types'

export function ProjectSwitcher({
  projectId,
  projects,
  defaultProjectId,
  loading,
  error,
  onChange,
  onReload,
}: {
  projectId: string
  projects: ProjectSummary[]
  defaultProjectId: string
  loading: boolean
  error: string | null
  onChange: (id: string) => void
  onReload: () => void
}) {
  const known = projects.some((p) => p.id === projectId)
  return (
    <div className="project-switcher" title={error ?? '切换项目：项目隔离数据源与 SQL 任务'}>
      <span className="meta">项目</span>
      {projects.length > 0 ? (
        <select
          className="project-select"
          value={projectId}
          disabled={loading}
          onChange={(e) => onChange(e.target.value)}
        >
          {!known && (
            <option value={projectId}>
              {projectId}（不在列表中）
            </option>
          )}
          {projects.map((p) => (
            <option value={p.id} key={p.id}>
              {p.name}
              {p.is_default ? '（默认）' : ''}
            </option>
          ))}
        </select>
      ) : (
        <select className="project-select" value={projectId} disabled>
          <option value={projectId}>{projectId}</option>
        </select>
      )}
      <button
        className="btn btn-ghost btn-tiny"
        disabled={loading}
        title="刷新项目列表"
        onClick={onReload}
      >
        {loading ? '…' : '刷新'}
      </button>
      {error && (
        <span className="warn-text project-switcher-error" title={error}>
          项目列表加载失败
        </span>
      )}
      {!error && projects.length === 0 && !loading && defaultProjectId && (
        <span className="meta">（没有可切换的项目）</span>
      )}
    </div>
  )
}
