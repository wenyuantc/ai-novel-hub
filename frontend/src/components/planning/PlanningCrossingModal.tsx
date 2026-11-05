import { Alert, Button, Form, Input, Modal, Space, Typography } from 'antd'
import { useState } from 'react'
import { ApiError } from '../../api/client'
import { planningApi } from '../../api/planning'
import type { PlanningRow, PlanningSource, SubplotCrossing } from '../../api/types'
import { useModalWidth } from '../../hooks/useIsMobile'
import { planningSourceKey } from '../../utils/planning'
import { PlanningSourceSelect, PlanningSubplotSelect } from './PlanningFields'
import PlanningRevisionReview from './PlanningRevisionReview'
import { usePlanningMutation } from './usePlanningMutation'

interface Props { projectId: number; revision: number; row?: PlanningRow; crossing?: SubplotCrossing; initialSubplotIds?: number[]; onClose: () => void; onSaved: (revision: number) => void }
/** 共享交汇独立维护完整参与集合，不从当前矩阵显示的支线推断。 */
export default function PlanningCrossingModal({ projectId, revision: initialRevision, row, crossing, initialSubplotIds = [], onClose, onSaved }: Props) {
  const [revision, setRevision] = useState(initialRevision)
  const [source, setSource] = useState<PlanningSource | undefined>(crossing?.source ?? row?.source)
  const [title, setTitle] = useState(crossing?.title ?? '')
  const [description, setDescription] = useState(crossing?.description ?? '')
  const [subplotIds, setSubplotIds] = useState(crossing?.subplot_ids ?? initialSubplotIds)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const mutation = usePlanningMutation(projectId, onSaved)
  const width = useModalWidth(760)
  const blocked = mutation.failure instanceof ApiError && [0, 409].includes(mutation.failure.status)
  const save = async () => {
    if (!source || !title.trim() || !description.trim()) { mutation.setError('请选择交汇位置并填写标题和说明'); return }
    if (subplotIds.length < 2 || subplotIds.length > 20) { mutation.setError('请选择 2–20 条参与支线'); return }
    const body = { expected_revision: revision, source, title: title.trim(), description: description.trim(), subplot_ids: subplotIds }
    const result = await mutation.run(() => crossing ? planningApi.updateCrossing(projectId, crossing.id, body) : planningApi.createCrossing(projectId, body))
    if (result) onClose()
  }
  const remove = async () => {
    if (!crossing) return
    const result = await mutation.run(() => planningApi.deleteCrossing(projectId, crossing.id, revision))
    if (result) onClose()
  }
  const targets = [source, crossing?.source].filter((item): item is PlanningSource => !!item).filter((item, index, list) => list.findIndex((other) => planningSourceKey(other) === planningSourceKey(item)) === index)
  return <Modal open title={crossing ? '编辑明确交汇事件' : '新建明确交汇事件'} width={width} maskClosable={false} onCancel={() => { if (!mutation.saving) onClose() }} footer={<Space>
    <Button disabled={mutation.saving} onClick={onClose}>取消</Button><Button type="primary" loading={mutation.saving} disabled={!mutation.canEdit || blocked} onClick={() => void save()}>保存交汇</Button>
  </Space>}>
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Alert type="info" showIcon message="记录剧情上的明确交汇" description="至少两条支线共同参与。事件将作为所选章节的规划目标传入 AI，每个事件只出现一次；仅同章推进不会自动变成交汇。" />
      <Form layout="vertical" disabled={!mutation.canEdit || mutation.saving}>
        <Form.Item label="交汇位置" required><PlanningSourceSelect projectId={projectId} value={source} onChange={setSource} initialRow={row} disabled={!mutation.canEdit || mutation.saving} /></Form.Item>
        <Form.Item label="事件标题" required><Input aria-label="交汇事件标题" value={title} onChange={(event) => setTitle(event.target.value)} maxLength={100} /></Form.Item>
        <Form.Item label="参与支线（2–20 条）" required><PlanningSubplotSelect projectId={projectId} value={subplotIds} onChange={setSubplotIds} disabled={!mutation.canEdit || mutation.saving} /></Form.Item>
        <Form.Item label="本章交汇说明" required><Input.TextArea aria-label="本章交汇说明" rows={6} value={description} onChange={(event) => setDescription(event.target.value)} maxLength={2000} showCount /></Form.Item>
      </Form>
      <Typography.Text type="secondary">事件可以独立存在，不要求先创建每条支线的普通推进节点。</Typography.Text>
      {mutation.error && <Alert type="error" showIcon message={mutation.error} />}
      <PlanningRevisionReview projectId={projectId} failure={mutation.failure} resource={{ kind: 'crossings', id: crossing?.id }} targets={targets} onAccept={(next) => { setRevision(next); mutation.clearError() }} />
      {crossing && <Button danger disabled={!mutation.canEdit || mutation.saving || blocked} onClick={() => setConfirmDelete(true)}>删除交汇事件</Button>}
      {confirmDelete && <Alert type="warning" message="删除事件后，参与支线及其普通节点仍保留。" action={<Button danger loading={mutation.saving} disabled={blocked} onClick={() => void remove()}>确认删除</Button>} />}
    </Space>
  </Modal>
}
