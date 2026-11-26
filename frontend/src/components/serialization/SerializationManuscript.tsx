import { Typography, theme } from 'antd'
import { useEffect, useRef } from 'react'
import type { ReaderBlock } from '../../api/reader'
import type { PreflightHit } from '../../api/serialization'
import MentionText from '../mentions/MentionText'
import RichReaderContent from '../reader/RichReaderContent'
import { manuscriptTitle } from './serializationForm'

export interface Manuscript { chapter_no: number; title: string; content: string; blocks: ReaderBlock[] | null }

export default function SerializationManuscript({ projectId, manuscript, hit }: {
  projectId: number; manuscript: Manuscript; hit?: PreflightHit
}) {
  const { token } = theme.useToken()
  const preview = useRef<HTMLElement>(null)
  useEffect(() => {
    if (!hit) return
    const mark = preview.current?.querySelector<HTMLElement>('[data-mention-focus]')
    mark?.scrollIntoView({ block: 'center' })
    mark?.focus({ preventScroll: true })
  }, [hit])
  const bodyHit = hit?.field === 'content' ? hit : undefined
  return <section ref={preview} aria-label="正文预览" style={{ padding: 16, border: `1px solid ${token.colorBorderSecondary}`,
    borderRadius: token.borderRadiusLG, maxHeight: '45vh', overflow: 'auto', overflowWrap: 'anywhere' }}>
    <Typography.Title level={5} style={{ marginTop: 0 }}>{hit?.field === 'title'
      ? <>第 {manuscript.chapter_no} 章 · <MentionText projectId={projectId} text={manuscript.title} spans={[]} focus={hit} /></>
      : manuscriptTitle(manuscript)}</Typography.Title>
    {manuscript.blocks?.length
      ? <RichReaderContent projectId={projectId} text={manuscript.content} blocks={manuscript.blocks}
        mentions={bodyHit ? { spans: [], focus: { ...bodyHit, origin: 'preflight' } } : undefined} />
      : <div style={{ whiteSpace: 'pre-wrap', lineHeight: 1.85 }}>{bodyHit
        ? <MentionText projectId={projectId} text={manuscript.content} spans={[]} focus={bodyHit} />
        : manuscript.content || '正文为空'}</div>}
  </section>
}
