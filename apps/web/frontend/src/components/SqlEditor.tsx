/**
 * SQL / PySpark 代码编辑器（CodeMirror 6）。
 *
 * 一期能力：语法高亮、括号/引号自动闭合、行号、撤销重做、
 * 基于数据源表结构的补全（关键字 / 表名 / 列名 / 别名.列）、Ctrl+Enter 保存。
 *
 * 编辑内容仍由父组件持有（value / onChange），未保存守卫、运行、保存
 * 等业务逻辑全部留在 TasksPanel；这里只负责「好写的编辑器」。
 * 补全数据来自 GET /api/tables（引擎默认数据源，与任务执行保持一致），
 * 不新增后端接口，也不调用 LLM。
 */
import { useEffect, useRef } from 'react'
import { Compartment, EditorState } from '@codemirror/state'
import {
  EditorView,
  keymap,
  lineNumbers,
  highlightActiveLine,
  highlightActiveLineGutter,
  highlightSpecialChars,
  drawSelection,
  dropCursor,
  rectangularSelection,
  crosshairCursor,
  type Command,
} from '@codemirror/view'
import {
  defaultKeymap,
  history,
  historyKeymap,
  indentLess,
  indentMore,
} from '@codemirror/commands'
import {
  autocompletion,
  closeBrackets,
  closeBracketsKeymap,
  completionKeymap,
} from '@codemirror/autocomplete'
import type { Completion } from '@codemirror/autocomplete'
import {
  bracketMatching,
  indentUnit,
  syntaxHighlighting,
  HighlightStyle,
} from '@codemirror/language'
import { sql, SQLite, type SQLNamespace } from '@codemirror/lang-sql'
import { tags } from '@lezer/highlight'
import type { TableInfo } from '../types'
import { schemaLinter } from './sqlLint'

/**
 * Tab 缩进：用两个空格，而不是 CodeMirror 默认的制表符。
 * 任务内容会落库并在结果区 `<pre>` 里展示，空格缩进在两边显示一致。
 */
const insertTwoSpaces: Command = ({ state, dispatch }) => {
  if (state.selection.ranges.some((range) => !range.empty)) {
    return indentMore({ state, dispatch })
  }
  dispatch(
    state.update(state.replaceSelection('  '), {
      scrollIntoView: true,
      userEvent: 'input',
    }),
  )
  return true
}

interface SqlEditorProps {
  /** 当前文本（切任务时整体替换） */
  value: string
  /** 每次输入回传全文 */
  onChange: (value: string) => void
  /** 数据源表结构，用于补全；空数组时只补关键字 */
  tables: TableInfo[]
  /** 可访问名称，例如「SQL 代码」 */
  ariaLabel: string
  /** Ctrl/Cmd+Enter */
  onSave: () => void
  /** Ctrl/Cmd+Shift+F：格式化（由父组件实现，PySpark 任务可不传） */
  onFormat?: () => void
}

/** 把表/列元数据转成 CodeMirror 的补全命名空间。 */
function buildSchema(tables: TableInfo[]): SQLNamespace {
  const schema: Record<
    string,
    { self: Completion; children: Completion[] }
  > = {}
  for (const table of tables) {
    const columns: Completion[] = table.columns.map((column) => ({
      label: column.name,
      type: 'property',
      detail: column.data_type,
      info: [
        `${column.data_type}${column.nullable ? '' : ' NOT NULL'}`,
        column.is_primary_key ? '主键' : '',
        column.comment,
      ]
        .filter(Boolean)
        .join(' · '),
      boost: column.is_primary_key ? 2 : 0,
    }))
    schema[table.name] = {
      self: {
        label: table.name,
        type: 'class',
        detail: `${table.column_count} 列`,
        info: table.comment || undefined,
      },
      children: columns,
    }
  }
  return schema
}

/** 暖纸墨线世界的语法高亮：不用花哨多色，靠字重与墨色区分层级。 */
const paperHighlight = HighlightStyle.define([
  { tag: tags.keyword, color: 'var(--ink)', fontWeight: '600' },
  {
    tag: [tags.operatorKeyword, tags.definitionKeyword, tags.modifier],
    color: 'var(--ink)',
    fontWeight: '600',
  },
  { tag: [tags.string, tags.special(tags.string)], color: 'var(--olive)' },
  { tag: [tags.number, tags.bool, tags.null], color: 'var(--ochre)' },
  {
    tag: [tags.comment, tags.lineComment, tags.blockComment],
    color: 'var(--ink-3)',
    fontStyle: 'italic',
  },
  {
    tag: [tags.typeName, tags.className, tags.namespace],
    color: 'var(--ink-2)',
  },
  { tag: [tags.propertyName, tags.variableName], color: 'var(--ink)' },
  {
    tag: [tags.function(tags.variableName), tags.function(tags.propertyName)],
    color: 'var(--olive)',
  },
  { tag: [tags.operator, tags.punctuation], color: 'var(--ink-2)' },
  { tag: tags.invalid, color: 'var(--vermilion)' },
])

