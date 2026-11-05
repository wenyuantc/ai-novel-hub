import { Alert, Button, Checkbox, Input, Modal, Radio, Space, Typography } from 'antd'
import { useState } from 'react'
import { ApiError } from '../../api/client'
import { planningApi } from '../../api/planning'
import type { PlanningNodeResolution } from '../../api/types'
import { useModalWidth } from '../../hooks/useIsMobile'
import { readPlanningConflict } from '../../utils/planning'
import type { PlanningMergeConflict } from '../../utils/planning'
import { usePlanningMutation } from './usePlanningMutation'

interface Props { projectId: number; conflict: PlanningMergeConflict; onClose: () => void; onSaved: (revision: number) => void }
/** 对每一个会丢失的单值显式做选择；集合与事件由后端原子合并。 */
export default function PlanningMergeModal({ projectId, conflict: initial, onClose, onSaved }: Props) {
  const [conflict, setConflict] = useState(initial)
  const [pov, setPov] = useState<number | null | undefined>(initial.povIds[0] === initial.povIds[1] ? initial.povIds[0] : undefined)
  const [resolutions, setResolutions] = useState<Record<number, PlanningNodeResolution>>({})
  const [confirmed, setConfirmed] = useState(false)
  const mutation = usePlanningMutation(projectId, onSaved)
  const width = useModalWidth(760)
  const blocked = mutation.failure instanceof ApiError && [0, 409].includes(mutation.failure.status)
  const other = conflict.unitIds.find((id) => id !== conflict.survivorUnitId)!
  const save = async () => {
    if (pov === undefined || !confirmed || conflict.nodeConflicts.some((item) => !resolutions[item.subplot_id]?.content.trim() || !item.nodes.some((node) => node.id === resolutions[item.subplot_id]?.keep_node_id))) { mutation.setError('请明确选择 POV、解决每条节点冲突并确认保留位置'); return }
    const result = await mutation.run(() => planningApi.merge(projectId, { expected_revision: conflict.revision, survivor_unit_id: conflict.survivorUnitId, other_unit_id: other, pov_character_id: pov, node_resolutions: conflict.nodeConflicts.map(({ subplot_id }) => ({ ...resolutions[subplot_id], content: resolutions[subplot_id].content.trim() })) }))
    if (result) onClose()
  }
  const refreshConflict = readPlanningConflict(mutation.failure)
  return <Modal open title="解决规划合并冲突" width={width} maskClosable={false} onCancel={() => { if (!mutation.saving) onClose() }} footer={<Space><Button disabled={mutation.saving} onClick={onClose}>取消</Button><Button type="primary" disabled={!mutation.canEdit || blocked || !confirmed} loading={mutation.saving} onClick={() => void save()}>确认合并规划</Button></Space>}>
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Alert type="warning" showIcon message="两边都有作者规划，尚未进行任何覆盖" description="标签与设定关联取并集，独立交汇事件保留；请选择最终 POV，并逐项确认重复支线的推进内容。合并后可以重试建章或重新关联。" />
      <div><Typography.Text strong>保留位置：</Typography.Text>{conflict.labels[conflict.survivorUnitId] ?? `规划 #${conflict.survivorUnitId}`}<br /><Typography.Text type="secondary">并入：{conflict.labels[other] ?? `规划 #${other}`}</Typography.Text></div>
      <div><Typography.Text strong>合并后的 POV</Typography.Text><Radio.Group aria-label="合并后的POV" value={pov === null ? 0 : pov} onChange={(event) => setPov(event.target.value === 0 ? null : Number(event.target.value))} disabled={mutation.saving}>
        <Space wrap>{[...new Set([0, ...conflict.povIds.filter((id): id is number => id !== null)])].map((id) => <Radio value={id} key={id}>{id === 0 ? '不指定视角' : conflict.povNames[id] ?? `角色 #${id}`}</Radio>)}</Space>
      </Radio.Group></div>
      {conflict.nodeConflicts.map((item) => <div key={item.subplot_id}>
        <Typography.Text strong>{item.subplot_name ?? `支线 #${item.subplot_id}`} 的重复节点</Typography.Text>
        <Radio.Group aria-label={`支线${item.subplot_id}保留节点`} value={resolutions[item.subplot_id]?.keep_node_id} disabled={mutation.saving} onChange={(event) => {
          const chosen = item.nodes.find((node) => node.id === event.target.value)
          if (chosen) setResolutions((old) => ({ ...old, [item.subplot_id]: { subplot_id: item.subplot_id, keep_node_id: chosen.id, content: chosen.content } }))
        }}><Space direction="vertical">{item.nodes.map((node) => <Radio key={node.id} value={node.id}>保留节点 #{node.id}：{node.content}</Radio>)}</Space></Radio.Group>
        {resolutions[item.subplot_id] && <Input.TextArea aria-label={`支线${item.subplot_id}合并文本`} value={resolutions[item.subplot_id].content} rows={4} maxLength={2000} disabled={mutation.saving} onChange={(event) => setResolutions((old) => ({ ...old, [item.subplot_id]: { ...old[item.subplot_id], content: event.target.value } }))} />}
      </div>)}
      <Checkbox checked={confirmed} disabled={mutation.saving} onChange={(event) => setConfirmed(event.target.checked)}>已核对保留位置及全部合并内容</Checkbox>
      {mutation.error && <Alert type="error" showIcon message={mutation.error} description={blocked ? '数据在确认期间再次变化，请核对最新冲突或返回刷新后重试。原填写内容仍保留。' : undefined} />}
      {refreshConflict && <Button onClick={() => { setConflict(refreshConflict); setConfirmed(false); setPov(undefined); mutation.clearError() }}>加载这次返回的最新冲突</Button>}
    </Space>
  </Modal>
}
