import { useMemo } from 'react'
import type { DataWriteSettings, SourceOut, SyncConfig } from '../types'

/**
 * 数据同步任务的配置编辑器（P1）。
 *
 * 任务正文存 JSON：{ source_id, table, target_table?, mode, watermark? }。
 * 这里只编辑配置，不执行；预演与提交在底部「同步记录」里确认。
 */
interface Props {
  value: string
  onChange: (next: string) => void
  sources: SourceOut[]
  dataWrite?: DataWriteSettings
  disabled?: boolean
}

const DEFAULT_CONFIG: SyncConfig = {
  source_id: '',
  table: '',
  target_table: '',
  mode: 'full',
  watermark: '',
}

function normalize(raw: unknown): SyncConfig {
  if (!raw || typeof raw !== 'object') return { ...DEFAULT_CONFIG }
  const obj = raw as Record<string, unknown>
  const mode = obj.mode === 'incremental' ? 'incremental' : 'full'
  return {
    source_id: typeof obj.source_id === 'string' ? obj.source_id : '',
    table: typeof obj.table === 'string' ? obj.table : '',
    target_table: typeof obj.target_table === 'string' ? obj.target_table : '',
    mode,
    watermark: typeof obj.watermark === 'string' ? obj.watermark : '',
  }
}

export function parseSyncValue(raw: string): {
  config: SyncConfig
  error: string | null
} {
  const text = raw.trim()
  if (!text) return { config: { ...DEFAULT_CONFIG }, error: null }
  try {
    const parsed = JSON.parse(text) as unknown
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return { config: { ...DEFAULT_CONFIG }, error: '同步配置必须是一个 JSON 对象。' }
    }
    const config = normalize(parsed)
    if (config.mode === 'incremental' && !config.watermark) {
      return {
        config,
        error: '增量模式需要填写水位列（每次只搬 watermark 大于上次水位的行）。',
      }
    }
    return { config, error: null }
  } catch (err) {
    return {
      config: { ...DEFAULT_CONFIG },
      error: `同步配置不是合法 JSON：${err instanceof Error ? err.message : String(err)}`,
    }
  }
}

function serialize(config: SyncConfig): string {
  const next: SyncConfig = {
    source_id: config.source_id,
    table: config.table,
    mode: config.mode,
  }
  const target = (config.target_table ?? '').trim()
  if (target && target !== config.table) next.target_table = target
  if (config.mode === 'incremental' && config.watermark) {
    next.watermark = config.watermark
  }
  return JSON.stringify(next, null, 2)
}

export default function SyncEditor({
  value,
  onChange,
  sources,
  dataWrite,
  disabled,
}: Props) {
  const { config, error } = useMemo(() => parseSyncValue(value), [value])
  const target = sources.find((s) => s.source_id === dataWrite?.source_id)
  const source = sources.find((s) => s.source_id === config.source_id)
  const update = (patch: Partial<SyncConfig>) =>
    onChange(serialize({ ...config, ...patch }))

  return (
    <div className="sync-editor">
      <div className="meta sync-hint">
        同步任务 = 读源库一张表 → 写到项目写目标（SQLite 测试库或 MySQL 实验库）。
        全量会先清空目标表再全搬，
        增量按水位列只搬新行；保存后点「预演同步」看行数与样本，确认后提交。
      </div>
      {error && <div className="alert alert-bad sync-error">{error}</div>}
      <div className="sync-grid">
        <label className="field">
          <span>源数据源</span>
          <select
            value={config.source_id}
            disabled={disabled}
            onChange={(e) => update({ source_id: e.target.value })}
          >
            <option value="">选择源数据源</option>
            {sources.map((s) => (
              <option key={s.source_id} value={s.source_id}>
                {s.label || s.source_id} · {s.adapter}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>源表</span>
          <input
            className="mono"
            value={config.table}
            placeholder="例如 probe_item"
            disabled={disabled}
            onChange={(e) => update({ table: e.target.value.trim() })}
          />
        </label>
        <label className="field">
          <span>目标表（写到项目写目标库）</span>
          <input
            className="mono"
            value={config.target_table ?? ''}
            placeholder="默认与源表同名"
            disabled={disabled}
            onChange={(e) => update({ target_table: e.target.value.trim() })}
          />
        </label>
        <label className="field">
          <span>同步模式</span>
          <select
            value={config.mode}
            disabled={disabled}
            onChange={(e) =>
              update({ mode: e.target.value as SyncConfig['mode'] })
            }
          >
            <option value="full">全量（清空目标表后全搬）</option>
            <option value="incremental">增量（按水位列只搬新行）</option>
          </select>
        </label>
        {config.mode === 'incremental' && (
          <label className="field">
            <span>水位列</span>
            <input
              className="mono"
              value={config.watermark ?? ''}
              placeholder="例如 id 或 updated_at"
              disabled={disabled}
              onChange={(e) => update({ watermark: e.target.value.trim() })}
            />
          </label>
        )}
      </div>

      {!dataWrite?.enabled && (
        <div className="alert alert-warn sync-warn">
          项目还没有开启受控写：同步提交会被拒绝。请先在项目详情的「受控写」里开启，
          并把写目标绑定到本项目的一个 SQLite 测试库或 MySQL 实验库。
        </div>
      )}
      {dataWrite?.enabled && (
        <p className="meta sync-target">
          写目标：{target ? `${target.label || target.source_id}（${target.adapter}）` : dataWrite.source_id}
          {' · '}单次上限 {dataWrite.max_affected || 100} 行 · 只允许写绑定库，问答链路永远只读
        </p>
      )}
      {source && source.adapter !== 'sqlite' && (
        <p className="meta sync-target">
          源是 {source.adapter}：读取走只读适配器，写入只用项目绑定的写目标
          （SQLite 测试库或 MySQL 实验库）。
        </p>
      )}

      <details className="sync-json">
        <summary className="mono">查看配置 JSON</summary>
        <pre className="mono">{serialize(config)}</pre>
      </details>
    </div>
  )
}