/** 编辑器主题：纸底墨字、mono 12.5/22、54px 行号槽，与 v11 面板同材质。 */
const paperTheme = EditorView.theme(
  {
    '&': {
      height: '100%',
      fontSize: '12.5px',
      color: 'var(--ink)',
      backgroundColor: 'transparent',
    },
    '.cm-scroller': {
      fontFamily: "'JetBrains Mono', ui-monospace, SFMono-Regular, monospace",
      lineHeight: '22px',
      overflow: 'auto',
    },
    '.cm-content': {
      padding: '10px 0',
      caretColor: 'var(--ink)',
    },
    '.cm-line': { padding: '0 18px 0 12px' },
    '.cm-gutters': {
      backgroundColor: 'var(--paper-2)',
      color: 'var(--ink-3)',
      border: 'none',
      borderRight: '1px solid rgba(27, 24, 18, 0.10)',
      minWidth: '54px',
    },
    '.cm-lineNumbers .cm-gutterElement': {
      padding: '0 10px 0 8px',
      minWidth: '30px',
      textAlign: 'right',
    },
    '.cm-activeLine': { backgroundColor: 'rgba(27, 24, 18, 0.035)' },
    '.cm-activeLineGutter': {
      backgroundColor: 'transparent',
      color: 'var(--ink-2)',
    },
    '&.cm-focused': { outline: 'none' },
    '&.cm-focused .cm-scroller': {
      boxShadow: 'inset 0 0 0 2px rgba(74, 90, 58, 0.26)',
    },
    '&.cm-focused .cm-cursor': { borderLeftColor: 'var(--ink)' },
    '&.cm-focused .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection':
      { backgroundColor: 'rgba(74, 90, 58, 0.18)' },
    '.cm-selectionMatch': { backgroundColor: 'rgba(138, 109, 51, 0.18)' },
    '.cm-matchingBracket, .cm-nonmatchingBracket': {
      backgroundColor: 'rgba(74, 90, 58, 0.14)',
      outline: '1px solid rgba(74, 90, 58, 0.35)',
    },
    '.cm-tooltip': {
      backgroundColor: 'var(--paper)',
      border: '1px solid rgba(27, 24, 18, 0.30)',
      boxShadow: '0 12px 28px rgba(27, 24, 18, 0.18)',
      color: 'var(--ink)',
    },
    // 补全列表最多渲染 8 项（约 200px），不需要容器内滚动；
    // 内层 ul 是列表本身，去掉滚动后 axe 的 scrollable-region-focusable
    // 才能通过（键盘上下键仍由 CodeMirror 在编辑器内接管）。
    '.cm-tooltip.cm-tooltip-autocomplete > ul': {
      maxHeight: 'none !important',
      overflow: 'visible !important',
    },
    '.cm-tooltip-autocomplete': {
      fontFamily: "'JetBrains Mono', ui-monospace, monospace",
      fontSize: '12px',
    },
    '.cm-tooltip-autocomplete > ul > li': {
      padding: '3px 10px',
      lineHeight: '19px',
    },
    '.cm-tooltip-autocomplete > ul > li[aria-selected]': {
      backgroundColor: 'var(--ink)',
      color: 'var(--paper)',
    },
    '.cm-completionLabel': { fontFamily: 'inherit' },
    '.cm-completionDetail': {
      color: 'var(--ink-3)',
      fontStyle: 'normal',
      fontSize: '11px',
      marginLeft: '8px',
    },
    '.cm-tooltip-autocomplete > ul > li[aria-selected] .cm-completionDetail': {
      color: 'rgba(244, 239, 226, 0.72)',
    },
    '.cm-completionIcon': { display: 'none' },
    // 二期：DDL lint 的波浪线与提示框，保持纸面墨线语言。
    '.cm-lintRange-warning': {
      textDecoration: 'underline wavy var(--ochre) 1px',
      textUnderlineOffset: '3px',
    },
    '.cm-lintRange-error': {
      textDecoration: 'underline wavy var(--vermilion) 1px',
      textUnderlineOffset: '3px',
    },
    '.cm-tooltip-lint': { padding: '0' },
    '.cm-diagnostic': {
      fontFamily: "'JetBrains Mono', ui-monospace, monospace",
      fontSize: '11.5px',
      lineHeight: '18px',
      padding: '6px 10px 6px 9px',
      borderLeft: '3px solid var(--ochre)',
    },
    '.cm-diagnostic-error': { borderLeftColor: 'var(--vermilion)' },
    '.cm-diagnosticText': { color: 'var(--ink)' },
    '.cm-diagnosticSource': { display: 'none' },
  },
  { dark: false },
)

