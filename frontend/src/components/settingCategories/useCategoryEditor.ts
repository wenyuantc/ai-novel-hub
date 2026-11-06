import { useEffect, useState, useSyncExternalStore } from 'react'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'
import { authorizationKey } from '../../utils/permissions'
import { projectAccess } from '../../utils/projectAccess'
import { invalidateProject } from '../../utils/projectRefresh'
import { CategoryEditor } from './categoryEditor'
import { CATEGORY_CONFIGURATION_RESOURCES } from './useCategoryRequest'

/** 同一作品与账号的草稿保留；授权变化中止操作，重新核对后才可保存。 */
export function useCategoryEditor(projectId: number, actorId: number, selected: (id: number | null) => void) {
  const [controller] = useState(() => {
    const access = () => {
      const auth = useAuthStore.getState()
      return auth.status === 'authenticated' && auth.user?.id === actorId ? projectAccess(useProjectStore.getState().project, auth.user, projectId) : null
    }
    return new CategoryEditor(projectId, undefined, undefined, () => access()?.canRead === true, () => access()?.canEdit === true,
      () => `${useAuthStore.getState().user?.id}:${authorizationKey(useAuthStore.getState().user)}`)
  })
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  useEffect(() => { controller.setChanged((id) => { selected(id); invalidateProject(projectId, ['setting-categories']) }) }, [controller, projectId, selected])
  useEffect(() => {
    controller.activate()
    let previous = `${actorId}:${authorizationKey(useAuthStore.getState().user)}`
    const stop = useAuthStore.subscribe((auth) => {
      const next = `${auth.user?.id}:${authorizationKey(auth.user)}`
      if (previous !== next || auth.status !== 'authenticated') { previous = next; controller.invalidate() }
    })
    return () => { stop(); controller.deactivate() }
  }, [actorId, controller])
  useProjectRefresh(projectId, CATEGORY_CONFIGURATION_RESOURCES, () => controller.invalidate())
  useProjectRefresh(projectId, ['characters', 'world', 'organizations', 'careers', 'chapters', 'outlines', 'planning', 'project'], controller.expirePreview)
  return { state, controller }
}
