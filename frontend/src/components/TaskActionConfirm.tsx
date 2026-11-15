import { App, Popconfirm, type PopconfirmProps } from 'antd'
import { useRef } from 'react'
import { currentRequestGeneration } from '../api/client'
import type { Task } from '../api/types'
import { useAuthStore } from '../stores/auth'
import { captureProjectScope, isProjectScopeCurrent, type ProjectScope } from '../stores/project'
import { authorizationKey } from '../utils/permissions'
import { snapshotTaskAction } from '../utils/taskSnapshot'

interface Props extends Omit<PopconfirmProps, 'onConfirm' | 'onOpenChange'> {
  task: Task
  onConfirm: (task: Task) => void | Promise<void>
}

export default function TaskActionConfirm({ task, onConfirm, children, ...props }: Props) {
  const { message } = App.useApp()
  const selected = useRef<{ task: Task; actor: string; scope: ProjectScope | null } | null>(null)
  const actor = () => {
    const auth = useAuthStore.getState()
    return `${auth.status}:${auth.user?.id ?? ''}:${authorizationKey(auth.user)}:${currentRequestGeneration()}`
  }
  return <Popconfirm {...props} onOpenChange={(open) => {
    if (open) selected.current = { task: snapshotTaskAction(task), actor: actor(),
      scope: task.project_id === null ? null : captureProjectScope(task.project_id) }
  }} onConfirm={() => {
    const captured = selected.current
    if (captured && captured.actor === actor() && (!captured.scope || isProjectScopeCurrent(captured.scope))) return onConfirm(captured.task)
    message.warning('任务或会话已变化，请刷新后重新确认。')
  }}>{children}</Popconfirm>
}
