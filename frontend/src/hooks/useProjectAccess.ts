import { useAuthStore } from '../stores/auth'
import { useProjectStore } from '../stores/project'
import { projectAccess } from '../utils/projectAccess'
import { useCallback, useEffect, useRef } from 'react'
import { authorizationKey, can } from '../utils/permissions'

export function useProjectAccess(projectId: number) {
  const user = useAuthStore((s) => s.user)
  const project = useProjectStore((s) => s.project)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const access = projectAccess(project, user, projectId)
  const identity = authorizationKey(user)
  const current = useCallback((permission: 'projects.read' | 'projects.edit' | 'projects.recycle') => {
    const auth = useAuthStore.getState()
    if (!mounted.current || !access.ready || useProjectStore.getState().project?.id !== projectId || auth.status !== 'authenticated' || auth.user?.id !== user?.id) return false
    return !access.managingOther || (authorizationKey(auth.user) === identity && can(auth.user, permission))
  }, [access.ready, access.managingOther, identity, projectId, user?.id])
  const canEditNow = useCallback(() => current('projects.edit'), [current])
  const canReadNow = useCallback(() => current('projects.read'), [current])
  const canRecycleNow = useCallback(() => current('projects.recycle'), [current])
  return { ...access, canReadNow, canEditNow, canRecycleNow }
}
