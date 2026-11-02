import { useEffect, useState, useSyncExternalStore } from 'react'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey, can } from '../../utils/permissions'
import { startVisiblePolling } from '../../utils/visiblePolling'
import { AdminBackupsController } from './adminBackupsController'

/** actor与权限代次共同拥有页面；撤权、换账号或卸载即清除秘密及所有迟到响应。 */
export function useAdminBackups(actorId: number, scope: string) {
  const [controller] = useState(() => {
    const current = () => {
      const auth = useAuthStore.getState()
      return auth.status === 'authenticated' && auth.user?.id === actorId && authorizationKey(auth.user) === scope ? auth.user : null
    }
    return new AdminBackupsController(undefined, {
      read: () => can(current(), 'backups.read'), configure: () => can(current(), 'backups.configure'),
      run: () => can(current(), 'backups.run'), download: () => can(current(), 'backups.download'),
    })
  })
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  useEffect(() => {
    controller.activate(); controller.refreshAll(); void controller.refreshSettings()
    const unsubscribe = useAuthStore.subscribe((auth) => {
      if (auth.status !== 'authenticated' || auth.user?.id !== actorId || authorizationKey(auth.user) !== scope || !can(auth.user, 'backups.read')) controller.revoke()
    })
    return () => { unsubscribe(); controller.deactivate() }
  }, [actorId, scope, controller])
  useEffect(() => {
    if (state.denied || !state.autoRefresh) return
    return startVisiblePolling(controller.refreshAll, document, 10_000)
  }, [state.autoRefresh, state.denied, controller])
  return { state, controller }
}
