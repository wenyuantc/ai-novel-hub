import { useEffect, useState, useSyncExternalStore } from 'react'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'
import { authorizationKey } from '../../utils/permissions'
import { projectAccess } from '../../utils/projectAccess'
import { invalidateProject } from '../../utils/projectRefresh'
import { CardImportController } from './cardImportController'

/** 账号、授权和作品身份变化时旧确认失效，广播变化时保留输入以重新核对。 */
export function useCardImport(projectId: number, actorId: number, identity: string, onImported: () => void) {
  const [controller] = useState(() => new CardImportController(projectId, undefined, () => {
    const auth = useAuthStore.getState()
    return auth.status === 'authenticated' && auth.user?.id === actorId && authorizationKey(auth.user) === identity
      && projectAccess(useProjectStore.getState().project, auth.user, projectId).canEdit
  }, () => {
    invalidateProject(projectId, ['characters', 'organizations', 'relationships', 'careers', 'setting_fields', 'setting-categories', 'setting-ai-visibility'])
    onImported()
  }))
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  useEffect(() => { controller.activate(); return () => controller.deactivate() }, [controller])
  useProjectRefresh(projectId, ['characters', 'organizations', 'careers', 'world', 'setting_fields', 'setting-categories', 'setting-ai-visibility'], controller.invalidate)
  return { state, controller }
}
