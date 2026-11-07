import { Alert, Button, Drawer, Empty, Input, List, Modal, Pagination, Space, Spin, Tag, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { ApiError } from '../../api/client'
import { planningApi } from '../../api/planning'
import type { PlanningRow, PlanningSource } from '../../api/types'
import { useModalWidth } from '../../hooks/useIsMobile'
import { useMentionQuery as usePlanningQuery } from '../../hooks/useMentionQuery'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { planningRowLabel, readPlanningConflict } from '../../utils/planning'
import type { PlanningMergeConflict } from '../../utils/planning'
import { PlanningSourceSelect, SameChapterSceneSelect } from './PlanningFields'
import PlanningRevisionReview from './PlanningRevisionReview'
import { usePlanningMutation } from './usePlanningMutation'

interface Props {
  projectId: number; onClose: () => void; onSaved: (revision: number) => void
  onEdit: (row: PlanningRow, revision: number) => void; onNode: (row: PlanningRow) => void
  onCrossings: (row: PlanningRow) => void; onMerge: (conflict: PlanningMergeConflict) => void
}
export default function PlanningDetachedDrawer({ projectId, onClose, onSaved, onEdit, onNode, onCrossings, onMerge }: Props) {
  const [page, setPage] = useState(1)
  const [q, setQ] = useState('')
  const [binding, setBinding] = useState<{ row: PlanningRow; revision: number }>()
  const { canEdit } = useProjectAccess(projectId)
  const width = useModalWidth(760)
  const load = useCallback((signal: AbortSignal) => planningApi.detached(projectId, { page, page_size: 20, q }, signal), [projectId, page, q])
  const result = usePlanningQuery(projectId, `detached:${page}:${q}`, load)
  useProjectRefresh(projectId, ['planning', 'chapters', 'outlines'], result.reload)
  return <>
    <Drawer open title="待重新关联的规划" width={width} onClose={onClose}>
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Alert type="info" showIcon message="源章节或细纲已移除，规划内容仍保留" description="这些规划不会进入任何章节的 AI 上下文。核对内容后，可明确关联到一个现有章节或细纲。" />
        <Input.Search aria-label="搜索待关联规划" placeholder="搜索原标题" value={q} allowClear onChange={(event) => { setQ(event.target.value); setPage(1) }} />
        {result.loading && <Spin />}
        {result.error && <Alert type="error" message={result.error} action={<Button onClick={result.reload}>重试</Button>} />}
        {result.data && <>
          <List dataSource={result.data.items} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有待重新关联的规划" /> }} renderItem={(row) => <List.Item style={{ display: 'block' }}>
            <Typography.Text strong>{planningRowLabel(row)}</Typography.Text><div className="planning-meta">{row.volume_title} · {row.references.length} 项手动关联{row.pov_character_id ? ` · POV ${row.pov_character_name ?? `角色 #${row.pov_character_id}`}` : ''}</div>
            <Space wrap>{row.tags.map((tag) => <Tag key={tag.id} color={tag.tone}>{tag.name}</Tag>)}</Space>
            <Space wrap style={{ marginTop: 8 }}><Button disabled={!canEdit} onClick={() => setBinding({ row, revision: result.data!.revision })}>重新关联</Button><Button disabled={!canEdit} onClick={() => onEdit(row, result.data!.revision)}>编辑规划</Button><Button onClick={() => onNode(row)}>查看推进节点</Button><Button onClick={() => onCrossings(row)}>查看交汇</Button></Space>
          </List.Item>} />
          <Pagination current={page} pageSize={20} total={result.data.total} onChange={setPage} showSizeChanger={false} showTotal={(total) => `共 ${total} 份规划`} />
        </>}
      </Space>
    </Drawer>
    {binding && <BindingModal projectId={projectId} row={binding.row} revision={binding.revision} onClose={() => setBinding(undefined)} onSaved={onSaved} onMerge={(conflict) => { setBinding(undefined); onMerge(conflict) }} />}
  </>
}
function BindingModal({ projectId, row, revision: initialRevision, onClose, onSaved, onMerge }: { projectId: number; row: PlanningRow; revision: number; onClose: () => void; onSaved: (revision: number) => void; onMerge: (conflict: PlanningMergeConflict) => void }) {
  const [revision, setRevision] = useState(initialRevision)
  const [target, setTarget] = useState<PlanningSource>()
  const [targetRow, setTargetRow] = useState<PlanningRow>()
  const mutation = usePlanningMutation(projectId, onSaved)
  const width = useModalWidth(640)
  const blocked = mutation.failure instanceof ApiError && [0, 409].includes(mutation.failure.status)
  const merge = readPlanningConflict(mutation.failure)
  const save = async () => {
    if (!target || row.unit_id === null) { mutation.setError('请选择新的关联位置'); return }
    if (row.source_level === 'scene' && (!row.source_chapter_id || target.kind !== 'scene' || !targetRow || targetRow.chapter_id !== row.source_chapter_id || targetRow.content_revision == null)) { mutation.setError('只能重连到原章的活动场景；请重新核对目标版本'); return }
    const expected_content_revisions = row.source_level === 'scene' && targetRow?.chapter_id && targetRow.content_revision != null ? [{ chapter_id: targetRow.chapter_id, content_revision: targetRow.content_revision }] : undefined
    const result = await mutation.run(() => planningApi.bind(projectId, row.unit_id!, { expected_revision: revision, target, ...(expected_content_revisions ? { expected_content_revisions } : {}) }))
    if (result) onClose()
  }
  return <Modal open title={`重新关联 · ${row.title}`} width={width} maskClosable={false} onCancel={() => { if (!mutation.saving) onClose() }} footer={<Space><Button disabled={mutation.saving} onClick={onClose}>取消</Button><Button type="primary" disabled={!mutation.canEdit || blocked} loading={mutation.saving} onClick={() => void save()}>确认关联</Button></Space>}>
    <Space direction="vertical" size={16} style={{ width: '100%' }}>{row.source_level === 'scene' ? row.source_chapter_id ? <SameChapterSceneSelect projectId={projectId} chapterId={row.source_chapter_id} value={target} onChange={(next) => { setTargetRow(next); setTarget(next.source) }} disabled={mutation.saving || !mutation.canEdit} /> : <Alert type="warning" message="原章已不存在，无法证明场景归属，不能重连；规划内容仍保留。" /> : <PlanningSourceSelect projectId={projectId} allowedKinds={['chapter', 'plan']} value={target} onChange={(next) => { if (next?.kind !== 'scene') setTarget(next) }} disabled={mutation.saving || !mutation.canEdit} />}
      {mutation.error && <Alert type="error" showIcon message={mutation.error} />}
      {merge && <Button onClick={() => onMerge(merge)}>核对并合并双方规划</Button>}
      <PlanningRevisionReview projectId={projectId} failure={mutation.failure} targets={[row.source, ...(target ? [target] : [])]} originalRows={[row]} onAccept={(next, latestRows) => { setRevision(next); const latest = latestRows?.find((item) => item.source.kind === 'scene' && item.source.id === target?.id); if (latest) setTargetRow(latest); mutation.clearError() }} />
    </Space>
  </Modal>
}
