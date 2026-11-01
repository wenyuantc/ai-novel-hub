import { authorizationKey, can } from '../../utils/permissions'
import { Alert, App, Input, Modal, Space, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { errorMessage } from '../../api/client'
import { recycleBinApi } from '../../api/recycleBin'
import type { RecycleProject } from '../../api/types'
import { useModalWidth } from '../../hooks/useIsMobile'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'
import { useTasksStore } from '../../stores/tasks'
import { canPurgeProject, canRestoreProject, createRecycleRequestGate } from '../../utils/recycleBin'
import { formatTime } from '../../utils/time'

export type RecycleAction = 'restore' | 'purge'

interface Props {
  project: RecycleProject
  action: RecycleAction
  actorId: number
  now: number
  onClose: () => void
  onChanged: () => void
}

interface ContentProps {
  project: RecycleProject
  restoring: boolean
  allowed: boolean
  busy: boolean
  input: string
  error: string | null
  onInput: (value: string) => void
  onSubmit: () => void
}

/** 确认正文与真实弹窗复用，保持不可逆提示和名称输入语义可独立验证。 */
export function RecycleProjectConfirmation({ project, restoring, allowed, busy, input, error, onInput, onSubmit }: ContentProps) {
  return <Space direction="vertical" size={16} style={{ width: '100%', overflowWrap: 'anywhere' }}>
    <Alert showIcon type={restoring ? 'info' : 'error'} message={restoring ? '恢复至原所属用户，保留原作品与关联内容。' : '将永久清理作品及其章节、版本、设定、封面等关联数据，无法恢复。'} description={restoring ? '不会启用已禁用的账号，也不会自动重试、继续或新建 AI / 索引任务。' : '此操作不经过新的 30 天保留期，也不会删除所属用户的模型配置或其他作品。'} />
    <div><Typography.Text>所属用户：{project.owner_username} · #{project.owner_user_id}</Typography.Text><br /><Typography.Text type="secondary">删除于 {formatTime(project.deleted_at, 'YYYY-MM-DD HH:mm:ss')} · 到期于 {formatTime(project.expires_at, 'YYYY-MM-DD HH:mm:ss')}</Typography.Text></div>
    {restoring && !allowed && <Alert showIcon type="warning" message="作品已到期或记录已不可恢复，请关闭后刷新回收站。" />}
    {!restoring && <div><Typography.Paragraph>请输入完整作品名称 <Typography.Text code>{project.title}</Typography.Text> 确认：</Typography.Paragraph><Input aria-label="确认彻底删除的作品名称" value={input} onChange={(event) => onInput(event.target.value)} placeholder={project.title} disabled={busy} autoComplete="off" onPressEnter={onSubmit} /></div>}
    {error && <Alert showIcon type="error" message={error} description="操作未在界面中提前生效；记录若已变化，请取消并刷新列表后重试。" />}
  </Space>
}

/** 恢复不自动续跑；不可逆清理必须输入当前记录的完整作品名。 */
export default function RecycleProjectModal({ project, action, actorId, now, onClose, onChanged }: Props) {
  const { message } = App.useApp()
  const width = useModalWidth(560)
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [gate] = useState(createRecycleRequestGate)
  const [identity] = useState(() => authorizationKey(useAuthStore.getState().user))
  const pending = useRef(false)
  useEffect(() => () => gate.invalidate(), [gate])
  const restoring = action === 'restore'
  const allowed = restoring ? canRestoreProject(project, now) : canPurgeProject(input, project.title)
  const permission = restoring ? 'recycle_bin.restore' : 'recycle_bin.purge'
  const isCurrent = (ticket: number) => gate.isCurrent(ticket) && useAuthStore.getState().user?.id === actorId && authorizationKey(useAuthStore.getState().user) === identity && can(useAuthStore.getState().user, permission)
  const close = () => { if (!pending.current) { gate.invalidate(); onClose() } }
  const submit = async () => {
    if (pending.current || !allowed || (restoring && !canRestoreProject(project))) return
    const user = useAuthStore.getState().user
    if (user?.id !== actorId || !can(user, permission) || authorizationKey(user) !== identity) return
    pending.current = true
    const ticket = gate.begin()
    setBusy(true)
    setError(null)
    try {
      if (restoring) await recycleBinApi.restore(project.id, { deleted_at: project.deleted_at, confirm: true })
      else await recycleBinApi.purge(project.id, { deleted_at: project.deleted_at, title: input, confirm: true })
      if (!isCurrent(ticket)) return
      useProjectStore.getState().discard(project.id)
      useTasksStore.getState().discardProject(project.id)
      message.success(restoring ? `已恢复《${project.title}》，任务不会自动续跑` : `已彻底删除《${project.title}》`)
      onChanged()
    } catch (err) {
      if (isCurrent(ticket)) setError(errorMessage(err))
    } finally {
      if (isCurrent(ticket)) { pending.current = false; setBusy(false) }
    }
  }
  return <Modal open width={width} title={`${restoring ? '恢复' : '彻底删除'}《${project.title}》？`} onCancel={close} onOk={() => void submit()} confirmLoading={busy} closable={!busy} maskClosable={!busy} keyboard={!busy} cancelButtonProps={{ disabled: busy }} okText={restoring ? '恢复作品' : '彻底删除'} cancelText="取消" okButtonProps={{ danger: !restoring, disabled: busy || !allowed }}>
    <RecycleProjectConfirmation project={project} restoring={restoring} allowed={allowed} busy={busy} input={input} error={error} onInput={setInput} onSubmit={() => void submit()} />
  </Modal>
}
