import { useEffect, useState, useSyncExternalStore } from 'react'
import type { AdminTaskListQuery } from '../../api/types'
import { useAuthStore } from '../../stores/auth'
import { startDashboardPolling } from '../dashboard/dashboardController'
import { AdminTasksController } from './adminTasksController'

/** 由页面 actor ID 的 key 保证账号切换重建；所有异步落地另复核实时身份。 */
export function useAdminTasks(actorId: number, initialQuery: AdminTaskListQuery) {
  const [controller] = useState(() => new AdminTasksController(actorId, initialQuery, undefined, () => {
    const auth = useAuthStore.getState()
    return auth.status === 'authenticated' && auth.user?.id === actorId && auth.user.is_admin
  }))
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  useEffect(() => {
    controller.activate()
    controller.refreshAll()
    const unsubscribe = useAuthStore.subscribe((auth) => {
      if (auth.status !== 'authenticated' || auth.user?.id !== actorId || !auth.user.is_admin) controller.revoke()
    })
    return () => { unsubscribe(); controller.deactivate() }
  }, [actorId, controller])
  useEffect(() => {
    if (state.denied || !state.autoRefresh) return
    return startDashboardPolling(controller.refreshAll, document, 5_000)
  }, [controller, state.autoRefresh, state.denied])
  return { state, controller }
}