export default function SqlEditor({
  value,
  onChange,
  tables,
  ariaLabel,
  onSave,
  onFormat,
}: SqlEditorProps) {
  const hostRef = useRef<HTMLDivElement | null>(null)
  const viewRef = useRef<EditorView | null>(null)
  /** 外部灌入文本时不要把它当成用户输入（否则又标脏）。 */
  const syncingRef = useRef(false)
  const onChangeRef = useRef(onChange)
  const onSaveRef = useRef(onSave)
  const onFormatRef = useRef(onFormat)
  onChangeRef.current = onChange
  onSaveRef.current = onSave
  onFormatRef.current = onFormat

  const schemaCompartment = useRef(new Compartment())

  useEffect(() => {
    const host = hostRef.current
    if (!host) return

    const state = EditorState.create({
      doc: value,
      extensions: [
        lineNumbers(),
        highlightActiveLineGutter(),
        highlightSpecialChars(),
        history(),
        drawSelection(),
        dropCursor(),
        EditorState.allowMultipleSelections.of(true),
        indentUnit.of('  '),
        EditorState.tabSize.of(2),
        bracketMatching(),
        closeBrackets(),
        rectangularSelection(),
        crosshairCursor(),
        highlightActiveLine(),
        autocompletion({
          activateOnTyping: true,
          maxRenderedOptions: 8,
          icons: false,
        }),
        keymap.of([
          {
            key: 'Mod-Enter',
            run: () => {
              onSaveRef.current()
              return true
            },
          },
          {
            key: 'Shift-Mod-f',
            run: () => {
              const format = onFormatRef.current
              if (!format) return false
              format()
              return true
            },
          },
          ...closeBracketsKeymap,
          ...defaultKeymap,
          ...historyKeymap,
          ...completionKeymap,
          { key: 'Tab', run: insertTwoSpaces, shift: indentLess },
        ]),
        schemaCompartment.current.of([
          sql({
            dialect: SQLite,
            schema: buildSchema(tables),
            upperCaseKeywords: true,
          }),
          schemaLinter(tables),
        ]),
        paperTheme,
        syntaxHighlighting(paperHighlight),
        EditorView.contentAttributes.of({
          'aria-label': ariaLabel,
          'aria-multiline': 'true',
          spellcheck: 'false',
        }),
        EditorView.updateListener.of((update) => {
          if (update.docChanged && !syncingRef.current) {
            onChangeRef.current(update.state.doc.toString())
          }
        }),
      ],
    })

    const view = new EditorView({ state, parent: host })
    viewRef.current = view
    // 给 E2E/探针留一个稳定句柄（分发文本、读取文档），不参与业务逻辑。
    ;(window as unknown as { __sqlEditor?: EditorView }).__sqlEditor = view

    return () => {
      view.destroy()
      viewRef.current = null
      delete (window as unknown as { __sqlEditor?: EditorView }).__sqlEditor
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 切任务 / 外部改文本：整体替换，光标回到开头（不会触发 onChange）。
  useEffect(() => {
    const view = viewRef.current
    if (!view) return
    if (value === view.state.doc.toString()) return
    syncingRef.current = true
    view.dispatch({
      changes: { from: 0, to: view.state.doc.length, insert: value },
      selection: { anchor: Math.min(0, value.length) },
    })
    syncingRef.current = false
  }, [value])

  // 表结构加载完成后重配补全与 lint（不重建编辑器，保留文档与历史）。
  useEffect(() => {
    const view = viewRef.current
    if (!view) return
    view.dispatch({
      effects: schemaCompartment.current.reconfigure([
        sql({
          dialect: SQLite,
          schema: buildSchema(tables),
          upperCaseKeywords: true,
        }),
        schemaLinter(tables),
      ]),
    })
  }, [tables])

  return <div className="cm-host" ref={hostRef} />
}
