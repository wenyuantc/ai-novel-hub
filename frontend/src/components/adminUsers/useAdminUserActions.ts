import { App } from 'antd'
import { useEffect, useState, useSyncExternalStore } from 'react'
import { useAuthStore } from '../../stores/auth'
import { AdminUserActionsController } from './adminUserActionsController'
import { authorizationKey, can } from '../../utils/permissions'

interface Options { actorId: number; onChanged: () => void; onDeleted: () => void }

/** 列表与详情各自拥有一个账号动作控制器，身份切换立即撤下目标弹窗。 */
export function useAdminUserActions({ actorId, onChanged, onDeleted }: Options) {
  const { message } = App.useApp()
  const [controller] = useState(() => new AdminUserActionsController(actorId, () => {
    const auth = useAuthStore.getState()
    return auth.status === 'authenticated' && auth.user?.id === actorId && can(auth.user, 'users.read')
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
    let previous = authorizationKey(useAuthStore.getState().user)
    const unsubscribe = useAuthStore.subscribe((auth) => {
      const next = authorizationKey(auth.user)
      if (next !== previous) { controller.deactivate(); previous = next }
      if (auth.status !== 'authenticated' || auth.user?.id !== actorId || !can(auth.user, 'users.read')) controller.deactivate()
      else controller.activate()
    })
    return () => { unsubscribe(); controller.deactivate() }
  }, [actorId, controller])
  return { state, controller }
}
