import { App } from 'antd'
import { useEffect, useState, useSyncExternalStore } from 'react'
import { useAuthStore } from '../../stores/auth'
import { AdminUserActionsController } from './adminUserActionsController'

interface Options { actorId: number; onChanged: () => void; onDeleted: () => void }

/** 列表与详情各自拥有一个账号动作控制器，身份切换立即撤下目标弹窗。 */
export function useAdminUserActions({ actorId, onChanged, onDeleted }: Options) {
  const { message } = App.useApp()
  const [controller] = useState(() => new AdminUserActionsController(actorId, () => {
    const auth = useAuthStore.getState()
    return auth.status === 'authenticated' && auth.user?.id === actorId && auth.user.is_admin
  }, {
    changed: onChanged,
    deleted: onDeleted,
    success: (text) => { void message.success(text) },
  }))
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  useEffect(() => {
    controller.setCallbacks({ changed: onChanged, deleted: onDeleted, success: (text) => { void message.success(text) } })
  }, [controller, onChanged, onDeleted, message])
  useEffect(() => {
    controller.activate()
    const unsubscribe = useAuthStore.subscribe((auth) => {
      if (auth.status !== 'authenticated' || auth.user?.id !== actorId || !auth.user.is_admin) controller.deactivate()
    })
    return () => { unsubscribe(); controller.deactivate() }
  }, [actorId, controller])
  return { state, controller }
}
