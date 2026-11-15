import { useEffect, useMemo, useSyncExternalStore } from 'react'
import { currentRequestGeneration, watchRequestGeneration } from '../../api/client'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { useAuthStore } from '../../stores/auth'
import { captureProjectScope, isProjectScopeCurrent, useProjectStore, verifyProjectScope } from '../../stores/project'
import { authorizationKey } from '../../utils/permissions'
import { projectAccess } from '../../utils/projectAccess'
import { invalidateProject } from '../../utils/projectRefresh'
import { SettingTagsSession, type SettingTagsAccess } from './settingTagsController'
import { settingTagResources } from './tagDraft'

const subscribeSession = (notify: () => void) => watchRequestGeneration(notify).dispose

/** 页面本地会话通过 props 交给详情；条目切换不重建，actor/project 变化重建并清理。 */
export function useSettingTagsSession(projectId: number) {
  const actorId = useAuthStore((state) => state.user?.id)
  const status = useAuthStore((state) => state.status)
  const generation = useSyncExternalStore(subscribeSession, currentRequestGeneration, currentRequestGeneration)
  const scope = useMemo(() => {
    const access = (): SettingTagsAccess => {
      const auth = useAuthStore.getState()
      const projectScope = captureProjectScope(projectId)
      const current = auth.status === 'authenticated' && auth.user?.id === actorId && status === 'authenticated'
        && generation === currentRequestGeneration() && isProjectScopeCurrent(projectScope)
        ? projectAccess(useProjectStore.getState().project, auth.user, projectId) : null
      const owner = `${projectId}:${auth.status}:${auth.user?.id ?? 'none'}:${currentRequestGeneration()}`
      return { owner, identity: `${owner}:${authorizationKey(auth.user)}:${useProjectStore.getState().scopeVersion}`,
        canRead: current?.canRead === true, canEdit: current?.canEdit === true }
    }
    return { access, session: new SettingTagsSession(projectId, undefined, access,
      (entry) => invalidateProject(projectId, settingTagResources(entry.kind)), async (identity, afterFailure) => {
        if (access().identity !== identity) return 'stale'
        const result = await verifyProjectScope(captureProjectScope(projectId), afterFailure)
        return access().identity === identity ? result : 'stale'
      }) }
  }, [actorId, projectId, status, generation])

  useEffect(() => {
    let previous = JSON.stringify(scope.access())
    const check = () => {
      const next = JSON.stringify(scope.access())
      if (next !== previous) { previous = next; scope.session.accessChanged() }
    }
    const stopAuth = useAuthStore.subscribe(check)
    const stopProject = useProjectStore.subscribe(check)
    return () => { stopAuth(); stopProject(); scope.session.deactivate() }
  }, [scope])
  useProjectRefresh(projectId, ['setting-categories'], scope.session.refresh)
  return scope.session
}
