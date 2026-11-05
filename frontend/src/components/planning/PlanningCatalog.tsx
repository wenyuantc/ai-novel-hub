import { Alert, Button, Form, Input, List, Modal, Pagination, Select, Space, Spin, Tag, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { ApiError } from '../../api/client'
import { planningApi } from '../../api/planning'
import type { PlanningPage, PlanningRevision, PlanningTag, PlanningTone, Subplot } from '../../api/types'
import { useModalWidth } from '../../hooks/useIsMobile'
import { useMentionQuery as usePlanningQuery } from '../../hooks/useMentionQuery'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { PLANNING_TONES } from '../../utils/planning'
import PlanningRevisionReview from './PlanningRevisionReview'
import { usePlanningMutation } from './usePlanningMutation'

interface Props { projectId: number; kind: 'subplots' | 'tags'; onClose: () => void; onSaved: (revision: number) => void }
export default function PlanningCatalog({ projectId, kind, onClose, onSaved }: Props) {
  const [q, setQ] = useState('')
  const [page, setPage] = useState(1)
  const [editing, setEditing] = useState<{ item?: Subplot | PlanningTag; revision: number }>()
  const { canEdit } = useProjectAccess(projectId)
  const width = useModalWidth(780)
  const load = useCallback((signal: AbortSignal) => kind === 'subplots'
    ? planningApi.subplots(projectId, { q, page, page_size: 20 }, signal)
    : planningApi.tags(projectId, { q, page, page_size: 20 }, signal), [kind, projectId, q, page])
  const result = usePlanningQuery<PlanningPage<PlanningTag | Subplot>>(projectId, `catalog:${kind}:${q}:${page}`, load)
  useProjectRefresh(projectId, ['planning'], result.reload)
  const label = kind === 'subplots' ? '支线' : '标签'
  return <>
    <Modal open title={`${label}管理`} width={width} footer={<Button onClick={onClose}>关闭</Button>} onCancel={onClose}>
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Space wrap><Input.Search aria-label={`搜索${label}`} placeholder={`搜索${label}名称`} value={q} onChange={(event) => { setQ(event.target.value); setPage(1) }} allowClear maxLength={200} /><Button type="primary" disabled={!canEdit || !result.data} onClick={() => result.data && setEditing({ revision: result.data.revision })}>新建{label}</Button></Space>
        {result.loading && <Spin />}
        {result.error && <Alert type="error" message={result.error} action={<Button onClick={result.reload}>重试</Button>} />}
        {result.data && <>
          <List dataSource={result.data.items} locale={{ emptyText: `暂无${label}，可从右上方新建。` }} renderItem={(item) => <List.Item actions={[<Button key="edit" onClick={() => setEditing({ item, revision: result.data!.revision })}>{canEdit ? '编辑' : '查看'}</Button>]}>
            <List.Item.Meta title={<Tag color={item.tone}>{item.name}</Tag>} description={'description' in item ? <><Typography.Paragraph ellipsis={{ rows: 2 }}>{item.description || '暂无作者概览'}</Typography.Paragraph><Typography.Text type="secondary">{item.node_count} 个节点 · {item.crossing_count} 个交汇</Typography.Text></> : `${item.usage_count} 个规划位置正在使用`} />
          </List.Item>} />
          <Pagination current={page} pageSize={20} total={result.data.total} onChange={setPage} showSizeChanger={false} showTotal={(total) => `共 ${total} 项`} />
        </>}
      </Space>
    </Modal>
    {editing && <CatalogEditor key={`${kind}:${editing.item?.id ?? 'new'}:${editing.revision}`} projectId={projectId} kind={kind} item={editing.item} revision={editing.revision} onClose={() => setEditing(undefined)} onSaved={(revision) => { onSaved(revision); result.reload() }} />}
  </>
}
function CatalogEditor({ projectId, kind, item, revision: initialRevision, onClose, onSaved }: Props & { item?: Subplot | PlanningTag; revision: number }) {
  const [revision, setRevision] = useState(initialRevision)
  const [name, setName] = useState(item?.name ?? '')
  const [tone, setTone] = useState<PlanningTone>(item?.tone ?? 'default')
  const [description, setDescription] = useState(item && 'description' in item ? item.description : '')
  const [confirmDelete, setConfirmDelete] = useState(false)
  const mutation = usePlanningMutation(projectId, onSaved)
  const width = useModalWidth(600)
  const label = kind === 'subplots' ? '支线' : '标签'
  const blocked = mutation.failure instanceof ApiError && [0, 409].includes(mutation.failure.status)
  const save = async () => {
    if (!name.trim()) { mutation.setError(`请填写${label}名称`); return }
    const body = { expected_revision: revision, name: name.trim(), tone }
    const result = await mutation.run<PlanningRevision>(() => kind === 'tags'
      ? (item ? planningApi.updateTag(projectId, item.id, body) : planningApi.createTag(projectId, body))
      : (item ? planningApi.updateSubplot(projectId, item.id, { ...body, description }) : planningApi.createSubplot(projectId, { ...body, description })))
    if (result) onClose()
  }
  const remove = async () => {
    if (!item) return
    const result = await mutation.run(() => kind === 'tags' ? planningApi.deleteTag(projectId, item.id, revision) : planningApi.deleteSubplot(projectId, item.id, revision))
    if (result) onClose()
  }
  return <Modal open title={`${item ? '维护' : '新建'}${label}`} width={width} maskClosable={false} onCancel={() => { if (!mutation.saving) onClose() }}
    footer={<Space><Button disabled={mutation.saving} onClick={onClose}>关闭</Button><Button type="primary" disabled={!mutation.canEdit || blocked} loading={mutation.saving} onClick={() => void save()}>保存{label}</Button></Space>}>
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Form layout="vertical" disabled={!mutation.canEdit || mutation.saving}>
        <Form.Item label={`${label}名称`} required><Input aria-label={`${label}名称`} maxLength={kind === 'tags' ? 50 : 100} value={name} onChange={(event) => setName(event.target.value)} /></Form.Item>
        <Form.Item label="颜色"><Select aria-label={`${label}颜色`} value={tone} options={PLANNING_TONES} onChange={setTone} /></Form.Item>
        {kind === 'subplots' && <Form.Item label="作者概览" extra="用于整体构思，不自动进入 AI；写作目标请记录在具体章节的推进节点或交汇事件。"><Input.TextArea aria-label="支线作者概览" rows={6} maxLength={5000} showCount value={description} onChange={(event) => setDescription(event.target.value)} /></Form.Item>}
      </Form>
      {mutation.error && <Alert type="error" showIcon message={mutation.error} />}
      <PlanningRevisionReview projectId={projectId} failure={mutation.failure} resource={{ kind, id: item?.id, q: name }} onAccept={(next) => { setRevision(next); mutation.clearError() }} />
      {item && <Button danger disabled={!mutation.canEdit || mutation.saving || blocked} onClick={() => setConfirmDelete(true)}>删除{label}</Button>}
      {confirmDelete && <Alert type="warning" message={`确认删除「${name}」？`} description={kind === 'tags' ? '所有规划位置上的这个标签也会移除。' : '这条支线的普通推进节点会删除；若仍有交汇事件引用，需要先处理交汇。'} action={<Button danger loading={mutation.saving} disabled={blocked} onClick={() => void remove()}>确认删除</Button>} />}
    </Space>
  </Modal>
}
