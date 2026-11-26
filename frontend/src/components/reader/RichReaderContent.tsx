import { firstEndingAfter } from '../../utils/mentions'
import { theme } from 'antd'
import { createElement, Fragment, useMemo, type CSSProperties, type ReactNode } from 'react'
import { ReaderMentionText, type ReaderMentions } from './ReaderMentionText'
import type { ReaderBlock, ReaderRun } from '../../api/reader'

interface Props {
  projectId: number
  text: string
  blocks: ReaderBlock[]
  mentions?: ReaderMentions
  activeStart?: number | null
}

/** 后端返回码点坐标；只截取正文，不把作者文字解释成 HTML。 */
function styledText(points: string[], runs: ReaderRun[], start: number, end: number): ReactNode {
  const result = []
  for (let i = firstEndingAfter(runs, start); i < runs.length && runs[i].start < end; i += 1) {
    const run = runs[i]
    const from = Math.max(start, run.start), to = Math.min(end, run.end)
    let node: ReactNode = points.slice(from, to).join('')
    if (run.italic) node = <em>{node}</em>
    if (run.bold) node = <strong>{node}</strong>
    result.push(<Fragment key={`${from}:${to}`}>{node}</Fragment>)
  }
  return result
}

/** 列表仅合并真正相邻的列表项；空行后的列表保持独立。 */
function groups(points: string[], blocks: ReaderBlock[]): ReaderBlock[][] {
  const result: ReaderBlock[][] = []
  for (const block of blocks) {
    const group = result[result.length - 1], previous = group?.[group.length - 1]
    const list = block.kind === 'bullet_list' || block.kind === 'ordered_list'
    const gap = previous ? points.slice(previous.end, block.start).join('') : ''
    if (list && previous?.kind === block.kind && ['', '\r', '\n', '\r\n'].includes(gap)) group.push(block)
    else result.push([block])
  }
  return result
}

export default function RichReaderContent({ projectId, text, blocks, mentions, activeStart }: Props) {
  const { token } = theme.useToken()
  const points = useMemo(() => Array.from(text), [text])
  const grouped = useMemo(() => groups(points, blocks), [points, blocks])
  const style: CSSProperties = { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', margin: '0 0 0.6em' }
  // 自然换行在富文档中是块间空隙。搜索词可只含换行，此时也要有可滚动的精确目标。
  const searchGap = (start: number, end: number) => {
    const focus = mentions?.focus
    if (focus?.origin !== 'book-search' && focus?.origin !== 'preflight') return null
    const from = Math.max(start, focus.start), to = Math.min(end, focus.end)
    if (from >= to) return null
    return <mark data-mention-focus tabIndex={-1} aria-label="命中的换行"
      style={{ whiteSpace: 'pre-wrap', background: token.colorWarningBg, borderInlineStart: `2px solid ${token.colorWarning}` }}>
      {points.slice(from, to).join('')}
    </mark>
  }
  const children = (block: ReaderBlock) => {
    if (!mentions) return styledText(points, block.runs, block.start, block.end)
    return <ReaderMentionText projectId={projectId} mentions={mentions} text={points.slice(block.start, block.end).join('')}
      offset={block.start} end={block.end} renderPart={(start, end) => styledText(points, block.runs, start, end)} />
  }
  // 空格式块可以与下一段共享起点，只有含正文的块可以跟随声音高亮。
  const isActive = (block: ReaderBlock) => (block.start === activeStart && block.end > block.start) || undefined
  return <div>{grouped.map((group, i) => {
    const first = group[0], key = `${first.start}:${first.end}:${i}`
    const active = isActive(first)
    const previous = grouped[i - 1]
    const before = searchGap(previous ? previous[previous.length - 1].end : 0, first.start)
    if (first.kind === 'bullet_list' || first.kind === 'ordered_list') {
      const tag = first.kind === 'bullet_list' ? 'ul' : 'ol'
      return createElement(tag, { key, style: { margin: '0 0 0.6em', paddingInlineStart: '2em' } },
        group.map((block, index) => <li key={`${block.start}:${index}`} data-read-aloud-active={isActive(block)} style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
          {index === 0 ? before : searchGap(group[index - 1].end, block.start)}{children(block)}
        </li>))
    }
    if (first.kind === 'heading') {
      const level = Math.max(1, Math.min(6, first.level ?? 1))
      return createElement(`h${level}`, { key, 'data-read-aloud-active': active, style: { ...style, lineHeight: 1.4 } }, before, children(first))
    }
    if (first.kind === 'blockquote') return <blockquote key={key} data-read-aloud-active={active} style={{ ...style, borderInlineStart: `3px solid ${token.colorBorder}`, paddingInlineStart: '1em', marginInline: 0 }}>{before}{children(first)}</blockquote>
    return <p key={key} data-read-aloud-active={active} style={{ ...style, textIndent: '2em' }}>{before}{children(first)}</p>
  })}{searchGap(blocks.length ? blocks[blocks.length - 1].end : 0, points.length)}</div>
}
