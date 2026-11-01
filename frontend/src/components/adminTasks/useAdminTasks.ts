import { useEffect, useState, useSyncExternalStore } from 'react'
import type { AdminTaskListQuery } from '../../api/types'
import { useAuthStore } from '../../stores/auth'
import { startDashboardPolling } from '../dashboard/dashboardController'
import { AdminTasksController } from './adminTasksController'
import { authorizationKey, can } from '../../utils/permissions'

/** 由页面 actor ID 的 key 保证账号切换重建；所有异步落地另复核实时身份。 */
export function useAdminTasks(actorId: number, initialQuery: AdminTaskListQuery) {
  const [controller] = useState(() => new AdminTasksController(actorId, initialQuery, undefined, () => {
    const auth = useAuthStore.getState()
    return auth.status === 'authenticated' && auth.user?.id === actorId && can(auth.user, 'tasks.read')
  }, () => can(useAuthStore.getState().user, 'tasks.cancel'), () => can(useAuthStore.getState().user, 'users.read')))
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  useEffect(() => {
    controller.activate()
    controller.refreshAll()
    let previous = authorizationKey(useAuthStore.getState().user)
    const unsubscribe = useAuthStore.subscribe((auth) => {
      const next = authorizationKey(auth.user)
      if (next !== previous) { controller.revokeWrites(); previous = next }
      if (!can(auth.user, 'users.read')) controller.revokeOwners()
      if (auth.status !== 'authenticated' || auth.user?.id !== actorId || !can(auth.user, 'tasks.read')) controller.revoke()
    })
    return () => { unsubscribe(); controller.deactivate() }
  }, [actorId, controller])
  useEffect(() => {
    if (state.denied || !state.autoRefresh) return
    return startDashboardPolling(controller.refreshAll, document, 5_000)
  }, [controller, state.autoRefresh, state.denied])
  return { state, controller }
}
