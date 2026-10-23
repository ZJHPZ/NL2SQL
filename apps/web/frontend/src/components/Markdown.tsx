/** 智能体回答的 Markdown 渲染。
 *
 * 为什么需要它：`answer` 是模型写的散文，实测经常带 `**加粗**`、列表、
 * 甚至代码片段。之前用 `white-space: pre-wrap` 直出，星号会原样显示，
 * 长结论也很难扫读。这里用 marked 解析 + DOMPurify 消毒：
 *   * 只选 marked（gzip 13.9KB）而不是 react-markdown（36KB/52 包）
 *   * 必须消毒——模型输出属于不可信内容，绝不能直接 innerHTML
 * 纯文本段落仍按原样换行（breaks:true），因为模型常一句话一行。
 */
import { useMemo } from 'react'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

marked.setOptions({ gfm: true, breaks: true })

export function Markdown({ text, className }: { text: string; className?: string }) {
  const html = useMemo(() => {
    // marked 的类型是 string | Promise<string>；这里同步模式必为 string
    const raw = marked.parse(text, { async: false }) as string
    return DOMPurify.sanitize(raw, { USE_PROFILES: { html: true } })
  }, [text])

  return (
    <div
      className={className ? `md ${className}` : 'md'}
      // 消毒后的 HTML：内容来自模型，属于不可信输入
      dangerouslySetInnerHTML={{ __html: html }}
    />
  )
}
