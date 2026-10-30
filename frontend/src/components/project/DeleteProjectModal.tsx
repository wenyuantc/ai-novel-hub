import { Alert, App, Input, Modal, Typography } from 'antd'
import { useState } from 'react'
import { errorMessage } from '../../api/client'
import { projectApi } from '../../api/projects'
import { canConfirmProjectDelete } from '../../utils/projectAccess'

interface Props {
  project: { id: number; title: string; owner_username?: string }
  onClose: () => void
  onDeleted: () => void
}

/** 管理列表与工作区共用同一删除确认，避免绕过名称确认。 */
export default function DeleteProjectModal({ project, onClose, onDeleted }: Props) {
  const { message } = App.useApp()
  const [input, setInput] = useState('')
  const [deleting, setDeleting] = useState(false)
  const allowed = canConfirmProjectDelete(input, project.title)
  const remove = async () => {
    if (!allowed || deleting) return
    setDeleting(true)
    try {
      await projectApi.remove(project.id)
      message.success('项目已删除')
      onDeleted()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setDeleting(false)
    }
  }
  return <Modal open title={`删除《${project.title}》？`} onCancel={deleting ? undefined : onClose} closable={!deleting} maskClosable={!deleting} onOk={() => void remove()} confirmLoading={deleting} okText="永久删除" cancelText="取消" okButtonProps={{ danger: true, disabled: !allowed }}>
    <Alert type="error" showIcon message={`将永久删除${project.owner_username ? `「${project.owner_username}」的` : ''}项目及其章节和设定，无法恢复。`} style={{ marginBottom: 16 }} />
    <Typography.Paragraph>请输入项目名称 <Typography.Text code>{project.title}</Typography.Text> 确认：</Typography.Paragraph>
    <Input value={input} onChange={(e) => setInput(e.target.value)} placeholder={project.title} disabled={deleting} autoComplete="off" onPressEnter={() => void remove()} aria-label="确认删除的项目名称" />
  </Modal>
}
