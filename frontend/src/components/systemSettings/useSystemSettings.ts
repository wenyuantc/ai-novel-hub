import { useEffect, useState, useSyncExternalStore } from 'react'
import { useAuthStore } from '../../stores/auth'
import { DRAIN_STATUS_POLL_MS, SystemSettingsController } from './systemSettingsController'
import { authorizationKey, can } from '../../utils/permissions'
import { startForegroundPolling } from '../../utils/visiblePolling'

export function watchSystemSettingsAuthorization(actorId: number, controller: SystemSettingsController, refreshDrain: () => void) {
  let previous = authorizationKey(useAuthStore.getState().user)
  return useAuthStore.subscribe(({ status, user }) => {
    if (status !== 'authenticated' || user?.id !== actorId || !can(user, 'settings.read')) {
      controller.revoke()
      return
    }
    const next = authorizationKey(user)
    if (next === previous) return
    previous = next
    controller.revokeWrites(can(user, 'settings.update') ? null : undefined)
    controller.invalidateDrainStatus()
    void controller.refresh()
    refreshDrain()
  })
}

/** 页面按actor重建，异步落地再次复核当前启用的管理员会话。 */
export function useSystemSettings(actorId: number) {
  const [controller] = useState(() => new SystemSettingsController(undefined, () => {
    const auth = useAuthStore.getState()
    return auth.status === 'authenticated' && auth.user?.id === actorId && can(auth.user, 'settings.read')
  }, (config) => useAuthStore.getState().applyConfig(config), () => can(useAuthStore.getState().user, 'settings.update')))
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  useEffect(() => {
    controller.activate()
    const refreshDrain = () => {
      if (document.visibilityState === 'visible') void controller.refreshDrainStatus()
    }
    const stop = watchSystemSettingsAuthorization(actorId, controller, refreshDrain)
    void controller.refresh()
    refreshDrain()
    const stopPolling = startForegroundPolling(refreshDrain, document, window, DRAIN_STATUS_POLL_MS)
    return () => { stopPolling(); stop(); controller.deactivate() }
  }, [actorId, controller])
  return { state, controller }
}
