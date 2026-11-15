import { useEffect, useState, useSyncExternalStore } from 'react'
import { currentRequestGeneration } from '../../api/client'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { useAuthStore } from '../../stores/auth'
import { captureProjectScope, isProjectScopeCurrent, useProjectStore, verifyProjectScope } from '../../stores/project'
import { projectAccess } from '../../utils/projectAccess'
import { invalidateProject } from '../../utils/projectRefresh'
import { cardAccess } from './cardAccess'
import { CardImportController } from './cardImportController'

/** 账号、授权和作品身份变化时旧确认失效，广播变化时保留输入以重新核对。 */
export function useCardImport(projectId: number, onImported: () => void) {
  const [controller] = useState(() => {
    const actorId = useAuthStore.getState().user?.id
    const generation = currentRequestGeneration()
    const canAccess = (write: boolean) => {
      const auth = useAuthStore.getState()
      if (auth.status !== 'authenticated' || auth.user?.id !== actorId || generation !== currentRequestGeneration()) return false
      const scope = captureProjectScope(projectId)
      if (!isProjectScopeCurrent(scope, write ? 'canEdit' : 'canRead')) return false
      const access = projectAccess(useProjectStore.getState().project, auth.user, projectId)
      const cards = cardAccess(access.role, access.isCollaborator, access.canRead, access.canEdit)
      return write ? cards.canImport : cards.canPreview
    }
    return new CardImportController(projectId, undefined, () => canAccess(false), () => {
      invalidateProject(projectId, ['characters', 'organizations', 'relationships', 'careers', 'setting_fields', 'setting-categories', 'setting-ai-visibility'])
      onImported()
    }, () => canAccess(true), (afterFailure) => verifyProjectScope(captureProjectScope(projectId), afterFailure))
  })
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  useEffect(() => {
    controller.activate()
    const fingerprint = () => `${useProjectStore.getState().scopeVersion}:${useAuthStore.getState().status}:${useAuthStore.getState().user?.id}:${currentRequestGeneration()}`
    let previous = fingerprint()
    const check = () => {
      const current = fingerprint()
      if (current !== previous) { previous = current; controller.accessChanged() }
    }
    const stopProject = useProjectStore.subscribe(check)
    const stopAuth = useAuthStore.subscribe(check)
    return () => { stopProject(); stopAuth(); controller.deactivate() }
  }, [controller])
  useProjectRefresh(projectId, ['characters', 'organizations', 'careers', 'world', 'setting_fields', 'setting-categories', 'setting-ai-visibility'], controller.invalidate)
  return { state, controller }
}
