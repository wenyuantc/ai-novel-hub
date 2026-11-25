import { Fragment, useMemo } from 'react'
import { readerParagraphs, type ReaderParagraph } from '../../utils/reader'
import { ReaderMentionText, type ReaderMentions } from './ReaderMentionText'

interface Props {
  projectId: number
  text: string
  paragraphs?: ReaderParagraph[]
  activeStart?: number | null
  mentions?: ReaderMentions
}

/** 普通排版缩进分段。定位视图逐字保留段间空白，换行本身也可以是命中目标。 */
export default function PlainReaderContent({ projectId, text, paragraphs, activeStart, mentions }: Props) {
  const ranges = useMemo(() => paragraphs ?? readerParagraphs(text), [paragraphs, text])
  const preserveWhitespace = !!mentions
  const points = useMemo(() => preserveWhitespace ? Array.from(text) : [], [text, preserveWhitespace])
  if (!mentions) return ranges.map((p) => <p key={p.start} data-read-aloud-active={activeStart === p.start || undefined}
    style={{ textIndent: '2em', margin: '0 0 0.6em', wordBreak: 'break-word' }}>{p.text.trim()}</p>)
  const render = (start: number, end: number, part: string) => end > start &&
    <ReaderMentionText projectId={projectId} text={part} offset={start} end={end} mentions={mentions} />
  const gap = (start: number, end: number) => render(start, end, points.slice(start, end).join(''))
  return <div style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
    {ranges.map((p, i) => <Fragment key={p.start}>
      {gap(i ? ranges[i - 1].end : 0, p.start)}
      <span data-read-aloud-active={activeStart === p.start || undefined}>{render(p.start, p.end, p.text)}</span>
    </Fragment>)}
    {gap(ranges.length ? ranges[ranges.length - 1].end : 0, points.length)}
  </div>
}
