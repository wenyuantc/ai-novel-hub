import { Alert, App, Button, Drawer, Form, Input, Space, Spin, Tag, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { errorMessage } from '../../api/client'
import { workshopApi } from '../../api/workshop'
import type { WorkshopMetadata, WorkshopSubmission, WorkshopSubmit } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { useModalWidth } from '../../hooks/useIsMobile'
import { SUBMISSION_STATUS, updateShare, workshopSubmissionKindLabel, WORKSHOP_KINDS } from '../../utils/workshop'
import { formatTime } from '../../utils/time'
import WorkshopContent from './WorkshopContent'
import WorkshopMetadataFields from './WorkshopMetadataFields'

interface Props {
  submissionId: number
  review: boolean
  onClose: () => void
  onChanged: () => void
  onResubmit: (initial: WorkshopSubmit) => void
  onOpenItem: (id: number) => void
}
export default function ReviewPanel({ submissionId, review, onClose, onChanged, onResubmit, onOpenItem }: Props) {
  const width = useModalWidth(800)
  const loader = useCallback(() => workshopApi.submission(submissionId), [submissionId])
  const { data, error } = useLoader(loader)
  return <Drawer open title={review ? '审核分享' : '提交详情'} width={width} onClose={onClose} destroyOnHidden>
    {error ? <Alert type="error" showIcon message={error} /> : data ? <SubmissionDetail key={`${data.id}:${data.updated_at}`} submission={data}
      review={review} onDone={() => { onChanged(); onClose() }} onResubmit={onResubmit} onOpenItem={onOpenItem} /> : <Spin />}
  </Drawer>
}
interface DetailProps {
  submission: WorkshopSubmission
  review: boolean
  onDone: () => void
  onResubmit: (initial: WorkshopSubmit) => void
  onOpenItem: (id: number) => void
}
function SubmissionDetail({ submission: s, review, onDone, onResubmit, onOpenItem }: DetailProps) {
  const { message, modal } = App.useApp()
  const [form] = Form.useForm<WorkshopMetadata & { note: string }>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const run = async (action: () => Promise<WorkshopSubmission>, text: string) => {
    setBusy(true)
    setError(null)
    try { await action(); message.success(text); onDone() }
    catch (err) { setError(errorMessage(err)) } finally { setBusy(false) }
  }
  const approve = async () => {
    let values: WorkshopMetadata
    try { values = await form.validateFields(['category', 'tags']) } catch { return }
    await run(() => workshopApi.approve(s.id, { category: values.category, tags: values.tags }), '已通过审核')
  }
  const reject = async () => {
    let values: { note: string }
    try { values = await form.validateFields(['note']) } catch { return }
    await run(() => workshopApi.reject(s.id, values.note.trim()), '已驳回')
  }
  const status = SUBMISSION_STATUS[s.status]
  return <Space direction="vertical" size="middle" style={{ width: '100%', overflowWrap: 'anywhere' }}>
    <Typography.Title level={4} style={{ margin: 0 }}>{s.name}</Typography.Title>
    <Space wrap><Tag color={status.color}>{status.label}</Tag><Tag>{WORKSHOP_KINDS[s.kind]}</Tag><Tag>{workshopSubmissionKindLabel(s)}</Tag>{s.is_anonymous && <Tag>匿名分享</Tag>}</Space>
    <Typography.Text>作者：{s.author_username ?? s.author_name ?? '匿名'}</Typography.Text>
    <Typography.Text type="secondary">提交于 {formatTime(s.created_at)}{s.reviewed_at && ` · 审核于 ${formatTime(s.reviewed_at)}`}</Typography.Text>
    {s.prompt_key && <Typography.Text code>{s.prompt_key}</Typography.Text>}
    <Typography.Paragraph style={{ whiteSpace: 'pre-wrap' }}>{s.description || '暂无说明'}</Typography.Paragraph>
    {s.review_note && <Alert type={s.status === 'rejected' ? 'error' : 'info'} showIcon message="审核说明" description={s.review_note} />}
    {error && <Alert type="error" showIcon message={error} />}
    {review && s.status === 'pending' ? <Form form={form} layout="vertical" initialValues={{ category: s.category, tags: s.tags, note: '' }} disabled={busy} style={{ width: '100%' }}>
      <WorkshopMetadataFields disabled={busy} />
      <Form.Item name="note" label="驳回原因" rules={[{ required: true, whitespace: true, message: '驳回时必须填写原因' }, { max: 500 }]}><Input.TextArea maxLength={500} showCount autoSize={{ minRows: 2, maxRows: 5 }} /></Form.Item>
      <Space wrap><Button type="primary" loading={busy} onClick={() => void approve()}>通过审核</Button><Button danger disabled={busy} onClick={() => void reject()}>驳回</Button></Space>
      <Typography.Paragraph type="secondary" style={{ marginTop: 12 }}>可以调整分类和标签；正文须由作者修改后重新提交。</Typography.Paragraph>
    </Form> : <>
      <Form layout="vertical" initialValues={{ category: s.category, tags: s.tags }}><WorkshopMetadataFields disabled /></Form>
      <Space wrap>
        {!review && s.status === 'pending' && <Button disabled={busy} onClick={() => modal.confirm({ title: '撤回这个待审核提交？', okText: '撤回', cancelText: '取消', onOk: () => run(() => workshopApi.withdraw(s.id), '已撤回提交') })}>撤回提交</Button>}
        {!review && (s.status === 'rejected' || s.status === 'cancelled') && <Button onClick={() => onResubmit(updateShare(s))}>修改后重新提交</Button>}
        {s.item_id !== null && <Button onClick={() => onOpenItem(s.item_id!)}>查看已发布条目</Button>}
      </Space>
    </>}
    <WorkshopContent kind={s.kind} content={s.content} />
  </Space>
}
