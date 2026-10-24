/** 任务面板（M9）。
 *
 * 任务 = 项目里保存的只读 SQL（或按规划中的 PySpark 文本），
 * 归属到项目内的文件夹树：首次进入自动有 ods / dwd / dwm / ads 四层。
 *
 * 交互按用户要求重做：
 *   1. **右键文件夹**弹菜单：新建任务 / 新建子文件夹 / 重命名 / 删除。
 *      右键不是唯一入口——顶栏与文件夹行都留了可键盘触达的按钮，
 *      否则纯鼠标右键的界面没法用键盘操作。
 *   2. 新建任务是**对话框**：名称、类型（SQL / PySpark）、说明、文件夹。
 *   3. 运行的是**已保存**的 SQL（run 接口不收 SQL 参数）；
 *      SQL 改过没保存时禁止运行，避免"我明明改了怎么还是旧结果"。
 *   4. PySpark 任务可以保存、留版本，但执行器尚未接入——界面如实标注，
 *      不假装它能跑。
 *   5. 每次保存 / agent 修改都会留下版本记录，author="ai" 必须有角标。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import type {
  ResultPayload,
  TaskDetailResponse,
  TaskFolder,
  TaskRunResponse,
  TaskRunResultPayload,
  TaskSummary,
} from '../types'
import { ResultTable } from './ResultTable'

const errText = (r: { error: { error: string; detail: string } }) =>
  `${r.error.error}: ${r.error.detail}`

const KIND_LABEL: Record<string, string> = {
  sql: 'SQL 查询',
  pyspark: 'PySpark',
}

const KIND_HINT: Record<string, string> = {
  sql: '只读 SELECT / WITH，可直接运行',
  pyspark: '可保存与版本管理，执行器尚未接入',
}

/** 任务返回值补成 ResultTable 认识的 ResultPayload。 */
function toResultPayload(r: TaskRunResultPayload): ResultPayload {
  const rows = r.rows ?? []
  return {
    columns: r.columns ?? [],
    rows,
    row_count: r.row_count ?? rows.length,
    truncated: r.truncated ?? false,
    elapsed_ms: r.elapsed_ms ?? 0,
    data_time: null,
    cached: false,
    warnings: [],
  }
}

function authorLabel(author: string): string {
  if (author === 'ai') return 'AI'
  if (author === 'user') return '手动'
  return author || '未知'
}

function joinFolder(parent: string, name: string): string {
  return parent ? `${parent}/${name}` : name
}

function folderName(path: string): string {
  const segs = path.split('/')
  return segs[segs.length - 1] || path
}

// ---------------------------------------------------------------- 文件夹树
interface TreeNode {
  path: string
  name: string
  children: TreeNode[]
  tasks: TaskSummary[]
}

function buildTree(folders: TaskFolder[], tasks: TaskSummary[]): TreeNode[] {
  const map = new Map<string, TreeNode>()
  const roots: TreeNode[] = []

  const ensure = (path: string): TreeNode | null => {
    if (!path) return null
    const hit = map.get(path)
    if (hit) return hit
    const segs = path.split('/')
    const parentPath = segs.slice(0, -1).join('/')
    const node: TreeNode = {
      path,
      name: segs[segs.length - 1],
      children: [],
      tasks: [],
    }
    map.set(path, node)
    const parent = parentPath ? ensure(parentPath) : null
    if (parent) parent.children.push(node)
    else roots.push(node)
    return node
  }

  for (const f of folders) ensure(f.path)
  for (const t of tasks) {
    if (t.folder) ensure(t.folder)?.tasks.push(t)
  }
  return roots
}

/** 搜索时按任务名 / 文件夹名过滤树；文件夹名命中就整棵留下。 */
function filterTree(nodes: TreeNode[], query: string): TreeNode[] {
  const needle = query.toLowerCase()
  return nodes.flatMap((node) => {
    const selfHit = node.path.toLowerCase().includes(needle)
    if (selfHit) return [node]
    const children = filterTree(node.children, query)
    const tasks = node.tasks.filter(
      (t) => t.name.toLowerCase().includes(needle) || t.kind.includes(needle),
    )
    if (children.length || tasks.length) return [{ ...node, children, tasks }]
    return []
  })
}

// ---------------------------------------------------------------- 右键菜单
interface MenuItem {
  label: string
  hint?: string
  danger?: boolean
  onSelect: () => void
}

