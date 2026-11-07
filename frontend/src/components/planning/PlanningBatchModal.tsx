import { Alert, Button, Divider, Modal, Select, Space, Tag, Typography } from 'antd'
import { useState } from 'react'
import { ApiError } from '../../api/client'
import { planningApi } from '../../api/planning'
import type { PlanningReferenceKind, PlanningRow } from '../../api/types'
import { useModalWidth } from '../../hooks/useIsMobile'
import { batchPayload, emptyBatchDraft, PLANNING_OP_LABELS, PLANNING_REFERENCE_LABELS, planningRowLabel } from '../../utils/planning'
import type { CollectionDraft, PlanningBatchDraft } from '../../utils/planning'
import { PlanningEntrySelect, PlanningTagSelect } from './PlanningFields'
import PlanningRevisionReview from './PlanningRevisionReview'
import { usePlanningMutation } from './usePlanningMutation'

interface Props { projectId: number; rows: PlanningRow[]; revision: number; onClose: () => void; onSaved: (revision: number) => void }
/** 明确字段与集合操作的批量编辑，未选择的类型不会被写入。 */
export default function PlanningBatchModal({ projectId, rows, revision: initialRevision, onClose, onSaved }: Props) {
  const [revision, setRevision] = useState(initialRevision)
  const [draft, setDraft] = useState<PlanningBatchDraft>(emptyBatchDraft)
  const [currentRows, setCurrentRows] = useState(rows)
  const sceneTargets = rows.every((row) => row.source.kind === 'scene')
  const operations = Object.entries(PLANNING_OP_LABELS).filter(([value]) => value !== 'inherit' || sceneTargets).map(([value, label]) => ({ value, label }))
  const mutation = usePlanningMutation(projectId, onSaved)
  const width = useModalWidth(780)
  const conflict = mutation.failure instanceof ApiError && [0, 409].includes(mutation.failure.status)
  const setReference = (kind: PlanningReferenceKind, value: CollectionDraft) => setDraft((old) => ({ ...old, references: { ...old.references, [kind]: value } }))
  const save = async () => {
    try {
      const body = batchPayload(revision, rows.map((row) => row.source), draft, currentRows)
      const result = await mutation.run(() => planningApi.batch(projectId, body))
      if (result) onClose()
    } catch (err) { mutation.setError(err instanceof Error ? err.message : '请检查填写内容') }
  }
  return <Modal open title={rows.length === 1 ? sceneTargets ? '编辑场景规划覆盖' : '编辑章节规划' : `批量调整 ${rows.length} 个规划位置`} width={width} onCancel={() => { if (!mutation.saving) onClose() }} maskClosable={false}
    footer={<Space><Button disabled={mutation.saving} onClick={onClose}>取消</Button><Button type="primary" loading={mutation.saving} disabled={!mutation.canEdit || conflict} onClick={() => void save()}>确认调整 {rows.length} 项</Button></Space>}>
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <div><Typography.Text type="secondary">作用于：</Typography.Text>{rows.map((row) => <Tag key={row.key}>{planningRowLabel(row)}</Tag>)}</div>
      {sceneTargets && <Alert type="info" showIcon message="逐项继承或覆盖本章默认" description="设置、追加、移除或替换仅自定义本场景的对应项；明确无是空覆盖，恢复继承重新跟随本章。未选择的项保持原状，不反写本章选角。" />}
      <div><Typography.Text strong>视角人物（POV）</Typography.Text><Space direction="vertical" style={{ width: '100%', marginTop: 8 }}>
        <Select aria-label="POV操作" value={draft.pov} disabled={mutation.saving} style={{ width: '100%' }} options={[{ value: 'keep', label: '保持不变' }, { value: 'set', label: sceneTargets ? '本场景自定义视角' : '设置视角角色' }, { value: 'clear', label: '明确无视角' }, ...(sceneTargets ? [{ value: 'inherit', label: '恢复继承本章' }] : [])]} onChange={(pov) => setDraft((old) => ({ ...old, pov }))} />
        {draft.pov === 'set' && <PlanningEntrySelect projectId={projectId} kind="character" single label="视角角色" value={draft.povId ? [draft.povId] : []} onChange={(ids) => setDraft((old) => ({ ...old, povId: ids[0] }))} disabled={mutation.saving} />}
      </Space></div>
      <div><Typography.Text strong>标签</Typography.Text><Space direction="vertical" style={{ width: '100%', marginTop: 8 }}>
        <Select aria-label="标签操作" value={draft.tags.op} style={{ width: '100%' }} options={operations} disabled={mutation.saving} onChange={(op) => setDraft((old) => ({ ...old, tags: { ...old.tags, op } }))} />
        {!['keep', 'clear', 'inherit'].includes(draft.tags.op) && <PlanningTagSelect projectId={projectId} value={draft.tags.ids} known={rows.flatMap((row) => row.tags)} disabled={mutation.saving} onChange={(ids) => setDraft((old) => ({ ...old, tags: { ...old.tags, ids } }))} />}
      </Space></div>
      <Divider style={{ margin: 0 }}>手动关联设定</Divider>
      {(Object.keys(PLANNING_REFERENCE_LABELS) as PlanningReferenceKind[]).map((kind) => {
        const choice = draft.references[kind] ?? { op: 'keep', ids: [] }
        return <div key={kind}><Typography.Text strong>{PLANNING_REFERENCE_LABELS[kind]}</Typography.Text><Space direction="vertical" style={{ width: '100%', marginTop: 8 }}>
          <Select aria-label={`${PLANNING_REFERENCE_LABELS[kind]}关联操作`} style={{ width: '100%' }} value={choice.op} options={operations} disabled={mutation.saving} onChange={(op) => setReference(kind, { ...choice, op })} />
          {!['keep', 'clear', 'inherit'].includes(choice.op) && <PlanningEntrySelect projectId={projectId} kind={kind} label={`选择${PLANNING_REFERENCE_LABELS[kind]}`} value={choice.ids} onChange={(ids) => setReference(kind, { ...choice, ids })} disabled={mutation.saving} />}
          {choice.op === 'replace' && <Typography.Text type="warning">会替换这些位置已有的全部{PLANNING_REFERENCE_LABELS[kind]}关联；其他类型保持不变。</Typography.Text>}
        </Space></div>
      })}
      {mutation.error && <Alert type="error" showIcon message={mutation.error} />}
      <PlanningRevisionReview projectId={projectId} failure={mutation.failure} targets={rows.map((row) => row.source)} originalRows={rows} onAccept={(next, latestRows) => { setRevision(next); if (latestRows) setCurrentRows(latestRows); mutation.clearError() }} />
    </Space>
  </Modal>
}
