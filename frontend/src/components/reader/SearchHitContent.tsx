import { Alert } from 'antd'
import { useEffect, useRef } from 'react'
import { Link } from 'react-router-dom'
import type { MentionAnchor } from '../../api/mentions'
import type { ReaderBlock } from '../../api/reader'
import { staleAnchorMessage } from '../../utils/mentions'
import MentionText from '../mentions/MentionText'
import RichReaderContent from './RichReaderContent'

interface Props {
  projectId: number
  text: string
  contentHash: string
  blocks: ReaderBlock[] | null
  anchor: MentionAnchor
}

/** 章指纹由阅读接口随正文返回，定位普通用词也只需本次已鉴权的阅读快照。 */
export default function SearchHitContent({ projectId, text, contentHash, blocks, anchor }: Props) {
  const container = useRef<HTMLDivElement>(null)
  const focus = anchor.content_hash === contentHash && anchor.end <= Array.from(text).length ? anchor : null
  useEffect(() => {
    if (!focus) return
    const mark = container.current?.querySelector<HTMLElement>('[data-mention-focus]')
    mark?.scrollIntoView({ block: 'center' })
    mark?.focus({ preventScroll: true })
  }, [focus])
  return <div ref={container}>
    {!focus && <Alert type="warning" showIcon message={staleAnchorMessage(anchor, '搜索结果已失效。')}
      action={<Link to={`/projects/${projectId}/book-search`}>重新搜索</Link>} />}
    {blocks ? <RichReaderContent projectId={projectId} text={text} blocks={blocks} mentions={{ spans: [], focus }} />
      : <div style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
        <MentionText projectId={projectId} text={text} spans={[]} focus={focus} />
      </div>}
  </div>
}
