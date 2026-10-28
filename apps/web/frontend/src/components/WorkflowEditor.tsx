/**
 * 工作流步骤编辑器（P1 原生调度编排）。
 *
 * 工作流的正文是一段 JSON：{"steps":[{"key","task_id","depends_on":[...]}]}。
 * 这里不用文本框直接写 JSON，而是给步骤列表：选任务、点依赖。
 * 保存仍然走任务正文（服务端会再做一遍校验：循环依赖、引用必须在项目内且是只读 SQL）。
 *
 * 设计边界：
 * - 只列出本项目可引用的只读 SQL 任务（write / pyspark / workflow 不能进工作流）。
 * - 依赖只表达先后，执行顺序由后端拓扑排序；同一层按这里的书写顺序。
 * - 本地只做「提示」，不阻断保存：循环依赖最终以服务端 400 为准（服务端是唯一裁判）。
 */
import { useMemo } from 'react'
import type { TaskSummary, WorkflowStep } from '../types'

interface WorkflowEditorProps {
  value: string
  onChange: (next: string) => void
  /** 本项目可引用的候选任务（由父组件传入全部任务，这里再筛只读 SQL） */
  candidates: TaskSummary[]
  /** 当前工作流任务 ID：不能引用自己 */
  currentTaskId: string
  disabled?: boolean
}

interface ParsedWorkflow {
  steps: WorkflowStep[]
  invalid: string
}

function parse(value: string): ParsedWorkflow {
  try {
    const data = JSON.parse(value || '{}') as { steps?: unknown }
    if (!data || !Array.isArray(data.steps)) {
      return { steps: [], invalid: '正文不是 { "steps": [...] } 结构' }
    }
    const steps: WorkflowStep[] = []
    for (const raw of data.steps) {
      if (!raw || typeof raw !== 'object') continue
      const item = raw as Record<string, unknown>
      const key = typeof item.key === 'string' ? item.key.trim() : ''
      const taskId = typeof item.task_id === 'string' ? item.task_id.trim() : ''
      const depends = Array.isArray(item.depends_on)
        ? item.depends_on
            .filter((d): d is string => typeof d === 'string')
            .map((d) => d.trim())
            .filter(Boolean)
        : []
      steps.push({ key, task_id: taskId, depends_on: depends })
    }
    return { steps, invalid: '' }
  } catch {
    return { steps: [], invalid: '正文不是合法 JSON' }
  }
}

function serialize(steps: WorkflowStep[]): string {
  return JSON.stringify({ steps }, null, 2)
}

/** 找环（只用于提示；服务端会再判定一次） */
function findCycle(steps: WorkflowStep[]): string | null {
  const keys = new Set(steps.map((s) => s.key).filter(Boolean))
  const state = new Map<string, 0 | 1 | 2>()
  const byKey = new Map(steps.filter((s) => s.key).map((s) => [s.key, s]))
  const visit = (key: string): string | null => {
    const mark = state.get(key) ?? 0
    if (mark === 1) return key
    if (mark === 2) return null
    state.set(key, 1)
    const step = byKey.get(key)
    if (step) {
      for (const dep of step.depends_on) {
        if (!keys.has(dep)) continue
        const hit = visit(dep)
        if (hit) return hit
      }
    }
    state.set(key, 2)
    return null
  }
  for (const step of steps) {
    if (!step.key) continue
    const hit = visit(step.key)
    if (hit) return hit
  }
  return null
}

function nextKey(steps: WorkflowStep[]): string {
  const used = new Set(steps.map((s) => s.key))
  let index = steps.length + 1
  while (used.has(`s${index}`)) index += 1
  return `s${index}`
}

