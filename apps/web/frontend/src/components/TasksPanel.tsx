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
export function TasksPanel({
  projectId,
  projectName,
  isDefaultProject,
}: {
  projectId: string
  /** 只用于对话框里显示上下文 */
  projectName?: string
  /** 默认项目没有任务库：不请求任务接口，直接给引导 */
  isDefaultProject: boolean
}) {
  // ------------------------------------------------------------ 列表
  const [folders, setFolders] = useState<TaskFolder[] | null>(null)
  const [folderError, setFolderError] = useState<string | null>(null)
  const [tasks, setTasks] = useState<TaskSummary[] | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [listBusy, setListBusy] = useState(false)

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
    },
    [projectId],
  )

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
  const saveTask = async () => {
    if (!detail) return
    const tid = detail.task.task_id
    if (!name.trim()) {
      setSaveError('任务名称不能为空')
      return
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
      return
    }
    setSaveMsg(`已保存为 v${r.data.revision}`)
    setSqlDirty(false)
    setNote('')
    await reloadDetail()
    await loadList()
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

  return (
    <div className="task-workspace">
      <div className="task-toolbar">
        <button
          className="btn btn-primary btn-tiny"
          onClick={() => openNewTask(defaultFolder())}
        >
          ＋ 新建任务
        </button>
        <button
          className="btn btn-tiny"
          onClick={() => openNewFolder(activeFolder)}
        >
          ＋ 新建文件夹
        </button>
        <span className="spacer" />
        <span className="meta mono">
          {folders?.length ?? 0} 个文件夹 · {tasks?.length ?? 0} 个任务
        </span>
        <button
          className="btn btn-ghost btn-tiny"
          disabled={listBusy}
          onClick={() => void refreshAll()}
        >
          {listBusy ? '刷新中…' : '刷新'}
        </button>
      </div>

      {folderError && <div className="alert alert-bad">{folderError}</div>}
      {listError && <div className="alert alert-bad">{listError}</div>}

      <div className="task-grid">
        <aside className="task-side" aria-label="任务文件夹">
          {folders === null && <div className="side-empty">正在加载文件夹…</div>}
          {folders !== null && folders.length === 0 && (
            <div className="side-empty">
              还没有文件夹，点「＋ 新建文件夹」建一层。
            </div>
          )}
          <ul className="task-tree">
            {tree.map((node) => (
              <TreeFolder
                key={node.path}
                node={node}
                expanded={expanded}
                activeFolder={activeFolder}
                selectedTaskId={selectedId}
                onToggle={toggleFolder}
                onSelectFolder={setActiveFolder}
                onSelectTask={(tid) => void openTask(tid)}
                onMenu={openMenu}
              />
            ))}
            {rootTasks.length > 0 && (
              <li className="tree-node">
                <div className="tree-folder-row tree-root-row">
                  <span className="tree-caret" aria-hidden="true">
                    ·
                  </span>
                  <span className="tree-name">
                    <span className="mono">未归类</span>
                    <span className="tree-count mono">{rootTasks.length}</span>
                  </span>
                </div>
                <ul className="tree-children">
                  {rootTasks.map((t) => (
                    <li key={t.task_id}>
                      <button
                        className={
                          selectedId === t.task_id ? 'tree-task active' : 'tree-task'
                        }
                        onClick={() => void openTask(t.task_id)}
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
              </li>
            )}
          </ul>
          <div className="meta tree-hint">
            右键文件夹可新建任务或子文件夹；也可以点文件夹行的 ⋯。
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
            <div className="empty-block">
              <div className="empty-title">还没有选中任务</div>
              <div className="empty-hint">
                从左边的文件夹树里选一个任务，或者右键文件夹新建一个。
                任务对应大数据项目里的「新建查询」——保存的是只读 SQL，
                点「运行」才真正执行；PySpark 任务当前只能保存与版本管理。
              </div>
            </div>
          )}

          {detail && (
            <>
              <div className="detail-head">
                <h2 className="detail-title">{detail.task.name}</h2>
                <span className={detail.task.kind === 'pyspark' ? 'badge badge-warn' : 'badge'}>
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
                  删除任务
                </button>
              </div>

              {saveMsg && <div className="alert alert-info">{saveMsg}</div>}
              {saveError && <div className="alert alert-bad">{saveError}</div>}

              <div className="section-box">
                <div className="section-title">
                  任务内容
                  <span className="spacer" />
                  <span className="meta">
                    {detail.task.kind === 'pyspark'
                      ? KIND_HINT.pyspark
                      : `只读 ${KIND_HINT.sql}`}
                  </span>
                </div>
                <div className="field-grid">
                  <label className="field">
                    <span>名称</span>
                    <input value={name} onChange={(e) => setName(e.target.value)} />
                  </label>
                  <label className="field">
                    <span>所在文件夹</span>
                    <select
                      value={detail.task.folder}
                      disabled={moveBusy}
                      onChange={(e) => void moveTask(e.target.value)}
                      title="移动只改归属，不生成新版本"
                    >
                      {taskFolderOptions.map((f) => (
                        <option key={f || '__root__'} value={f}>
                          {f || '根目录'}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="field">
                    <span>类型</span>
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
                  <label className="field">
                    <span>说明</span>
                    <input
                      value={description}
                      onChange={(e) => setDescription(e.target.value)}
                      placeholder="可选：这个任务做什么"
                    />
                  </label>
                  <label className="field span2">
                    <span>
                      {detail.task.kind === 'pyspark' ? 'PySpark 代码' : 'SQL（只读）'}
                    </span>
                    <textarea
                      className="sql-editor mono"
                      rows={12}
                      value={sql}
                      spellCheck={false}
                      onChange={(e) => {
                        setSql(e.target.value)
                        setSqlDirty(true)
                      }}
                      onKeyDown={(e) => {
                        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
                          e.preventDefault()
                          void saveTask()
                        }
                      }}
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
                <div className="form-actions">
                  <button
                    className="btn btn-primary"
                    disabled={saveBusy || !name.trim()}
                    onClick={() => void saveTask()}
                  >
                    {saveBusy ? '保存中…' : '保存（Ctrl+Enter）'}
                  </button>
                  <button
                    className="btn"
                    disabled={
                      runBusy ||
                      sqlDirty ||
                      detail.task.kind !== 'sql'
                    }
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
                </div>
              </div>

              {runError && <div className="alert alert-bad">{runError}</div>}
              {runResult && (
                <div className="section-box">
                  <div className="section-title">
                    运行结果
                    <span className="spacer" />
                    <span className="meta mono">
                      {runResult.ok ? `${runResult.result?.row_count ?? 0} 行` : '未返回结果集'}
                      {runResult.result?.elapsed_ms
                        ? ` · ${runResult.result.elapsed_ms} ms`
                        : ''}
                    </span>
                  </div>
                  {runResult.result ? (
                    <ResultTable result={toResultPayload(runResult.result)} />
                  ) : (
                    <div className="alert alert-warn">
                      <strong>{runResult.status}</strong>
                      {runResult.message}
                    </div>
                  )}
                </div>
              )}

              <div className="section-box">
                <div className="section-title">
                  版本历史 <span className="meta">{detail.revisions.length}</span>
                  <span className="spacer" />
                  <span className="meta">历史只增不改；回滚 = 用旧内容生成新版本</span>
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
                                setSaveMsg(`已回滚到 v${rev.revision}（新版本 v${r.data.revision}）`)
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
            </>
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
