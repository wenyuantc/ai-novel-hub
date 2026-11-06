import { useEffect, useMemo } from 'react'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'
import { authorizationKey } from '../../utils/permissions'
import { projectAccess } from '../../utils/projectAccess'
import { invalidateProject } from '../../utils/projectRefresh'
import { SettingTagsSession, type SettingTagsAccess } from './settingTagsController'
import { settingTagResources } from './tagDraft'

/** 页面本地会话通过 props 交给详情；条目切换不重建，actor/project 变化重建并清理。 */
export function useSettingTagsSession(projectId: number) {
  const actorId = useAuthStore((state) => state.user?.id)
  const status = useAuthStore((state) => state.status)
  const scope = useMemo(() => {
    const access = (): SettingTagsAccess => {
      const auth = useAuthStore.getState()
      const current = auth.status === 'authenticated' && auth.user?.id === actorId && status === 'authenticated'
        ? projectAccess(useProjectStore.getState().project, auth.user, projectId) : null
      return { identity: `${projectId}:${auth.status}:${auth.user?.id ?? 'none'}:${authorizationKey(auth.user)}`,
        canRead: current?.canRead === true, canEdit: current?.canEdit === true }
    }
    return { access, session: new SettingTagsSession(projectId, undefined, access,
      (entry) => invalidateProject(projectId, settingTagResources(entry.kind))) }
  }, [actorId, projectId, status])

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
