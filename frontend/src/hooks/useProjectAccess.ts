import { useCallback, useEffect, useMemo, useRef } from 'react'
import { useAuthStore } from '../stores/auth'
import {
  captureProjectScope, isProjectScopeCurrent, useProjectStore, type ProjectCapability,
} from '../stores/project'
import { authorizationKey } from '../utils/permissions'
import { projectAccess } from '../utils/projectAccess'

/** UI 的服务端能力快照与请求回调守卫；真正授权仍由后端执行。 */
export function useProjectAccess(projectId: number) {
  const user = useAuthStore((s) => s.user)
  const status = useAuthStore((s) => s.status)
  const project = useProjectStore((s) => s.project)
  const version = useProjectStore((s) => s.scopeVersion)
  const identity = authorizationKey(user)
  const userId = user?.id
  const scope = useMemo(() => {
    if (status !== 'authenticated' || userId === undefined) return null
    const snapshot = captureProjectScope(projectId)
    return snapshot?.version === version && snapshot.actorId === userId
      && snapshot.identity.startsWith(`${userId}:${identity}:`) ? snapshot : null
  }, [projectId, version, status, userId, identity])
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const access = projectAccess(scope ? project : null, user, projectId)
  const current = useCallback((capability: ProjectCapability) => (
    mounted.current && isProjectScopeCurrent(scope, capability)
  ), [scope])
  const canReadNow = useCallback(() => current('canRead'), [current])
  const canEditNow = useCallback(() => current('canEdit'), [current])
  const canRecycleNow = useCallback(() => current('canRecycle'), [current])
  const canExportNow = useCallback(() => current('canExport'), [current])
  const canUseAINow = useCallback(() => current('canUseAI'), [current])
  const canUseAIEntitledNow = useCallback(() => current('canUseAIEntitled'), [current])
  const canUseOwnerAINow = useCallback(() => current('canUseOwnerAI'), [current])
  const canUseAgentNow = useCallback(() => current('canUseAgent'), [current])
  const canViewAgentNow = useCallback(() => current('canViewAgent'), [current])
  const canManageCollaboratorsNow = useCallback(() => current('canManageCollaborators'), [current])
  const canBackupNow = useCallback(() => current('canBackup'), [current])
  const canWriteReadingProgressNow = useCallback(() => current('canWriteReadingProgress'), [current])
  return {
    ...access, canReadNow, canEditNow, canRecycleNow, canExportNow, canUseAINow, canUseAIEntitledNow, canUseOwnerAINow,
    canUseAgentNow, canViewAgentNow, canManageCollaboratorsNow, canBackupNow, canWriteReadingProgressNow,
  }
}
