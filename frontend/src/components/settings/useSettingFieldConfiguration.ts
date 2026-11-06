import { useEffect, useState, useSyncExternalStore } from 'react'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'
import { authorizationKey } from '../../utils/permissions'
import { projectAccess } from '../../utils/projectAccess'
import { invalidateProject } from '../../utils/projectRefresh'
import { SettingFieldsController } from './settingFieldsController'

/** 作品与actor拥有本地草稿；同actor授权改变使在途操作失效并保留草稿。 */
export function useSettingFieldConfiguration(projectId: number, actorId: number) {
  const [controller] = useState(() => {
    const access = () => {
      const auth = useAuthStore.getState()
      return auth.status === 'authenticated' && auth.user?.id === actorId
        ? projectAccess(useProjectStore.getState().project, auth.user, projectId) : null
    }
    return new SettingFieldsController(projectId, undefined, () => access()?.canRead === true, () => access()?.canEdit === true,
      (aiChanged) => invalidateProject(projectId, aiChanged ? ['setting_fields', 'setting-ai-visibility'] : ['setting_fields']))
  })
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  useEffect(() => {
    controller.activate()
    void controller.refresh()
    let previous = authorizationKey(useAuthStore.getState().user)
    const stop = useAuthStore.subscribe((auth) => {
      const next = authorizationKey(auth.user)
      if (next !== previous || auth.status !== 'authenticated' || auth.user?.id !== actorId) {
        previous = next
        controller.invalidate()
        void controller.refresh()
      }
    })
    return () => { stop(); controller.deactivate() }
  }, [actorId, controller])
  useProjectRefresh(projectId, ['setting_fields'], () => { controller.invalidate(); void controller.refresh() })
  return { state, controller }
}
