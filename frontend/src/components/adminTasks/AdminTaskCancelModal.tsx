import { Alert, Button, Checkbox, Form, Input, Modal, Space, Typography } from 'antd'
import { useState } from 'react'
import type { AdminTaskBrief } from '../../api/types'
import { errorMessage } from '../../api/client'
import { useModalWidth } from '../../hooks/useIsMobile'
import { adminTaskCancelReason, adminTaskTitle } from '../../utils/adminTasks'
import { AdminTaskProject, AdminTaskStatusTags } from './AdminTaskViews'

export default function AdminTaskCancelModal({ task, loading, submitting, error, onClose, onSubmit }: {
  task: AdminTaskBrief; loading: boolean; submitting: boolean; error: string | null; onClose: () => void; onSubmit: (reason: string) => void
}) {
  const width = useModalWidth(580)
  const [reason, setReason] = useState('')
  const [confirming, setConfirming] = useState(false)
  const [checked, setChecked] = useState(false)
  const [validationError, setValidationError] = useState<string | null>(null)
  const validTarget = !loading && !error && task.can_cancel && !task.cancellation_pending
  const next = () => {
    try { setReason(adminTaskCancelReason(reason)); setValidationError(null); setConfirming(true) }
    catch (err) { setValidationError(errorMessage(err)) }
  }
  return <Modal open title={confirming ? '再次确认取消请求' : '申请取消任务'} width={width} onCancel={onClose} footer={null} destroyOnHidden>
    <Space direction="vertical" size={16} style={{ width: '100%', overflowWrap: 'anywhere' }}>
      <div><Typography.Text strong>{adminTaskTitle(task)}</Typography.Text><br />所属用户：{task.owner_username} · #{task.owner_user_id}<br />关联作品：<AdminTaskProject task={task} /><br />当前状态：<AdminTaskStatusTags task={task} /></div>
      <Alert type="warning" showIcon message="已保存内容和已执行效果保留" description="取消只停止后续执行，不自动撤销正文、智能体修改或外部工具效果。请求受理不等于任务已取消，完成保存或进入待审核可能先获胜，最终状态以安全收尾为准。" />
      {loading && <Typography.Text role="status">正在核对当前目标状态…</Typography.Text>}
      {error && <Alert type="error" showIcon message={error} />}
      {confirming ? <>
        <div><Typography.Text strong>取消原因</Typography.Text><Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>{reason}</Typography.Paragraph></div>
        <Checkbox autoFocus checked={checked} disabled={submitting} onChange={(event) => setChecked(event.target.checked)}>我已核对上述用户、作品和任务，明确请求取消，并理解已提交内容不会撤销。</Checkbox>
        <Space wrap><Button onClick={() => { setConfirming(false); setChecked(false) }} disabled={submitting}>返回修改</Button><Button danger type="primary" loading={submitting} disabled={!checked || !validTarget || submitting} onClick={() => onSubmit(reason)}>明确确认并提交取消请求</Button></Space>
      </> : <Form layout="vertical" onFinish={next} style={{ width: '100%' }}>
        <Form.Item htmlFor="admin-task-cancel-reason" label="取消原因（必填，1–200 字）" validateStatus={validationError ? 'error' : undefined} help={<span id="admin-task-cancel-help">{validationError ?? '原因将与真实管理员身份一起记录到操作日志；请勿粘贴任务原文、密钥或私有配置。'}</span>}>
          <Input.TextArea autoFocus id="admin-task-cancel-reason" aria-label="取消原因" aria-describedby="admin-task-cancel-help" aria-invalid={!!validationError} autoSize={{ minRows: 3, maxRows: 6 }} value={reason} maxLength={400} onChange={(event) => { setReason(event.target.value); setValidationError(null) }} disabled={submitting} />
          <Typography.Text type="secondary">{Array.from(reason.trim()).length} / 200 字</Typography.Text>
        </Form.Item>
        <Space wrap><Button onClick={onClose}>返回</Button><Button danger htmlType="submit" type="primary" disabled={!validTarget}>下一步：核对并确认</Button></Space>
      </Form>}
    </Space>
  </Modal>
}
