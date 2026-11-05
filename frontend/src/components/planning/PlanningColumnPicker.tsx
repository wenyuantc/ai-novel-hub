import { Alert, Button, Modal, Space, Typography } from 'antd'
import { useState } from 'react'
import type { MatrixColumnRef, PlanningReferenceKind } from '../../api/types'
import { useModalWidth } from '../../hooks/useIsMobile'
import { PLANNING_REFERENCE_LABELS, planningReferenceId } from '../../utils/planning'
import { PlanningEntrySelect, PlanningSubplotSelect } from './PlanningFields'

interface Props { projectId: number; value: MatrixColumnRef[]; onClose: () => void; onApply: (value: MatrixColumnRef[]) => void }
export default function PlanningColumnPicker({ projectId, value, onClose, onApply }: Props) {
  const [references, setReferences] = useState<Partial<Record<PlanningReferenceKind, number[]>>>(() => {
    const result: Partial<Record<PlanningReferenceKind, number[]>> = {}
    for (const column of value) if (column.kind === 'setting') result[column.reference.kind] = [...(result[column.reference.kind] ?? []), planningReferenceId(column.reference)]
    return result
  })
  const [subplots, setSubplots] = useState(value.flatMap((column) => column.kind === 'subplot' ? [column.subplot_id] : []))
  const [error, setError] = useState('')
  const width = useModalWidth(680)
  const apply = () => {
    const columns: MatrixColumnRef[] = []
    for (const kind of Object.keys(PLANNING_REFERENCE_LABELS) as PlanningReferenceKind[]) for (const id of references[kind] ?? []) columns.push({ kind: 'setting', reference: kind === 'relationship' ? { kind, timeline_id: id } : { kind, id } })
    for (const id of subplots) columns.push({ kind: 'subplot', subplot_id: id })
    if (columns.length > 30) { setError('自选视图最多同时展示 30 列，请减少所选条目'); return }
    onApply(columns); onClose()
  }
  return <Modal open title="选择矩阵展示列" width={width} onCancel={onClose} footer={<Space><Button onClick={onClose}>取消</Button><Button type="primary" onClick={apply}>应用视图</Button></Space>}>
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Typography.Text type="secondary">可把不同设定与支线放在同一矩阵。这里仅调整展示列，不改变任何章节关联。</Typography.Text>
      {(Object.keys(PLANNING_REFERENCE_LABELS) as PlanningReferenceKind[]).map((kind) => <div key={kind}><Typography.Text strong>{PLANNING_REFERENCE_LABELS[kind]}</Typography.Text><PlanningEntrySelect projectId={projectId} kind={kind} label={`展示${PLANNING_REFERENCE_LABELS[kind]}`} value={references[kind] ?? []} onChange={(ids) => setReferences((old) => ({ ...old, [kind]: ids }))} /></div>)}
      <div><Typography.Text strong>支线</Typography.Text><PlanningSubplotSelect projectId={projectId} value={subplots} onChange={setSubplots} /></div>
      {error && <Alert type="error" message={error} />}
    </Space>
  </Modal>
}
