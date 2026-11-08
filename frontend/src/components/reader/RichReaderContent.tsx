import { theme } from 'antd'
import { createElement, Fragment, useMemo, type CSSProperties, type ReactNode } from 'react'
import type { MentionAnchor, MentionSpan } from '../../api/mentions'
import type { ReaderBlock, ReaderRun } from '../../api/reader'
import MentionText from '../mentions/MentionText'

interface Props {
  projectId: number
  text: string
  blocks: ReaderBlock[]
  mentions?: { spans: MentionSpan[]; focus: MentionAnchor | null }
}

function firstEndingAfter(items: { end: number }[], start: number): number {
  let lo = 0, hi = items.length
  while (lo < hi) {
    const mid = (lo + hi) >>> 1
    if (items[mid].end <= start) lo = mid + 1
    else hi = mid
  }
  return lo
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

export default function RichReaderContent({ projectId, text, blocks, mentions }: Props) {
  const { token } = theme.useToken()
  const points = useMemo(() => Array.from(text), [text])
  const grouped = useMemo(() => groups(points, blocks), [points, blocks])
  // 提及 API 按原文顺序返回不重叠范围；每块二分起点，避免长章中反复扫描全书匹配。
  const mentionSpans = mentions?.spans ?? []
  const style: CSSProperties = { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', margin: '0 0 0.6em' }
  const children = (block: ReaderBlock) => {
    if (!mentions) return styledText(points, block.runs, block.start, block.end)
    const spans = []
    for (let i = firstEndingAfter(mentionSpans, block.start); i < mentionSpans.length && mentionSpans[i].start < block.end; i += 1) {
      const span = mentionSpans[i]
      spans.push({ ...span, start: Math.max(span.start, block.start), end: Math.min(span.end, block.end) })
    }
    const focus = mentions.focus && mentions.focus.start < block.end && mentions.focus.end > block.start
      ? { start: Math.max(mentions.focus.start, block.start), end: Math.min(mentions.focus.end, block.end) }
      : null
    return <MentionText projectId={projectId} text={points.slice(block.start, block.end).join('')} offset={block.start}
      spans={spans} focus={focus} renderPart={(start, end) => styledText(points, block.runs, start, end)} />
  }
  return <div>{grouped.map((group, i) => {
    const first = group[0], key = `${first.start}:${first.end}:${i}`
    if (first.kind === 'bullet_list' || first.kind === 'ordered_list') {
      const tag = first.kind === 'bullet_list' ? 'ul' : 'ol'
      return createElement(tag, { key, style: { margin: '0 0 0.6em', paddingInlineStart: '2em' } },
        group.map((block, index) => <li key={`${block.start}:${index}`} style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{children(block)}</li>))
    }
    if (first.kind === 'heading') {
      const level = Math.max(1, Math.min(6, first.level ?? 1))
      return createElement(`h${level}`, { key, style: { ...style, lineHeight: 1.4 } }, children(first))
    }
    if (first.kind === 'blockquote') return <blockquote key={key} style={{ ...style, borderInlineStart: `3px solid ${token.colorBorder}`, paddingInlineStart: '1em', marginInline: 0 }}>{children(first)}</blockquote>
    return <p key={key} style={{ ...style, textIndent: '2em' }}>{children(first)}</p>
  })}</div>
}