function ContextMenu({
  x,
  y,
  title,
  items,
  onClose,
}: {
  x: number
  y: number
  title: string
  items: MenuItem[]
  onClose: () => void
}) {
  const ref = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  // 贴边时向内收，避免菜单掉出视口
  const left = Math.min(x, Math.max(8, window.innerWidth - 232))
  const top = Math.min(y, Math.max(8, window.innerHeight - 8 - items.length * 36 - 44))

  return (
    <div
      className="ctx-menu"
      role="menu"
      aria-label={`${title} 操作`}
      ref={ref}
      style={{ left, top }}
    >
      <div className="ctx-title mono">{title}</div>
      {items.map((item) => (
        <button
          key={item.label}
          role="menuitem"
          className={item.danger ? 'ctx-item ctx-danger' : 'ctx-item'}
          onClick={() => {
            onClose()
            item.onSelect()
          }}
        >
          <span>{item.label}</span>
          {item.hint && <span className="ctx-hint mono">{item.hint}</span>}
        </button>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------- 对话框
function Dialog({
  title,
  children,
  onClose,
}: {
  title: string
  children: React.ReactNode
  onClose: () => void
}) {
  const ref = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    ref.current?.querySelector<HTMLElement>('input, select, textarea')?.focus()
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        ref={ref}
      >
        <div className="modal-head">
          <b>{title}</b>
          <span className="spacer" />
          <button className="btn btn-ghost btn-tiny" onClick={onClose}>
            关闭
          </button>
        </div>
        {children}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- 树节点
function TreeFolder({
  node,
  expanded,
  activeFolder,
  selectedTaskId,
  onToggle,
  onSelectFolder,
  onSelectTask,
  onMenu,
}: {
  node: TreeNode
  expanded: Set<string>
  activeFolder: string
  selectedTaskId: string | null
  onToggle: (path: string) => void
  onSelectFolder: (path: string) => void
  onSelectTask: (taskId: string) => void
  onMenu: (x: number, y: number, folder: string) => void
}) {
  const open = expanded.has(node.path)
  const total =
    node.tasks.length + node.children.reduce((acc, c) => acc + c.tasks.length, 0)

  return (
    <li className="tree-node">
      <div
        className={
          activeFolder === node.path ? 'tree-folder-row active' : 'tree-folder-row'
        }
        onContextMenu={(e) => {
          e.preventDefault()
          onSelectFolder(node.path)
          onMenu(e.clientX, e.clientY, node.path)
        }}
      >
        <button
          className="tree-caret"
          aria-expanded={open}
          aria-label={open ? `收起 ${node.name}` : `展开 ${node.name}`}
          onClick={() => onToggle(node.path)}
        >
          <span aria-hidden="true">{open ? '▾' : '▸'}</span>
        </button>
        <button
          className="tree-name"
          onClick={() => {
            onSelectFolder(node.path)
            onToggle(node.path)
          }}
          title={node.path}
        >
          <span className="mono">{node.name}</span>
          <span className="tree-count mono">{total}</span>
        </button>
        <button
          className="folder-menu-btn"
          aria-label={`${node.name} 文件夹操作`}
          onClick={(e) => {
            const r = e.currentTarget.getBoundingClientRect()
            onSelectFolder(node.path)
            onMenu(r.left, r.bottom + 4, node.path)
          }}
        >
          ⋯
        </button>
      </div>
      {open && (
        <ul className="tree-children">
          {node.children.map((child) => (
            <TreeFolder
              key={child.path}
              node={child}
              expanded={expanded}
              activeFolder={activeFolder}
              selectedTaskId={selectedTaskId}
              onToggle={onToggle}
              onSelectFolder={onSelectFolder}
              onSelectTask={onSelectTask}
              onMenu={onMenu}
            />
          ))}
          {node.tasks.map((t) => (
            <li key={t.task_id}>
              <button
                className={
                  selectedTaskId === t.task_id ? 'tree-task active' : 'tree-task'
                }
                onClick={() => onSelectTask(t.task_id)}
                title={t.name}
              >
                <span className={t.kind === 'pyspark' ? 'kind-dot kind-pyspark' : 'kind-dot'} aria-hidden="true" />
                <span className="tree-task-name">{t.name}</span>
                <span className="tree-task-meta mono">
                  {t.kind === 'pyspark' ? 'py' : 'sql'} · v{t.revision}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

// ---------------------------------------------------------------- 主组件
/** 切换 tab / 关 tab / 回概览的去向：有未保存 SQL 时先确认再走。 */
type NavIntent =
  | { kind: 'overview' }
  | { kind: 'task'; id: string }
  | { kind: 'close'; id: string }

export function TasksPanel({
  projectId,
  projectName,
  projectDescription,
  isDefaultProject,
  onOpenOverview,
}: {
  projectId: string
  /** 只用于对话框里显示上下文 */
  projectName?: string
  projectDescription?: string
  /** 默认项目没有任务库：不请求任务接口，直接给引导 */
  isDefaultProject: boolean
  /** 跳到项目概览（数据源 / 语义口径 / 表快照）。 */
  onOpenOverview?: () => void
}) {
  // ------------------------------------------------------------ 列表
  const [folders, setFolders] = useState<TaskFolder[] | null>(null)
  const [folderError, setFolderError] = useState<string | null>(null)
  const [tasks, setTasks] = useState<TaskSummary[] | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [listBusy, setListBusy] = useState(false)

  // ------------------------------------------------------------ IDE 外壳
  /** 打开的任务 tab；概览 tab 用 selectedId === null 表示。 */
  const [tabs, setTabs] = useState<{ id: string; name: string }[]>([])
  const [sideQuery, setSideQuery] = useState('')
  /** 底部面板：null = 收起。 */
  const [bottom, setBottom] = useState<'run' | 'history' | 'info' | null>('run')
  const gutterRef = useRef<HTMLDivElement | null>(null)
  /** 有未保存 SQL 时点了别的 tab：先弹确认，别默默丢改动。 */
  const [pendingNav, setPendingNav] = useState<NavIntent | null>(null)

  // ------------------------------------------------------------ 树状态
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [activeFolder, setActiveFolder] = useState('')
  const [menu, setMenu] = useState<{ x: number; y: number; folder: string } | null>(
    null,
  )

  // ------------------------------------------------------------ 详情
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<TaskDetailResponse | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)

  // 编辑器
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [sql, setSql] = useState('')
  const [sqlDirty, setSqlDirty] = useState(false)
  const [note, setNote] = useState('')
  const [saveBusy, setSaveBusy] = useState(false)
  const [saveMsg, setSaveMsg] = useState<string | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [moveBusy, setMoveBusy] = useState(false)

  // 运行
  const [runBusy, setRunBusy] = useState(false)
  const [runResult, setRunResult] = useState<TaskRunResponse | null>(null)
  const [runError, setRunError] = useState<string | null>(null)

  // 新建任务对话框
  const [newTask, setNewTask] = useState<{ folder: string } | null>(null)
  const [ntName, setNtName] = useState('')
  const [ntKind, setNtKind] = useState<'sql' | 'pyspark'>('sql')
  const [ntDesc, setNtDesc] = useState('')
  const [ntSql, setNtSql] = useState('')
  const [ntBusy, setNtBusy] = useState(false)
  const [ntError, setNtError] = useState<string | null>(null)

  // 新建文件夹对话框
  const [newFolder, setNewFolder] = useState<{ parent: string } | null>(null)
  const [nfName, setNfName] = useState('')
  const [nfBusy, setNfBusy] = useState(false)
  const [nfError, setNfError] = useState<string | null>(null)

  // 重命名文件夹对话框
  const [renameFolder, setRenameFolder] = useState<{ path: string } | null>(null)
  const [rfName, setRfName] = useState('')
  const [rfBusy, setRfBusy] = useState(false)
  const [rfError, setRfError] = useState<string | null>(null)

  // 删除确认
  const [deleteFolder, setDeleteFolder] = useState<{ path: string } | null>(null)
  const [deleteTask, setDeleteTask] = useState<{ taskId: string; name: string } | null>(
    null,
  )
  const [deleteBusy, setDeleteBusy] = useState(false)

  // ------------------------------------------------------------ 加载
  const loadFolders = useCallback(async () => {
    const r = await api.taskFolders(projectId)
    if (!r.ok) {
      setFolderError(errText(r))
      return
    }
    setFolderError(null)
    const list = r.data.folders ?? []
    setFolders(list)
    // 首次进来把四层默认目录展开，让人一眼看到树
    setExpanded((prev) => {
      if (prev.size > 0) return prev
      return new Set(list.filter((f) => !f.path.includes('/')).map((f) => f.path))
    })
  }, [projectId])

  const loadList = useCallback(async () => {
    setListBusy(true)
    const r = await api.listTasks(projectId)
    setListBusy(false)
    if (!r.ok) {
      setListError(errText(r))
      return
    }
    setListError(null)
    setTasks(r.data.tasks ?? [])
  }, [projectId])

  const refreshAll = useCallback(async () => {
    await Promise.all([loadFolders(), loadList()])
  }, [loadFolders, loadList])

  useEffect(() => {
    if (isDefaultProject) return
    setFolders(null)
    setTasks(null)
    setSelectedId(null)
    setDetail(null)
    setRunResult(null)
    setActiveFolder('')
    setTabs([])
    setSideQuery('')
    setBottom(null)
    void refreshAll()
  }, [isDefaultProject, projectId, refreshAll])

  const openTask = useCallback(
    async (taskId: string) => {
      setSelectedId(taskId)
      setDetail(null)
      setDetailLoading(true)
      setDetailError(null)
      setRunResult(null)
      setRunError(null)
      setBottom('run')
      setSaveMsg(null)
      setSaveError(null)
      const r = await api.getTask(projectId, taskId)
      setDetailLoading(false)
      if (!r.ok) {
        setDetailError(errText(r))
        return
      }
      setDetail(r.data)
      setName(r.data.task.name)
      setDescription(r.data.task.description ?? '')
      setSql(r.data.task.sql)
      setSqlDirty(false)
      setNote('')
      setActiveFolder(r.data.task.folder ?? '')
      setTabs((prev) =>
        prev.some((t) => t.id === taskId)
          ? prev.map((t) => (t.id === taskId ? { ...t, name: r.data.task.name } : t))
          : [...prev, { id: taskId, name: r.data.task.name }],
      )
    },
    [projectId],
  )

  /** 关闭 tab：关掉当前 tab 就切到最后一个；没有任务了回概览。 */
  const closeTab = (taskId: string) => {
    setTabs((prev) => {
      const next = prev.filter((t) => t.id !== taskId)
      if (selectedId === taskId) {
        const fallback = next[next.length - 1]
        if (fallback) void openTask(fallback.id)
        else {
          setSelectedId(null)
          setDetail(null)
          setRunResult(null)
        }
      }
      return next
    })
  }

  /** 真正执行切换（已确认或无未保存改动）。 */
  const applyNav = (intent: NavIntent) => {
    setPendingNav(null)
    setSqlDirty(false)
    if (intent.kind === 'overview') {
      setSelectedId(null)
      setDetail(null)
      setDetailError(null)
      setRunResult(null)
      setDetailLoading(false)
    } else if (intent.kind === 'task') {
      if (intent.id !== selectedId) void openTask(intent.id)
    } else {
      closeTab(intent.id)
    }
  }

  /** 切换前拦一道：有未保存 SQL 先问，避免切 tab 丢改动。 */
  const askNav = (intent: NavIntent) => {
    if (intent.kind === 'task' && intent.id === selectedId) return
    if (!sqlDirty) {
      applyNav(intent)
      return
    }
    setPendingNav(intent)
  }

  // 刷新 / 关窗口时也提醒（组件内切换由 askNav 处理）。
  useEffect(() => {
    if (!sqlDirty) return
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [sqlDirty])

  const reloadDetail = useCallback(async () => {
    if (!selectedId) return
    const r = await api.getTask(projectId, selectedId)
    if (!r.ok) {
      setDetailError(errText(r))
      return
    }
    setDetail(r.data)
  }, [projectId, selectedId])

  // ------------------------------------------------------------ 树操作
  const toggleFolder = (path: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }

  const openMenu = (x: number, y: number, folder: string) => {
    setMenu({ x, y, folder })
  }

  const defaultFolder = (): string => {
    if (activeFolder) return activeFolder
    const root = folders?.find((f) => !f.path.includes('/'))
    return root?.path ?? ''
  }

  const openNewTask = (folder: string) => {
    setNtName('')
    setNtKind('sql')
    setNtDesc('')
    setNtSql('')
    setNtError(null)
    setNewTask({ folder })
  }

  const openNewFolder = (parent: string) => {
    setNfName('')
    setNfError(null)
    setNewFolder({ parent })
  }

  const openRenameFolder = (path: string) => {
    setRfName(folderName(path))
    setRfError(null)
    setRenameFolder({ path })
  }

  // ------------------------------------------------------------ 任务动作
  const saveTask = async (): Promise<boolean> => {
    if (!detail) return false
    const tid = detail.task.task_id
    if (!name.trim()) {
      setSaveError('任务名称不能为空')
      return false
    }
    setSaveBusy(true)
    setSaveError(null)
    setSaveMsg(null)
    const payload: {
      name: string
      description: string
      note: string
      sql?: string
    } = {
      name: name.trim(),
      description: description.trim(),
      note: note.trim() || '手动保存',
    }
    if (sqlDirty) payload.sql = sql
    const r = await api.updateTask(projectId, tid, payload)
    setSaveBusy(false)
    if (!r.ok) {
      setSaveError(errText(r))
      return false
    }
    setSaveMsg(`已保存为 v${r.data.revision}`)
    setSqlDirty(false)
    setNote('')
    setTabs((prev) =>
      prev.map((t) => (t.id === tid ? { ...t, name: name.trim() } : t)),
    )
    await reloadDetail()
    await loadList()
    return true
  }

  const runTask = async () => {
    if (!detail) return
    if (detail.task.kind !== 'sql') {
      setRunError('PySpark 任务执行器尚未接入，当前只能保存与版本管理')
      return
    }
    if (sqlDirty) {
      setRunError('SQL 改动还没保存，运行的是已保存的版本')
      return
    }
    setRunBusy(true)
    setRunError(null)
    setRunResult(null)
    const r = await api.runTask(projectId, detail.task.task_id)
    setRunBusy(false)
    if (!r.ok) {
      setRunError(errText(r))
      return
    }
    setRunResult(r.data)
    setBottom('run')
    await loadList()
  }

  const submitDeleteTask = async () => {
    if (!deleteTask) return
    setDeleteBusy(true)
    const r = await api.deleteTask(projectId, deleteTask.taskId)
    setDeleteBusy(false)
    if (!r.ok) {
      setSaveError(errText(r))
      setDeleteTask(null)
      return
    }
    setDeleteTask(null)
    if (selectedId === deleteTask.taskId) {
      setSelectedId(null)
      setDetail(null)
      setRunResult(null)
    }
    setTabs((prev) => prev.filter((t) => t.id !== deleteTask.taskId))
    await refreshAll()
  }

  const moveTask = async (folder: string) => {
    if (!detail) return
    setMoveBusy(true)
    setSaveMsg(null)
    setSaveError(null)
    const r = await api.moveTask(projectId, detail.task.task_id, folder)
    setMoveBusy(false)
    if (!r.ok) {
      setSaveError(errText(r))
      return
    }
    setSaveMsg(`已移动到「${folder || '根目录'}」`)
    await reloadDetail()
    await loadList()
  }

  // ------------------------------------------------------------ 文件夹动作
  const createFolder = async () => {
    if (!newFolder) return
    const seg = nfName.trim()
    if (!seg) {
      setNfError('文件夹名称不能为空')
      return
    }
    const full = joinFolder(newFolder.parent, seg)
    setNfBusy(true)
    setNfError(null)
    const r = await api.createTaskFolder(projectId, full)
    setNfBusy(false)
    if (!r.ok) {
      setNfError(errText(r))
      return
    }
    setFolders(r.data.folders ?? [])
    setExpanded((prev) => new Set(prev).add(newFolder.parent).add(full))
    setActiveFolder(full)
    setNewFolder(null)
  }

  const submitRename = async () => {
    if (!renameFolder) return
    const seg = rfName.trim()
    if (!seg) {
      setRfError('文件夹名称不能为空')
      return
    }
    const parent = renameFolder.path.split('/').slice(0, -1).join('/')
    const next = joinFolder(parent, seg)
    if (next === renameFolder.path) {
      setRenameFolder(null)
      return
    }
    setRfBusy(true)
    setRfError(null)
    const r = await api.renameTaskFolder(projectId, renameFolder.path, next)
    setRfBusy(false)
    if (!r.ok) {
      setRfError(errText(r))
      return
    }
    setFolders(r.data.folders ?? [])
    setRenameFolder(null)
    setActiveFolder(next)
    if (detail && (detail.task.folder === renameFolder.path || detail.task.folder.startsWith(renameFolder.path + '/'))) {
      await openTask(detail.task.task_id)
    }
    await loadList()
  }

  const submitDeleteFolder = async () => {
    if (!deleteFolder) return
    setDeleteBusy(true)
    const r = await api.deleteTaskFolder(projectId, deleteFolder.path)
    setDeleteBusy(false)
    if (!r.ok) {
      setFolderError(errText(r))
      setDeleteFolder(null)
      return
    }
    setFolders(r.data.folders ?? [])
    setDeleteFolder(null)
    if (detail && detail.task.folder.startsWith(deleteFolder.path)) {
      setSelectedId(null)
      setDetail(null)
    }
    await loadList()
  }

  const submitNewTask = async () => {
    if (!newTask) return
    if (!ntName.trim()) {
      setNtError('任务名称不能为空')
      return
    }
    if (!ntSql.trim()) {
      setNtError(ntKind === 'sql' ? 'SQL 不能为空' : 'PySpark 代码不能为空')
      return
    }
    setNtBusy(true)
    setNtError(null)
    const r = await api.createTask(projectId, {
      name: ntName.trim(),
      sql: ntSql,
      description: ntDesc.trim(),
      folder: newTask.folder,
      kind: ntKind,
    })
    setNtBusy(false)
    if (!r.ok) {
      setNtError(errText(r))
      return
    }
    const tid = r.data.task.task_id
    setNewTask(null)
    setActiveFolder(r.data.task.folder)
    await refreshAll()
    await openTask(tid)
  }

  // ------------------------------------------------------------ 构建树
  const tree = buildTree(folders ?? [], tasks ?? [])
  const rootTasks = (tasks ?? []).filter((t) => !t.folder)
  const menuFolder = menu?.folder ?? ''

  const menuItems: MenuItem[] = menu
    ? [
        { label: '新建任务', hint: 'SQL / PySpark', onSelect: () => openNewTask(menuFolder) },
        { label: '新建子文件夹', onSelect: () => openNewFolder(menuFolder) },
        { label: '重命名文件夹', onSelect: () => openRenameFolder(menuFolder) },
        { label: '删除文件夹', danger: true, onSelect: () => setDeleteFolder({ path: menuFolder }) },
      ]
    : []

  // ------------------------------------------------------------ 渲染
  if (isDefaultProject) {
    return (
      <div className="notice-box">
        <div className="notice-title">默认项目没有任务库</div>
        默认项目由环境变量构造（向后兼容），不启用任务库。新建一个项目并进入它，
        就能用 ods / dwd / dwm / ads 文件夹管理只读 SQL 与 PySpark 任务。
      </div>
    )
  }

  const taskFolderOptions = ['', ...(folders ?? []).map((f) => f.path)]

  // ------------------------------------------------------------ 渲染用派生值
  const lineCount = Math.max(1, sql.split('\n').length)
  const filteredTree = sideQuery.trim() ? filterTree(tree, sideQuery.trim()) : tree
  const filteredRootTasks = sideQuery.trim()
    ? rootTasks.filter((t) => t.name.toLowerCase().includes(sideQuery.trim().toLowerCase()))
    : rootTasks
  const recentTasks = (tasks ?? []).slice(0, 6)
  const stats = {
    folders: folders?.length ?? 0,
    tasks: tasks?.length ?? 0,
    sql: (tasks ?? []).filter((t) => t.kind !== 'pyspark').length,
    pyspark: (tasks ?? []).filter((t) => t.kind === 'pyspark').length,
  }

  const syncGutter = (el: HTMLTextAreaElement) => {
    const g = gutterRef.current
    if (g) g.scrollTop = el.scrollTop
  }

  return (
    <div className="ide-workspace">
      <div className="ide-tabs">
        <button
          type="button"
          aria-current={!selectedId ? 'page' : undefined}
          className={!selectedId ? 'ide-tab active' : 'ide-tab'}
          onClick={() => askNav({ kind: 'overview' })}
        >
          <span className="ide-tab-name">概览</span>
        </button>
        {tabs.map((t) => (
          <span key={t.id} className={selectedId === t.id ? 'ide-tab active' : 'ide-tab'}>
            <button
              type="button"
              aria-current={selectedId === t.id ? 'page' : undefined}
              className="ide-tab-label"
              title={t.name}
              onClick={() => askNav({ kind: 'task', id: t.id })}
            >
              {t.name}
            </button>
            <button
              type="button"
              className="ide-tab-close"
              aria-label={`关闭 ${t.name}`}
              onClick={() => askNav({ kind: 'close', id: t.id })}
            >
              ×
            </button>
          </span>
        ))}
        <span className="spacer" />
        <span className="ide-meta mono">
          {stats.folders} 个文件夹 · {stats.tasks} 个任务
        </span>
        <button
          className="btn btn-ghost btn-tiny"
          disabled={listBusy}
          onClick={() => void refreshAll()}
        >
          {listBusy ? '刷新中…' : '刷新'}
        </button>
        <button
          className="btn btn-primary btn-tiny"
          onClick={() => openNewTask(defaultFolder())}
        >
          ＋ 新建任务
        </button>
      </div>

      {folderError && <div className="alert alert-bad">{folderError}</div>}
      {listError && <div className="alert alert-bad">{listError}</div>}

      <div className="ide-body">
        <aside className="task-side" aria-label="任务导航">
          <div className="task-side-head">
            <input
              className="task-search"
              value={sideQuery}
              onChange={(e) => setSideQuery(e.target.value)}
              placeholder="搜索任务 / 文件夹"
              aria-label="搜索任务或文件夹"
            />
          </div>
          {folders === null && <div className="side-empty">正在加载文件夹…</div>}
          {folders !== null && folders.length === 0 && (
            <div className="side-empty">还没有文件夹，点「＋ 新建子文件夹」建一层。</div>
          )}
          <div className="task-side-scroll">
            <ul className="task-tree">
              {filteredTree.map((node) => (
                <TreeFolder
                  key={node.path}
                  node={node}
                  expanded={expanded}
                  activeFolder={activeFolder}
                  selectedTaskId={selectedId}
                  onToggle={toggleFolder}
                  onSelectFolder={setActiveFolder}
                  onSelectTask={(tid) => askNav({ kind: 'task', id: tid })}
                  onMenu={openMenu}
                />
              ))}
              {filteredRootTasks.length > 0 && (
                <li className="tree-node">
                  <div className="tree-folder-row tree-root-row">
                    <span className="tree-caret" aria-hidden="true">
                      ·
                    </span>
                    <span className="tree-name">
                      <span className="mono">未归类</span>
                      <span className="tree-count mono">{filteredRootTasks.length}</span>
                    </span>
                  </div>
                  <ul className="tree-children">
                    {filteredRootTasks.map((t) => (
                      <li key={t.task_id}>
                        <button
                          className={selectedId === t.task_id ? 'tree-task active' : 'tree-task'}
                          onClick={() => askNav({ kind: 'task', id: t.task_id })}
                        >
                          <span
                            className={t.kind === 'pyspark' ? 'kind-dot kind-pyspark' : 'kind-dot'}
                            aria-hidden="true"
                          />
                          <span className="tree-task-name">{t.name}</span>
                          <span className="tree-task-meta mono">
                            {t.kind === 'pyspark' ? 'py' : 'sql'} · v{t.revision}
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>
                </li>
              )}
            </ul>
            <div className="meta tree-hint">
              右键文件夹可新建任务或子文件夹；也可以点文件夹行的 ⋯。
            </div>
            <button
              className="linkbtn task-side-more"
              onClick={() => openNewFolder(activeFolder)}
            >
              ＋ 新建子文件夹
            </button>
          </div>
        </aside>

        <section className="task-main" aria-live="polite">
          {detailLoading && <div className="loading">正在加载任务…</div>}
          {detailError && (
            <div className="alert alert-bad">
              <strong>任务加载失败</strong>
              {detailError}
            </div>
          )}

          {!detailLoading && !detail && !detailError && (
            <div className="ide-overview">
              <div className="ide-overview-head">
                <div>
                  <div className="ide-kicker mono">项目 · 概览</div>
                  <h2 className="ide-title">{projectName || '当前项目'}</h2>
                  {projectDescription && <p className="ide-desc">{projectDescription}</p>}
                </div>
              </div>
              <div className="ide-stats">
                <div className="ide-stat">
                  <b>{stats.folders}</b>
                  <span>文件夹</span>
                </div>
                <div className="ide-stat">
                  <b>{stats.tasks}</b>
                  <span>任务</span>
                </div>
                <div className="ide-stat">
                  <b>{stats.sql}</b>
                  <span>SQL 查询</span>
                </div>
                <div className="ide-stat">
                  <b>{stats.pyspark}</b>
                  <span>PySpark</span>
                </div>
              </div>
              <div className="ide-overview-cols">
                <section className="ide-card">
                  <div className="ide-card-title">最近任务</div>
                  {recentTasks.length === 0 ? (
                    <p className="meta">
                      还没有任务。右键 ods / dwd / dwm / ads 文件夹，或点右上「＋ 新建任务」。
                    </p>
                  ) : (
                    <ul className="ide-recent">
                      {recentTasks.map((t) => (
                        <li key={t.task_id}>
                          <button
                            className="linkbtn"
                            title={t.name}
                            onClick={() => askNav({ kind: 'task', id: t.task_id })}
                          >
                            {t.name}
                          </button>
                          <span className="meta mono">
                            {t.folder || '根目录'} · v{t.revision}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </section>
                <section className="ide-card">
                  <div className="ide-card-title">项目资料</div>
                  <p className="meta">
                    数据源、表快照与语义口径在「概览」页维护；任务这里只保存只读 SQL 与版本。
                  </p>
                  <button className="btn btn-tiny" onClick={() => onOpenOverview?.()}>
                    打开项目概览 →
                  </button>
                </section>
              </div>
            </div>
          )}

          {detail && (
            <div className="editor-shell">
              <div className="editor-bar">
                <input
                  className="editor-name"
                  value={name}
                  aria-label="任务名称"
                  onChange={(e) => setName(e.target.value)}
                />
                <span
                  className={detail.task.kind === 'pyspark' ? 'badge badge-warn' : 'badge'}
                >
                  {KIND_LABEL[detail.task.kind] ?? detail.task.kind}
                </span>
                <span className="chip mono" title="所属文件夹">
                  {detail.task.folder || '根目录'}
                </span>
                <span className="chip mono" title="当前版本">
                  v{detail.task.revision}
                </span>
                <span className="spacer" />
                <button
                  className="btn btn-ghost btn-tiny"
                  onClick={() =>
                    setDeleteTask({ taskId: detail.task.task_id, name: detail.task.name })
                  }
                >
                  删除
                </button>
                <button
                  className="btn btn-tiny"
                  disabled={runBusy || sqlDirty || detail.task.kind !== 'sql'}
                  title={
                    detail.task.kind !== 'sql'
                      ? 'PySpark 执行器尚未接入'
                      : sqlDirty
                        ? '先保存再运行'
                        : '运行已保存的 SQL'
                  }
                  onClick={() => void runTask()}
                >
                  {runBusy ? '运行中…' : '运行'}
                </button>
                <button
                  className="btn btn-primary btn-tiny"
                  disabled={saveBusy || !name.trim()}
                  onClick={() => void saveTask()}
                >
                  {saveBusy ? '保存中…' : '保存'}
                </button>
              </div>

              {(saveMsg || saveError || runError) && (
                <div
                  className={saveError || runError ? 'editor-note editor-note-bad' : 'editor-note'}
                  role="status"
                >
                  {saveError || runError || saveMsg}
                </div>
              )}

              <div className="editor-cols">
                <div className="editor-gutter mono" ref={gutterRef} aria-hidden="true">
                  {Array.from({ length: lineCount }, (_, i) => (
                    <span key={i}>{i + 1}</span>
                  ))}
                </div>
                <textarea
                  className="editor-code mono"
                  value={sql}
                  spellCheck={false}
                  aria-label={detail.task.kind === 'pyspark' ? 'PySpark 代码' : 'SQL（只读）'}
                  onChange={(e) => {
                    setSql(e.target.value)
                    setSqlDirty(true)
                  }}
                  onScroll={(e) => syncGutter(e.currentTarget)}
                  onKeyDown={(e) => {
                    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
                      e.preventDefault()
                      void saveTask()
                      return
                    }
                    if (e.key === 'Tab') {
                      e.preventDefault()
                      const el = e.currentTarget
                      if (e.shiftKey) {
                        // 反缩进：删掉光标前最多两个空格
                        const start = el.selectionStart
                        const spaces = sql.slice(0, start).match(/ {1,2}$/)
                        if (spaces) {
                          el.setRangeText('', start - spaces[0].length, start, 'end')
                          setSql(el.value)
                          setSqlDirty(true)
                        }
                        return
                      }
                      el.setRangeText('  ', el.selectionStart, el.selectionEnd, 'end')
                      setSql(el.value)
                      setSqlDirty(true)
                    }
                  }}
                />
              </div>

              <div className="editor-foot">
                <span className="meta">
                  {detail.task.kind === 'pyspark'
                    ? KIND_HINT.pyspark
                    : `只读 ${KIND_HINT.sql}`}
                </span>
                <span className="spacer" />
                {sqlDirty ? (
                  <span className="editor-dirty mono">● 未保存</span>
                ) : (
                  <span className="meta mono">{lineCount} 行 · 已保存</span>
                )}
                <span className="meta mono">Ctrl+Enter 保存 · Tab 缩进</span>
              </div>

              <div className="bottom-panel">
                <div className="bottom-tabs" role="tablist" aria-label="运行与版本">
                  <button
                    type="button"
                    role="tab"
                    aria-selected={bottom === 'run'}
                    className={bottom === 'run' ? 'bottom-tab active' : 'bottom-tab'}
                    onClick={() => setBottom(bottom === 'run' ? null : 'run')}
                  >
                    运行记录
                    {runResult && <span className="bottom-dot" aria-hidden="true" />}
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={bottom === 'history'}
                    className={bottom === 'history' ? 'bottom-tab active' : 'bottom-tab'}
                    onClick={() => setBottom(bottom === 'history' ? null : 'history')}
                  >
                    版本历史 <span className="bottom-count mono">{detail.revisions.length}</span>
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={bottom === 'info'}
                    className={bottom === 'info' ? 'bottom-tab active' : 'bottom-tab'}
                    onClick={() => setBottom(bottom === 'info' ? null : 'info')}
                  >
                    任务信息
                  </button>
                  <span className="spacer" />
                  <span className="meta mono">
                    {bottom ? '点当前标签收起' : '点标签展开'}
                  </span>
                </div>

                {bottom && (
                  <div className="bottom-body">
                    {bottom === 'run' && (
                      <>
                        {!runResult && !runBusy && (
                          <p className="meta bottom-empty">
                            还没有运行记录。保存后点「运行」，结果与耗时显示在这里。
                          </p>
                        )}
                        {runBusy && <p className="meta">正在运行已保存的 SQL…</p>}
                        {runResult && (
                          <>
                            <div className="bottom-run-head">
                              <span className={runResult.ok ? 'badge badge-ok' : 'badge badge-warn'}>
                                {runResult.ok ? '成功' : runResult.status}
                              </span>
                              <span className="meta mono">
                                {runResult.result
                                  ? `${runResult.result.row_count ?? 0} 行 · ${runResult.result.elapsed_ms ?? 0} ms`
                                  : '未返回结果集'}
                              </span>
                              <span className="spacer" />
                              <span className="meta mono">{runResult.sql}</span>
                            </div>
                            {runResult.result ? (
                              <ResultTable result={toResultPayload(runResult.result)} />
                            ) : (
                              <div className="alert alert-warn">
                                <strong>{runResult.status}</strong>
                                {runResult.message}
                              </div>
                            )}
                          </>
                        )}
                      </>
                    )}

                    {bottom === 'history' && (
                      <div className="bottom-history">
                        <div className="meta bottom-history-hint">
                          历史只增不改；回滚 = 用旧内容生成新版本
                        </div>
                        <ul className="rev-list">
                          {detail.revisions
                            .slice()
                            .sort((a, b) => b.revision - a.revision)
                            .map((rev) => (
                              <li key={rev.revision} className="rev-item">
                                <div className="rev-head">
                                  <span className="badge mono">v{rev.revision}</span>
                                  {rev.author === 'ai' && (
                                    <span className="badge badge-ai">AI 修改</span>
                                  )}
                                  <span className="meta">{authorLabel(rev.author)}</span>
                                  <span className="meta mono">{rev.created_at}</span>
                                  <span className="spacer" />
                                  <button
                                    className="linkbtn"
                                    onClick={() => {
                                      setSql(rev.sql)
                                      setSqlDirty(true)
                                      setSaveMsg(`已载入 v${rev.revision}，保存后成为新版本`)
                                    }}
                                  >
                                    载入编辑器
                                  </button>
                                  <button
                                    className="linkbtn"
                                    onClick={() => {
                                      void (async () => {
                                        setSaveBusy(true)
                                        const r = await api.restoreTaskRevision(
                                          projectId,
                                          detail.task.task_id,
                                          rev.revision,
                                        )
                                        setSaveBusy(false)
                                        if (!r.ok) {
                                          setSaveError(errText(r))
                                          return
                                        }
                                        setSaveMsg(
                                          `已回滚到 v${rev.revision}（新版本 v${r.data.revision}）`,
                                        )
                                        await reloadDetail()
                                        await loadList()
                                      })()
                                    }}
                                  >
                                    回滚
                                  </button>
                                </div>
                                {rev.note && <div className="rev-note">{rev.note}</div>}
                              </li>
                            ))}
                        </ul>
                      </div>
                    )}

                    {bottom === 'info' && (
                      <div className="field-grid info-grid">
                        <label className="field">
                          <span>所在文件夹（移动不生成版本）</span>
                          <select
                            value={detail.task.folder}
                            disabled={moveBusy}
                            onChange={(e) => void moveTask(e.target.value)}
                          >
                            {taskFolderOptions.map((f) => (
                              <option key={f || '__root__'} value={f}>
                                {f || '根目录'}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label className="field">
                          <span>任务类型</span>
                          <select
                            value={detail.task.kind}
                            onChange={(e) => {
                              const kind = e.target.value as 'sql' | 'pyspark'
                              if (kind === detail.task.kind) return
                              void (async () => {
                                setSaveBusy(true)
                                setSaveError(null)
                                const r = await api.updateTask(projectId, detail.task.task_id, {
                                  kind,
                                  note: `任务类型改为 ${KIND_LABEL[kind] ?? kind}`,
                                })
                                setSaveBusy(false)
                                if (!r.ok) {
                                  setSaveError(errText(r))
                                  return
                                }
                                await reloadDetail()
                                await loadList()
                              })()
                            }}
                          >
                            <option value="sql">SQL 查询</option>
                            <option value="pyspark">PySpark</option>
                          </select>
                        </label>
                        <label className="field span2">
                          <span>说明</span>
                          <input
                            value={description}
                            onChange={(e) => setDescription(e.target.value)}
                            placeholder="可选：这个任务做什么"
                          />
                        </label>
                        <label className="field span2">
                          <span>保存备注（可选）</span>
                          <input
                            value={note}
                            onChange={(e) => setNote(e.target.value)}
                            placeholder="例如：口径改成含退款"
                          />
                        </label>
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          )}
        </section>
      </div>

      {menu && (
        <ContextMenu
          x={menu.x}
          y={menu.y}
          title={menu.folder}
          items={menuItems}
          onClose={() => setMenu(null)}
        />
      )}

      {/* -------------------------------------------------- 新建任务 */}
      {newTask && (
        <Dialog
          title={`${projectName ? projectName + ' · ' : ''}在「${newTask.folder || '根目录'}」新建任务`}
          onClose={() => setNewTask(null)}
        >
          <div className="field-grid">
            <label className="field">
              <span>任务名称</span>
              <input
                value={ntName}
                placeholder="例如：各渠道实收金额"
                onChange={(e) => setNtName(e.target.value)}
              />
            </label>
            <label className="field">
              <span>任务类型</span>
              <select value={ntKind} onChange={(e) => setNtKind(e.target.value as 'sql' | 'pyspark')}>
                <option value="sql">SQL 查询（可运行）</option>
                <option value="pyspark">PySpark（可保存，执行器未接入）</option>
              </select>
            </label>
            <label className="field span2">
              <span>说明（可选）</span>
              <input value={ntDesc} onChange={(e) => setNtDesc(e.target.value)} />
            </label>
            <label className="field span2">
              <span>{ntKind === 'pyspark' ? 'PySpark 代码' : 'SQL（只读）'}</span>
              <textarea
                className="sql-editor mono"
                rows={10}
                value={ntSql}
                spellCheck={false}
                placeholder={ntKind === 'pyspark' ? '例如：df = spark.read...' : 'SELECT ...'}
                onChange={(e) => setNtSql(e.target.value)}
              />
            </label>
            <div className="span2 meta">{KIND_HINT[ntKind]}</div>
          </div>
          {ntError && <div className="alert alert-bad">{ntError}</div>}
          <div className="modal-actions">
            <button
              className="btn btn-primary"
              disabled={ntBusy || !ntName.trim() || !ntSql.trim()}
              onClick={() => void submitNewTask()}
            >
              {ntBusy ? '创建中…' : '创建任务'}
            </button>
            <button className="btn btn-ghost" onClick={() => setNewTask(null)}>
              取消
            </button>
          </div>
        </Dialog>
      )}

      {/* -------------------------------------------------- 新建文件夹 */}
      {newFolder && (
        <Dialog
          title={`在「${newFolder.parent || '项目根目录'}」新建文件夹`}
          onClose={() => setNewFolder(null)}
        >
          <div className="field-grid">
            <label className="field span2">
              <span>文件夹名称</span>
              <input
                value={nfName}
                placeholder="例如：dwd_order"
                onChange={(e) => setNfName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void createFolder()
                }}
              />
            </label>
            <div className="span2 meta mono">
              将创建：{joinFolder(newFolder.parent, nfName.trim() || '…')}
            </div>
          </div>
          {nfError && <div className="alert alert-bad">{nfError}</div>}
          <div className="modal-actions">
            <button
              className="btn btn-primary"
              disabled={nfBusy || !nfName.trim()}
              onClick={() => void createFolder()}
            >
              {nfBusy ? '创建中…' : '创建文件夹'}
            </button>
            <button className="btn btn-ghost" onClick={() => setNewFolder(null)}>
              取消
            </button>
          </div>
        </Dialog>
      )}

      {/* -------------------------------------------------- 重命名文件夹 */}
      {renameFolder && (
        <Dialog title={`重命名「${renameFolder.path}」`} onClose={() => setRenameFolder(null)}>
          <div className="field-grid">
            <label className="field span2">
              <span>新名称</span>
              <input
                value={rfName}
                onChange={(e) => setRfName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void submitRename()
                }}
              />
            </label>
            <div className="span2 meta">
              子文件夹与任务会一起搬到新路径；任务版本不受影响。
            </div>
          </div>
          {rfError && <div className="alert alert-bad">{rfError}</div>}
          <div className="modal-actions">
            <button
              className="btn btn-primary"
              disabled={rfBusy || !rfName.trim()}
              onClick={() => void submitRename()}
            >
              {rfBusy ? '重命名中…' : '重命名'}
            </button>
            <button className="btn btn-ghost" onClick={() => setRenameFolder(null)}>
              取消
            </button>
          </div>
        </Dialog>
      )}

      {/* -------------------------------------------------- 删除确认 */}
      {deleteFolder && (
        <Dialog title={`删除文件夹「${deleteFolder.path}」`} onClose={() => setDeleteFolder(null)}>
          <div className="alert alert-warn">
            只有空文件夹（没有任务、也没有子文件夹）能删除。任务请先挪走或删除。
          </div>
          <div className="modal-actions">
            <button
              className="btn"
              disabled={deleteBusy}
              onClick={() => void submitDeleteFolder()}
            >
              {deleteBusy ? '删除中…' : '确认删除文件夹'}
            </button>
            <button className="btn btn-ghost" onClick={() => setDeleteFolder(null)}>
              取消
            </button>
          </div>
        </Dialog>
      )}

      {pendingNav && (
        <Dialog title="有未保存的 SQL 改动" onClose={() => setPendingNav(null)}>
          <p className="modal-text">
            任务「{detail?.task.name ?? '当前任务'}」的 SQL 改动还没保存。切换到别处会丢失这次修改。
          </p>
          <div className="modal-actions">
            <button
              className="btn btn-primary"
              disabled={saveBusy}
              onClick={() => {
                if (!pendingNav) return
                const nav = pendingNav
                void (async () => {
                  const ok = await saveTask()
                  if (ok) applyNav(nav)
                })()
              }}
            >
              {saveBusy ? '保存中…' : '保存并切换'}
            </button>
            <button
              className="btn"
              onClick={() => {
                if (pendingNav) applyNav(pendingNav)
              }}
            >
              放弃修改并切换
            </button>
            <button className="btn btn-ghost" onClick={() => setPendingNav(null)}>
              取消
            </button>
          </div>
        </Dialog>
      )}

      {deleteTask && (
        <Dialog title={`删除任务「${deleteTask.name}」`} onClose={() => setDeleteTask(null)}>
          <div className="alert alert-warn">
            任务与全部版本历史会一起删除，不可恢复。
          </div>
          <div className="modal-actions">
            <button
              className="btn"
              disabled={deleteBusy}
              onClick={() => void submitDeleteTask()}
            >
              {deleteBusy ? '删除中…' : '确认删除任务'}
            </button>
            <button className="btn btn-ghost" onClick={() => setDeleteTask(null)}>
              取消
            </button>
          </div>
        </Dialog>
      )}
    </div>
  )
}
