import { Fragment, type ReactNode } from 'react'
import { theme } from 'antd'

/** 仅解析常用 Markdown 为 React 节点：原始 HTML 始终作为文本，链接只允许 http(s)。 */
function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\(https?:\/\/[^\s)]+\))/g).map((part, index) => {
    if (part.startsWith('**') && part.endsWith('**')) return <strong key={index}>{part.slice(2, -2)}</strong>
    if (part.startsWith('`') && part.endsWith('`')) return <code key={index}>{part.slice(1, -1)}</code>
    const link = part.match(/^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/)
    if (link) return <a key={index} href={link[2]} target="_blank" rel="noopener noreferrer">{link[1]}</a>
    return part
  })
}
export default function AgentMarkdown({ text }: { text: string }) {
  const { token } = theme.useToken()
  const blocks = text.split(/(```[^\n]*\n[\s\S]*?(?:```|$))/g)
  return <div style={{ overflowWrap: 'anywhere', lineHeight: 1.7, minWidth: 0 }}>
    {blocks.map((block, index) => block.startsWith('```') ?
      <pre key={index} style={{ background: token.colorFillQuaternary, padding: 12, borderRadius: token.borderRadius, overflowX: 'auto', whiteSpace: 'pre-wrap' }}><code>{block.replace(/^```[^\n]*\n/, '').replace(/```$/, '')}</code></pre> :
      <Fragment key={index}>{block.split('\n').map((line, i) => {
        const heading = line.match(/^#{1,6}\s+(.+)/)
        if (heading) return <p key={i} style={{ fontWeight: 600, margin: '12px 0 4px' }}>{inline(heading[1])}</p>
        const bullet = line.match(/^(?:[-*]|\d+\.)\s+(.+)/)
        return <div key={i} style={{ minHeight: line ? undefined : 8, paddingInlineStart: bullet ? 12 : undefined }}>{bullet && '• '}{inline(bullet?.[1] ?? line)}</div>
      })}</Fragment>)}
  </div>
}
