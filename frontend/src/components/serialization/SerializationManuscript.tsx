import { Typography, theme } from 'antd'
import type { ReaderBlock } from '../../api/reader'
import { manuscriptTitle } from './serializationForm'
import RichReaderContent from '../reader/RichReaderContent'

export interface Manuscript { chapter_no: number; title: string; content: string; blocks: ReaderBlock[] | null }

export default function SerializationManuscript({ projectId, manuscript }: { projectId: number; manuscript: Manuscript }) {
  const { token } = theme.useToken()
  return <section aria-label="正文预览" style={{ padding: 16, border: `1px solid ${token.colorBorderSecondary}`,
    borderRadius: token.borderRadiusLG, maxHeight: '45vh', overflow: 'auto', overflowWrap: 'anywhere' }}>
    <Typography.Title level={5} style={{ marginTop: 0 }}>{manuscriptTitle(manuscript)}</Typography.Title>
    {manuscript.blocks?.length
      ? <RichReaderContent projectId={projectId} text={manuscript.content} blocks={manuscript.blocks} />
      : <div style={{ whiteSpace: 'pre-wrap', lineHeight: 1.85 }}>{manuscript.content || '正文为空'}</div>}
  </section>
}
