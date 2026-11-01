import { useEffect, useState, useSyncExternalStore } from 'react'
import { useAuthStore } from '../../stores/auth'
import { SystemSettingsController } from './systemSettingsController'
import { authorizationKey, can } from '../../utils/permissions'

/** 页面按actor重建，异步落地再次复核当前启用的管理员会话。 */
export function useSystemSettings(actorId: number) {
  const [controller] = useState(() => new SystemSettingsController(undefined, () => {
    const auth = useAuthStore.getState()
    return auth.status === 'authenticated' && auth.user?.id === actorId && can(auth.user, 'settings.read')
  }, (config) => useAuthStore.getState().applyConfig(config), () => can(useAuthStore.getState().user, 'settings.update')))
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  useEffect(() => {
    controller.activate()
    void controller.refresh()
    let previous = authorizationKey(useAuthStore.getState().user)
    const stop = useAuthStore.subscribe((auth) => {
      const next = authorizationKey(auth.user)
      if (next !== previous) { controller.revokeWrites(); previous = next }
      if (auth.status !== 'authenticated' || auth.user?.id !== actorId || !can(auth.user, 'settings.read')) controller.revoke()
      else if (!can(auth.user, 'settings.update')) controller.revokeWrites()
    })
    return () => { stop(); controller.deactivate() }
  }, [actorId, controller])
  return { state, controller }
}
