import { Alert, Button, Card, Spin, Typography } from 'antd'
import { useCallback, useEffect, useRef } from 'react'
import { mentionApi, MENTION_SOURCE_LABELS, type MentionAnchor, type MentionSourceType } from '../../api/mentions'
import { useMentionQuery } from '../../hooks/useMentionQuery'
import MentionText from './MentionText'

export default function SourceMentionFocus({ projectId, type, id, anchor }: { projectId: number; type: MentionSourceType; id: number; anchor: MentionAnchor }) {
  const { start, end, content_hash } = anchor
  const load = useCallback((signal: AbortSignal) => mentionApi.source(projectId, type, id, { start, end, content_hash }, signal), [projectId, type, id, start, end, content_hash])
  const result = useMentionQuery(projectId, `${type}:${id}:${start}:${end}:${content_hash}`, load)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => { if (result.data && !result.data.stale) ref.current?.querySelector('[data-mention-focus]')?.scrollIntoView({ block: 'center' }) }, [result.data])
  return <div ref={ref}><Card size="small" title={`${MENTION_SOURCE_LABELS[type]}原文定位`} style={{ marginBlock: 12 }}>
    {result.loading && <Spin />}
    {result.error && <Alert type="error" showIcon message={result.error} action={<Button onClick={result.reload}>重试</Button>} />}
    {result.data && <>
      <Typography.Paragraph strong>{result.data.source.title}</Typography.Paragraph>
      {result.data.stale && <Alert type="warning" showIcon message="原文已变化，旧位置已失效。请返回提及列表重新定位。" />}
      <div style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 400, overflow: 'auto', lineHeight: 1.8 }}><MentionText projectId={projectId} text={result.data.text} spans={result.data.spans} offset={result.data.window_start} focus={result.data.target_start !== null && result.data.target_end !== null ? { start: result.data.target_start, end: result.data.target_end } : null} /></div>
    </>}
  </Card></div>
}
