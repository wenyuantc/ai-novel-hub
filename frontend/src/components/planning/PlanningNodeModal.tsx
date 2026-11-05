import { Alert, Button, Input, Modal, Space, Spin, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { ApiError } from '../../api/client'
import { planningApi } from '../../api/planning'
import type { PlanningRow, SubplotNode } from '../../api/types'
import { useModalWidth } from '../../hooks/useIsMobile'
import { useMentionQuery as usePlanningQuery } from '../../hooks/useMentionQuery'
import { planningRowLabel } from '../../utils/planning'
import { PlanningSubplotSelect } from './PlanningFields'
import PlanningRevisionReview from './PlanningRevisionReview'
import { usePlanningMutation } from './usePlanningMutation'

interface Props { projectId: number; row: PlanningRow; subplotId?: number; onClose: () => void; onSaved: (revision: number) => void }
export default function PlanningNodeModal({ projectId, row, subplotId, onClose, onSaved }: Props) {
  const [busy, setBusy] = useState(false)
  const [selected, setSelected] = useState(subplotId)
  const [confirmed, setConfirmed] = useState(!!subplotId)
  const width = useModalWidth(680)
  return <Modal open title={`支线推进 · ${planningRowLabel(row)}`} width={width} footer={null} closable={!busy} keyboard={!busy} onCancel={() => { if (!busy) onClose() }} maskClosable={false}>
    {!confirmed ? <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <PlanningSubplotSelect projectId={projectId} single label="选择要推进的支线" value={selected ? [selected] : []} onChange={(ids) => setSelected(ids[0])} />
      <Button type="primary" disabled={!selected} onClick={() => setConfirmed(true)}>查看并编辑该支线节点</Button>
    </Space> : selected && <NodeLoader projectId={projectId} row={row} subplotId={selected} onBusyChange={setBusy} onClose={onClose} onSaved={onSaved} />}
  </Modal>
}
interface NodeProps extends Required<Props> { onBusyChange: (busy: boolean) => void }
function NodeLoader({ projectId, row, subplotId, onClose, onSaved, onBusyChange }: NodeProps) {
  const load = useCallback((signal: AbortSignal) => planningApi.matrix(projectId, { source_refs: [row.source], column_mode: 'custom', column_refs: [{ kind: 'subplot', subplot_id: subplotId }], page_size: 1 }, signal), [projectId, row.source, subplotId])
  const result = usePlanningQuery(projectId, `node:${row.key}:${subplotId}`, load)
  if (result.loading) return <Spin />
  if (result.error) return <Alert type="error" message={result.error} action={<Button onClick={result.reload}>重试</Button>} />
  const current = result.data?.rows[0]
  if (!current || !result.data) return <Alert type="warning" message="目标位置已不存在，请返回后重新选择" />
  const cell = result.data.cells.find((item) => item.kind === 'subplot')
  const node = cell?.kind === 'subplot' ? cell.node : null
  return <NodeEditor projectId={projectId} row={current} subplotId={subplotId} node={node} revision={result.data.revision} onBusyChange={onBusyChange} onClose={onClose} onSaved={onSaved} />
}
function NodeEditor({ projectId, row, subplotId, node, revision: initialRevision, onClose, onSaved, onBusyChange }: NodeProps & { node: SubplotNode | null; revision: number }) {
  const [revision, setRevision] = useState(initialRevision)
  const [content, setContent] = useState(node?.content ?? '')
  const mutation = usePlanningMutation(projectId, onSaved)
  const blocked = mutation.failure instanceof ApiError && [0, 409].includes(mutation.failure.status)
  const save = async () => {
    if (!content.trim()) { mutation.setError('请填写本章的推进目标'); return }
    onBusyChange(true)
    const result = await mutation.run(() => planningApi.putNode(projectId, subplotId, row.source, { expected_revision: revision, content: content.trim() }))
    onBusyChange(false)
    if (result) onClose()
  }
  const remove = async () => {
    onBusyChange(true)
    const result = await mutation.run(() => planningApi.deleteNode(projectId, subplotId, row.source, revision))
    onBusyChange(false)
    if (result) onClose()
  }
  const [confirmDelete, setConfirmDelete] = useState(false)
  return <Space direction="vertical" size={16} style={{ width: '100%' }}>
    <Alert type="info" showIcon message="本章推进目标" description="该文本会作为本章生成与重写的规划；它不是已经发生的剧情事实。" />
    <Input.TextArea aria-label="本章支线推进目标" value={content} onChange={(event) => setContent(event.target.value)} rows={7} maxLength={2000} showCount disabled={!mutation.canEdit || mutation.saving} placeholder="这个位置要如何推进该支线？" />
    {mutation.error && <Alert type="error" showIcon message={mutation.error} />}
    <PlanningRevisionReview projectId={projectId} failure={mutation.failure} subplotId={subplotId} targets={[row.source]} originalRows={[row]} onAccept={(next) => { setRevision(next); mutation.clearError() }} />
    {confirmDelete && <Alert type="warning" message="确认移除这个推进节点？" description="支线本身和同位置的交汇事件会保留。" action={<Button danger disabled={blocked} loading={mutation.saving} onClick={() => void remove()}>确认移除</Button>} />}
    <Space wrap>
      <Button type="primary" disabled={!mutation.canEdit || blocked} loading={mutation.saving} onClick={() => void save()}>保存推进目标</Button>
      {node && <Button danger disabled={!mutation.canEdit || mutation.saving || blocked} onClick={() => setConfirmDelete(true)}>移除节点</Button>}
      <Button disabled={mutation.saving} onClick={onClose}>关闭</Button>
    </Space>
    <Typography.Text type="secondary">支线 #{subplotId} · {planningRowLabel(row)}</Typography.Text>
  </Space>
}