export default function WorkflowEditor({
  value,
  onChange,
  candidates,
  currentTaskId,
  disabled = false,
}: WorkflowEditorProps) {
  const parsed = useMemo(() => parse(value), [value])
  const steps = parsed.steps
  const cycle = useMemo(() => findCycle(steps), [steps])
  const options = useMemo(
    () =>
      (candidates ?? []).filter(
        (t) => t.kind === 'sql' && t.task_id !== currentTaskId,
      ),
    [candidates, currentTaskId],
  )

  const emit = (next: WorkflowStep[]) => onChange(serialize(next))

  const addStep = () => {
    if (disabled) return
    const key = nextKey(steps)
    emit([...steps, { key, task_id: '', depends_on: [] }])
  }

  const removeStep = (index: number) => {
    if (disabled) return
    const removed = steps[index]
    const next = steps
      .filter((_, i) => i !== index)
      .map((step) => ({
        ...step,
        depends_on: step.depends_on.filter((d) => d !== removed.key),
      }))
    emit(next)
  }

  const setTask = (index: number, taskId: string) => {
    if (disabled) return
    emit(steps.map((step, i) => (i === index ? { ...step, task_id: taskId } : step)))
  }

  const toggleDep = (index: number, depKey: string) => {
    if (disabled) return
    emit(
      steps.map((step, i) => {
        if (i !== index) return step
        const has = step.depends_on.includes(depKey)
        return {
          ...step,
          depends_on: has
            ? step.depends_on.filter((d) => d !== depKey)
            : [...step.depends_on, depKey],
        }
      }),
    )
  }

  return (
    <div className="wf-editor">
      {parsed.invalid && (
        <div className="alert alert-bad mono">正文无法解析：{parsed.invalid}</div>
      )}
      {cycle && (
        <div className="alert alert-warn mono">
          检测到循环依赖（{cycle}），保存会被服务端拒绝
        </div>
      )}
      {options.length === 0 && (
        <p className="wf-empty meta">
          本项目还没有可引用的只读 SQL 任务。先在左侧文件夹里右键新建一个 SQL
          任务，再回来把它加进步骤。
        </p>
      )}

      {steps.length === 0 ? (
        <div className="wf-empty wf-empty-first">
          <p className="meta">
            工作流由若干「步骤」组成：每个步骤引用一个只读 SQL 任务，再勾选它依赖的前置步骤。
            没有依赖的步骤是起点；同一工作流一次只会跑一个实例，失败即停。
          </p>
          <button
            type="button"
            className="btn btn-ghost btn-tiny"
            onClick={addStep}
            disabled={disabled || options.length === 0}
          >
            ＋ 添加步骤
          </button>
        </div>
      ) : (
        <ol className="wf-steps">
          {steps.map((step, index) => {
            const missing = !step.task_id
            const stale =
              step.task_id !== '' &&
              !options.some((t) => t.task_id === step.task_id)
            return (
              <li className="wf-step" key={`${step.key}-${index}`}>
                <div className="wf-step-head">
                  <span className="wf-step-key mono" title="步骤标识，用于依赖引用">
                    {step.key || `#${index + 1}`}
                  </span>
                  <select
                    className="wf-step-task"
                    value={step.task_id}
                    disabled={disabled}
                    aria-label={`第 ${index + 1} 步引用的任务`}
                    onChange={(e) => setTask(index, e.target.value)}
                  >
                    <option value="">选择只读 SQL 任务…</option>
                    {options.map((task) => (
                      <option key={task.task_id} value={task.task_id}>
                        {task.folder ? `${task.folder} / ` : ''}
                        {task.name}
                      </option>
                    ))}
                    {stale && (
                      <option value={step.task_id}>
                        （任务已不在本项目：{step.task_id}）
                      </option>
                    )}
                  </select>
                  {missing && <span className="wf-step-tag mono">未选任务</span>}
                  {stale && <span className="wf-step-tag mono">已失效</span>}
                  <button
                    type="button"
                    className="wf-step-remove linkbtn mono"
                    onClick={() => removeStep(index)}
                    disabled={disabled}
                    aria-label={`移除第 ${index + 1} 步`}
                  >
                    移除
                  </button>
                </div>
                <div className="wf-step-deps">
                  <span className="wf-step-deps-label mono">依赖</span>
                  {steps.filter((other) => other.key && other.key !== step.key)
                    .length === 0 ? (
                    <span className="meta">无（作为起始步骤）</span>
                  ) : (
                    <span className="wf-deps">
                      {steps
                        .filter((other) => other.key && other.key !== step.key)
                        .map((other) => {
                          const on = step.depends_on.includes(other.key)
                          return (
                            <button
                              type="button"
                              key={other.key}
                              className={`wf-dep-chip mono${on ? ' on' : ''}`}
                              aria-pressed={on}
                              disabled={disabled}
                              onClick={() => toggleDep(index, other.key)}
                            >
                              {other.key}
                            </button>
                          )
                        })}
                    </span>
                  )}
                </div>
              </li>
            )
          })}
        </ol>
      )}

      {steps.length > 0 && (
        <div className="wf-actions">
          <button
            type="button"
            className="btn btn-ghost btn-tiny"
            onClick={addStep}
            disabled={disabled || steps.length >= 20}
          >
            ＋ 添加步骤
          </button>
          <span className="meta">
            {steps.length >= 20 ? '已达 20 步上限' : `${steps.length} 个步骤`}
            ；保存后可在底部「运行实例」里手动触发，或在「调度」里配置 cron
          </span>
        </div>
      )}
    </div>
  )
}
