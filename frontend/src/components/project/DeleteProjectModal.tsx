import { Alert, App, Input, Modal, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { errorMessage } from '../../api/client'
import { projectApi } from '../../api/projects'
import { canConfirmProjectDelete } from '../../utils/projectAccess'
import { createRecycleRequestGate, PROJECT_DELETE_NOTICE } from '../../utils/recycleBin'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'
import { useTasksStore } from '../../stores/tasks'
import { useModalWidth } from '../../hooks/useIsMobile'

interface Props {
  project: { id: number; title: string; owner_username?: string }
  onClose: () => void
  onDeleted: () => void
}

/** 普通删除不是永久清理，所有入口共享保留期与任务停止说明。 */
export function DeleteProjectNotice({ owner }: { owner?: string }) {
  return <Alert type="warning" showIcon message={`将${owner ? `「${owner}」的` : ''}作品及关联内容移入回收站。`} description={PROJECT_DELETE_NOTICE} style={{ marginBottom: 16 }} />
}

/** 管理列表与工作区共用同一删除确认，避免绕过名称确认。 */
export default function DeleteProjectModal({ project, onClose, onDeleted }: Props) {
  const { message } = App.useApp()
  const width = useModalWidth(560)
  const [input, setInput] = useState('')
  const [deleting, setDeleting] = useState(false)
  const pending = useRef(false)
  const [gate] = useState(createRecycleRequestGate)
  useEffect(() => () => gate.invalidate(), [gate])
  const allowed = canConfirmProjectDelete(input, project.title)
  const remove = async () => {
    if (!allowed || pending.current) return
    const actorId = useAuthStore.getState().user?.id
    if (actorId === undefined) return
    pending.current = true
    const ticket = gate.begin()
    const isCurrent = () => gate.isCurrent(ticket) && useAuthStore.getState().user?.id === actorId
    setDeleting(true)
    try {
      await projectApi.remove(project.id)
      if (!isCurrent()) return
      useProjectStore.getState().discard(project.id)
      useTasksStore.getState().discardProject(project.id)
      message.success('作品已移入回收站，30 天内可联系管理员恢复')
      onDeleted()
    } catch (err) {
      if (isCurrent()) message.error(errorMessage(err))
    } finally {
      if (isCurrent()) { pending.current = false; setDeleting(false) }
    }
  }
  return <Modal open width={width} title={`将《${project.title}》移入回收站？`} onCancel={deleting ? undefined : () => { gate.invalidate(); onClose() }} closable={!deleting} maskClosable={!deleting} keyboard={!deleting} cancelButtonProps={{ disabled: deleting }} onOk={() => void remove()} confirmLoading={deleting} okText="移入回收站" cancelText="取消" okButtonProps={{ danger: true, disabled: !allowed || deleting }}>
    <DeleteProjectNotice owner={project.owner_username} />
    <Typography.Paragraph>请输入项目名称 <Typography.Text code>{project.title}</Typography.Text> 确认：</Typography.Paragraph>
    <Input value={input} onChange={(e) => setInput(e.target.value)} placeholder={project.title} disabled={deleting} autoComplete="off" onPressEnter={() => void remove()} aria-label="确认删除的项目名称" />
  </Modal>
}
