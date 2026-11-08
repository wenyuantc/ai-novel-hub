import { Button, Popover, Space, Typography, theme } from 'antd'
import { Fragment, useMemo, useState, type ReactNode } from 'react'
import { MENTION_CATEGORY_LABELS, type MentionEntity, type MentionSpan } from '../../api/mentions'
import { mentionSegments } from '../../utils/mentions'
import EntityPreview from './EntityPreview'

interface Props { projectId: number; text: string; spans: MentionSpan[]; offset?: number; focus?: { start: number; end: number } | null; renderPart?: (start: number, end: number, text: string) => ReactNode }
export default function MentionText({ projectId, text, spans, offset = 0, focus, renderPart }: Props) {
  const { token } = theme.useToken()
  const [selected, setSelected] = useState<{ pid: number; entity: MentionEntity } | null>(null)
  const segments = useMemo(() => mentionSegments(text, spans, offset, focus), [text, spans, offset, focus])
  return <>
    {segments.map((part) => {
      const content = renderPart ? renderPart(part.start, part.end, part.text) : part.text
      const style = { font: 'inherit', color: 'inherit', background: part.focused ? token.colorWarningBg : 'transparent', border: 0, padding: 0, textAlign: 'inherit' as const, whiteSpace: 'inherit' as const, cursor: 'pointer', textDecorationLine: 'underline', textDecorationColor: part.entities.length > 1 ? token.colorWarning : token.colorPrimary, textDecorationStyle: part.entities.length > 1 ? 'dotted' as const : 'solid' as const, textUnderlineOffset: 3 }
      if (part.entities.length === 0) return part.focused ? <mark key={part.start} data-mention-focus style={{ background: token.colorWarningBg, color: 'inherit' }}>{content}</mark> : <Fragment key={part.start}>{content}</Fragment>
      const control = <button type="button" data-mention-focus={part.focused || undefined} data-mention-start={part.start} style={style} aria-label={`${part.text}：${part.entities.length > 1 ? '选择对应设定' : '查看设定'}`} onClick={part.entities.length === 1 ? () => setSelected({ pid: projectId, entity: part.entities[0] }) : undefined}>{content}</button>
      return part.entities.length === 1 ? <Fragment key={part.start}>{control}</Fragment> : <Popover key={part.start} trigger="click" title="此称呼对应多个条目" content={<Space direction="vertical"><Typography.Text type="secondary">尚不能确定归属，请选择查看：</Typography.Text>{part.entities.map((entity) => <Button key={`${entity.kind}:${entity.id}`} type="link" onClick={() => setSelected({ pid: projectId, entity })}>{entity.name}（{MENTION_CATEGORY_LABELS[entity.category]}）</Button>)}</Space>}>{control}</Popover>
    })}
    {selected?.pid === projectId && <EntityPreview key={`${projectId}:${selected.entity.kind}:${selected.entity.id}`} projectId={projectId} entity={selected.entity} onClose={() => setSelected(null)} />}
  </>
}
