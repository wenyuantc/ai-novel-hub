import { firstEndingAfter } from '../../utils/mentions'
import type { ReactNode } from 'react'
import type { MentionAnchor, MentionSpan } from '../../api/mentions'
import MentionText from '../mentions/MentionText'

type ReaderFocus = MentionAnchor | { start: number; end: number; origin: 'preflight' }
export interface ReaderMentions { spans: MentionSpan[]; focus: ReaderFocus | null }

export function ReaderMentionText({ projectId, text, offset, end, mentions, renderPart }: {
  projectId: number
  text: string
  offset: number
  end: number
  mentions: ReaderMentions
  renderPart?: (start: number, end: number) => ReactNode
}) {
  const spans: MentionSpan[] = []
  for (let i = firstEndingAfter(mentions.spans, offset); i < mentions.spans.length && mentions.spans[i].start < end; i += 1) {
    const span = mentions.spans[i]
    spans.push({ ...span, start: Math.max(offset, span.start), end: Math.min(end, span.end) })
  }
  const focus = mentions.focus && mentions.focus.end > offset && mentions.focus.start < end ? mentions.focus : null
  if (!spans.length && !focus) return renderPart ? renderPart(offset, end) : text
  return <MentionText projectId={projectId} text={text} offset={offset} spans={spans} focus={focus} renderPart={renderPart} />
}
