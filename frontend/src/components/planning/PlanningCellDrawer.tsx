import { Alert, Button, Divider, Drawer, Space, Tag, Typography } from 'antd'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError } from '../../api/client'
import { planningApi } from '../../api/planning'
import type { MatrixCell, MatrixColumn, PlanningReference, PlanningReferenceOperation, PlanningRow, SubplotCrossing } from '../../api/types'
import { useModalWidth } from '../../hooks/useIsMobile'
import { planningRowLabel, settingCellDescription } from '../../utils/planning'
import EntityMentions from '../mentions/EntityMentions'
import PlanningRevisionReview from './PlanningRevisionReview'
import { usePlanningMutation } from './usePlanningMutation'

interface Props {
  projectId: number; row: PlanningRow; column: MatrixColumn; cell: MatrixCell; revision: number; onClose: () => void
  onSaved: (revision: number) => void; onNode: (row: PlanningRow, subplotId: number) => void
  onCrossing: (event: SubplotCrossing) => void; onCreateCrossing: (row: PlanningRow, subplotIds: number[]) => void
}
function entryLink(pid: number, reference: PlanningReference) {
  if (reference.kind === 'relationship') return `/projects/${pid}/relationships?timeline_id=${reference.timeline_id}`
  const page = { character: 'characters', world: 'world', organization: 'organizations', career: 'careers' }[reference.kind]
  return `/projects/${pid}/${page}?id=${reference.id}`
}
export default function PlanningCellDrawer({ projectId, row, column, cell, revision: initialRevision, onClose, onSaved, onNode, onCrossing, onCreateCrossing }: Props) {
  const [revision, setRevision] = useState(initialRevision)
  const mutation = usePlanningMutation(projectId, onSaved)
  const width = useModalWidth(640)
  const blocked = mutation.failure instanceof ApiError && [0, 409].includes(mutation.failure.status)
  const changeManual = async (reference: PlanningReference, remove: boolean) => {
    const op = remove ? 'remove' : 'add'
    const operation: PlanningReferenceOperation = reference.kind === 'relationship' ? { kind: reference.kind, op, timeline_ids: [reference.timeline_id] } : { kind: reference.kind, op, ids: [reference.id] }
    const result = await mutation.run(() => planningApi.batch(projectId, { expected_revision: revision, targets: [row.source], references: [operation] }))
    if (result) onClose()
  }
  return <Drawer open title={`${column.name} · ${planningRowLabel(row)}`} width={width} onClose={() => { if (!mutation.saving) onClose() }}>
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {column.selection.kind === 'setting' && cell.kind === 'setting' && <>
        <Typography.Paragraph>{settingCellDescription(cell)}</Typography.Paragraph>
        <Space wrap><Link to={entryLink(projectId, column.selection.reference)}><Button>打开设定详情</Button></Link>
          <Button type="primary" disabled={!mutation.canEdit || blocked} loading={mutation.saving} onClick={() => column.selection.kind === 'setting' && void changeManual(column.selection.reference, cell.manual)}>{cell.manual ? '移除手动关联' : '加入手动关联'}</Button>
        </Space>
        <Typography.Text type="secondary">手动规划与正文提及独立。移除手动关联不会修改正文，也不会清除自动提及。</Typography.Text>
        {row.chapter_no !== null && cell.tracking_supported && (column.selection.reference.kind === 'character' || column.selection.reference.kind === 'world') && <><Divider>提及来源</Divider><EntityMentions projectId={projectId} entity={{ kind: column.selection.reference.kind, id: column.selection.reference.id }} chapterFrom={row.chapter_no ?? undefined} chapterTo={row.chapter_no ?? undefined} sourceType="chapter" /></>}
      </>}
      {column.selection.kind === 'subplot' && cell.kind === 'subplot' && <>
        <Typography.Title level={4}>本章推进目标</Typography.Title>
        <Typography.Paragraph style={{ whiteSpace: 'pre-wrap' }}>{cell.node?.content || '尚未安排普通推进节点'}</Typography.Paragraph>
        <Button disabled={!mutation.canEdit} onClick={() => { if (column.selection.kind === 'subplot') onNode(row, column.selection.subplot_id) }}>编辑推进节点</Button>
        <Divider>明确交汇</Divider>
        {cell.crossings.length ? cell.crossings.map((event) => <div key={event.id}><Button type="link" onClick={() => onCrossing(event)}>◆ {event.title}</Button><Tag>{event.subplot_ids.length} 条参与支线</Tag><Typography.Paragraph style={{ whiteSpace: 'pre-wrap' }}>{event.description}</Typography.Paragraph></div>) : <Typography.Text type="secondary">尚无明确交汇。多条支线在本章推进不会自动产生交汇事件。</Typography.Text>}
        <Button disabled={!mutation.canEdit} onClick={() => { if (column.selection.kind === 'subplot') onCreateCrossing(row, [column.selection.subplot_id]) }}>记录明确交汇</Button>
      </>}
      {mutation.error && <Alert type="error" showIcon message={mutation.error} />}
      <PlanningRevisionReview projectId={projectId} failure={mutation.failure} targets={[row.source]} originalRows={[row]} onAccept={(next) => { setRevision(next); mutation.clearError() }} />
    </Space>
  </Drawer>
}
