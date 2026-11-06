import { useEffect, useMemo, useSyncExternalStore } from 'react'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'
import { authorizationKey } from '../../utils/permissions'
import { projectAccess } from '../../utils/projectAccess'
import type { ProjectResource } from '../../utils/projectRefresh'
import { CategoryRequest } from './categoryRequest'

export const CATEGORY_RESULT_RESOURCES: readonly ProjectResource[] = ['setting-categories', 'setting_fields', 'characters', 'world', 'organizations', 'careers', 'chapters', 'outlines', 'planning', 'project']
export const CATEGORY_CONFIGURATION_RESOURCES: readonly ProjectResource[] = ['setting-categories', 'setting_fields']

/** 查询内容由作品、账号、授权与loader拥有；相同revision的重试也发起新请求。 */
export function useCategoryRequest<T>(projectId: number, actorId: number, loader: (signal: AbortSignal) => Promise<T>, resources: readonly ProjectResource[] = CATEGORY_RESULT_RESOURCES) {
  const user = useAuthStore((state) => state.user)
  const identity = `${actorId}:${authorizationKey(user)}`
  const controller = useMemo(() => new CategoryRequest(loader, () => {
    const auth = useAuthStore.getState()
    return auth.status === 'authenticated' && auth.user?.id === actorId && `${actorId}:${authorizationKey(auth.user)}` === identity
      && projectAccess(useProjectStore.getState().project, auth.user, projectId).canRead
  }, () => `${useAuthStore.getState().user?.id}:${authorizationKey(useAuthStore.getState().user)}`), [actorId, identity, loader, projectId])
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  useEffect(() => { controller.activate(); void controller.refresh(); return () => controller.deactivate() }, [controller])
  useProjectRefresh(projectId, resources, () => { void controller.refresh() })
  return { ...state, refresh: controller.refresh }
}
