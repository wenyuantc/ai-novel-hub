import { Alert, Button, Spin, Typography } from 'antd'
import { useCallback, useEffect, useRef, type ReactNode } from 'react'
import { mentionApi, type MentionAnchor, type MentionSpan } from '../../api/mentions'
import { useMentionQuery } from '../../hooks/useMentionQuery'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { MENTION_DICTIONARY_RESOURCES } from '../../utils/mentions'
import MentionText from './MentionText'

export default function MentionPreview({ projectId, text, anchor, draft = false, onLocated, renderText }: { projectId: number; text: string; anchor?: MentionAnchor | null; draft?: boolean; onLocated?: () => void; renderText?: (spans: MentionSpan[], focus: MentionAnchor | null) => ReactNode }) {
  const load = useCallback((signal: AbortSignal) => mentionApi.preview(projectId, text, signal), [projectId, text])
  const result = useMentionQuery(projectId, text, load, true, draft ? 300 : 0)
  useProjectRefresh(projectId, MENTION_DICTIONARY_RESOURCES, result.reload)
  const ref = useRef<HTMLDivElement>(null)
  const valid = !!anchor && !!result.data && anchor.content_hash === result.data.content_hash
  useEffect(() => {
    if (!valid) return
    const node = ref.current?.querySelector<HTMLElement>('[data-mention-focus]')
    node?.scrollIntoView({ block: 'center' })
    node?.focus({ preventScroll: true })
    onLocated?.()
  }, [valid, anchor?.start, anchor?.end, result.data, onLocated])
  return <div ref={ref}>
    {draft && <Typography.Paragraph type="secondary">当前草稿的提及标记；保存后计入全书统计。</Typography.Paragraph>}
    {result.loading && <span role="status"><Spin size="small" /> 正在识别提及…</span>}
    {result.error && <Alert type="error" showIcon message={result.error} action={<Button onClick={result.reload}>重试</Button>} />}
    {anchor && result.data && !valid && <Alert type="warning" showIcon message="原文已变化，请重新查看提及列表定位。" />}
    {renderText ? renderText(result.data?.spans ?? [], valid ? anchor : null) : <div style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', lineHeight: 'inherit' }}>
      {result.data ? <MentionText projectId={projectId} text={text} spans={result.data.spans} focus={valid ? anchor : null} /> : text}
    </div>}
  </div>
}
