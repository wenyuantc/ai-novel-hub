import { useEffect, useState, useSyncExternalStore } from 'react'
import { useAuthStore } from '../../stores/auth'
import { SystemSettingsController } from './systemSettingsController'

/** 页面按actor重建，异步落地再次复核当前启用的管理员会话。 */
export function useSystemSettings(actorId: number) {
  const [controller] = useState(() => new SystemSettingsController(undefined, () => {
    const auth = useAuthStore.getState()
    return auth.status === 'authenticated' && auth.user?.id === actorId && auth.user.is_admin
  }, (config) => useAuthStore.getState().applyConfig(config)))
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  useEffect(() => {
    controller.activate()
    void controller.refresh()
    const stop = useAuthStore.subscribe((auth) => {
      if (auth.status !== 'authenticated' || auth.user?.id !== actorId || !auth.user.is_admin) controller.revoke()
    })
    return () => { stop(); controller.deactivate() }
  }, [actorId, controller])
  return { state, controller }
}
