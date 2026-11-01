import { useEffect, useMemo, useSyncExternalStore } from 'react'
import { useAuthStore } from '../../stores/auth'
import { DashboardController, startDashboardPolling } from './dashboardController'
import { can } from '../../utils/permissions'

/** 管理页局部状态，不跨账号缓存；StrictMode 重新挂载也只保留有效的一轮请求。 */
export function useAdminDashboard() {
  const owner = useAuthStore((state) => state.user?.id ?? null)
  const allowed = useAuthStore((state) => state.status === 'authenticated' && can(state.user, 'dashboard.read'))
  const controller = useMemo(() => new DashboardController(owner), [owner])
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)

  useEffect(() => {
    if (!allowed) return
    controller.activate()
    controller.refreshAll()
    return () => controller.deactivate()
  }, [allowed, controller])

  useEffect(() => {
    if (!allowed || state.denied || !state.autoRefresh) return
    return startDashboardPolling(controller.refreshAll, document)
  }, [allowed, controller, state.autoRefresh, state.denied])

  return { allowed, state, controller }
}
