/**
 * 基于数据源表结构（DDL）的轻量 SQL lint（二期补全的一部分）。
 *
 * 只报两类「确定性问题」，避免误报打断书写：
 *   1. FROM / JOIN 后面引用了数据源里不存在的表；
 *   2. `别名.列` / `表名.列` 里列不存在（别名来自同一条语句的 FROM/JOIN）。
 * 裸列、函数、CTE、子查询别名等一律不报——宁可少报，不可乱报。
 *
 * 数据来源与补全相同：GET /api/tables（任务运行同一数据源），不新增接口、不调 LLM。
 */
import { linter, type Diagnostic } from '@codemirror/lint'
import type { Extension } from '@codemirror/state'
import { syntaxTree } from '@codemirror/language'
import type { TableInfo } from '../types'

/** FROM 表名后面这些词不是别名（如 FROM t WHERE / FROM t LEFT JOIN）。 */
const NOT_ALIAS = new Set([
  'on',
  'where',
  'group',
  'order',
  'having',
  'limit',
  'offset',
  'union',
  'except',
  'intersect',
  'join',
  'left',
  'right',
  'inner',
  'outer',
  'full',
  'cross',
  'using',
  'natural',
  'window',
  'qualify',
  'fetch',
  'returning',
  'and',
  'or',
  'set',
  'values',
  'from',
  'select',
  'as',
])

/** 把字符串字面量、单行/块注释替换成空格（保留长度，方便定位）。 */
function maskNonCode(text: string): string {
  const out = text.split('')
  let i = 0
  while (i < text.length) {
    const ch = text[i]
    if (ch === "'") {
      out[i] = ' '
      i += 1
      while (i < text.length) {
        if (text[i] === "'") {
          if (text[i + 1] === "'") {
            out[i] = ' '
            out[i + 1] = ' '
            i += 2
            continue
          }
          out[i] = ' '
          i += 1
          break
        }
        if (text[i] !== '\n') out[i] = ' '
        i += 1
      }
      continue
    }
    if (ch === '-' && text[i + 1] === '-') {
      while (i < text.length && text[i] !== '\n') {
        out[i] = ' '
        i += 1
      }
      continue
    }
    if (ch === '/' && text[i + 1] === '*') {
      out[i] = ' '
      out[i + 1] = ' '
      i += 2
      while (i < text.length && !(text[i] === '*' && text[i + 1] === '/')) {
        if (text[i] !== '\n') out[i] = ' '
        i += 1
      }
      if (i < text.length) {
        out[i] = ' '
        out[i + 1] = ' '
        i += 2
      }
      continue
    }
    i += 1
  }
  return out.join('')
}

type SchemaEntry = { table: TableInfo; columns: Set<string> }

function buildSchemaIndex(tables: TableInfo[]): Map<string, SchemaEntry> {
  const index = new Map<string, SchemaEntry>()
  for (const table of tables) {
    index.set(table.name.toLowerCase(), {
      table,
      columns: new Set(table.columns.map((column) => column.name.toLowerCase())),
    })
  }
  return index
}

const TABLE_REF =
  /\b(from|join)\s+((?:[A-Za-z_][\w$]*\.)?[A-Za-z_][\w$]*)(?:\s+(?:as\s+)?([A-Za-z_][\w$]*))?/gi
const QUALIFIED_COLUMN = /\b([A-Za-z_][\w$]*)\.([A-Za-z_][\w$]*)/g
/** WITH 定义的 CTE 名字：当成「已知表但列未知」，既不报表也不报列。 */
const CTE_DEF = /(?:^|\bwith\b|,)\s*([A-Za-z_][\w$]*)\s*(?:\([^)]*\))?\s+as\s*\(/gi

function lintStatement(
  text: string,
  offset: number,
  index: Map<string, SchemaEntry>,
  diagnostics: Diagnostic[],
): void {
  /** 别名 → 表名小写；null 表示 CTE 等「列不可知」的来源。 */
  const aliases = new Map<string, string | null>()
  const cteNames = new Set<string>()
  CTE_DEF.lastIndex = 0
  for (let match = CTE_DEF.exec(text); match; match = CTE_DEF.exec(text)) {
    cteNames.add(match[1].toLowerCase())
  }

  TABLE_REF.lastIndex = 0
  for (let match = TABLE_REF.exec(text); match; match = TABLE_REF.exec(text)) {
    const reference = match[2]
    const dotted = reference.lastIndexOf('.')
    const tableName = dotted >= 0 ? reference.slice(dotted + 1) : reference
    const tableKey = tableName.toLowerCase()
    const tableOffset =
      offset + match.index + match[0].toLowerCase().lastIndexOf(tableKey)
    const entry = index.get(tableKey)
    if (!entry) {
      if (cteNames.has(tableKey)) {
        aliases.set(tableKey, null)
        const cteAlias = match[3]
        if (cteAlias && !NOT_ALIAS.has(cteAlias.toLowerCase())) {
          aliases.set(cteAlias.toLowerCase(), null)
        }
        continue
      }
      diagnostics.push({
        from: tableOffset,
        to: tableOffset + tableName.length,
        severity: 'warning',
        message: `未知表「${tableName}」：当前数据源里没有这张表`,
      })
      continue
    }
    aliases.set(tableKey, tableKey)
    const alias = match[3]
    if (alias && !NOT_ALIAS.has(alias.toLowerCase())) {
      aliases.set(alias.toLowerCase(), tableKey)
    }
  }

  QUALIFIED_COLUMN.lastIndex = 0
  for (let match = QUALIFIED_COLUMN.exec(text); match; match = QUALIFIED_COLUMN.exec(text)) {
    const qualifier = match[1].toLowerCase()
    if (!aliases.has(qualifier) && !index.has(qualifier)) continue // schema.表 / 未解析别名：不报
    const tableKey = aliases.get(qualifier) ?? qualifier
    if (!tableKey) continue // CTE：列不可知，跳过
    const entry = index.get(tableKey)
    if (!entry) continue
    const column = match[2]
    if (entry.columns.has(column.toLowerCase())) continue
    diagnostics.push({
      from: offset + match.index,
      to: offset + match.index + match[0].length,
      severity: 'warning',
      message: `未知列「${match[1]}.${column}」：${entry.table.name} 没有这一列`,
    })
  }
}

/** 表结构驱动的 lint 扩展；表结构还没加载时返回空扩展（不标红任何东西）。 */
export function schemaLinter(tables: TableInfo[]): Extension {
  if (!tables.length) return []
  const index = buildSchemaIndex(tables)
  return linter(
    (view) => {
      const diagnostics: Diagnostic[] = []
      const doc = view.state.doc
      const root = syntaxTree(view.state).topNode
      for (let node = root.firstChild; node; node = node.nextSibling) {
        if (node.name !== 'Statement') continue
        const text = maskNonCode(doc.sliceString(node.from, node.to))
        lintStatement(text, node.from, index, diagnostics)
        if (diagnostics.length >= 20) break
      }
      return diagnostics
    },
    { delay: 500 },
  )
}
