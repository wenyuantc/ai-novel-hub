import { useEffect, useMemo } from 'react'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'
import { MENTION_DICTIONARY_RESOURCES } from '../../utils/mentions'
import { authorizationKey } from '../../utils/permissions'
import { projectAccess } from '../../utils/projectAccess'
import { invalidateProject } from '../../utils/projectRefresh'
import { SettingAiSession, type SettingAiAccess } from './settingAiController'

/** 一个页面内的条目切换保留各自草稿，退出项目／账号则彻底清理。 */
export function useSettingAiSession(projectId: number) {
  const actorId = useAuthStore((state) => state.user?.id)
  const status = useAuthStore((state) => state.status)
  const scope = useMemo(() => {
    const access = (): SettingAiAccess => {
      const auth = useAuthStore.getState()
      const current = auth.status === 'authenticated' && auth.user?.id === actorId && status === 'authenticated'
        ? projectAccess(useProjectStore.getState().project, auth.user, projectId) : null
      return { identity: `${projectId}:${auth.status}:${auth.user?.id ?? 'none'}:${authorizationKey(auth.user)}`,
        canRead: current?.canRead === true, canEdit: current?.canEdit === true }
    }
    return { access, session: new SettingAiSession(projectId, access,
      () => invalidateProject(projectId, ['setting-ai-visibility'])) }
  }, [actorId, projectId, status])

  useEffect(() => {
    let previous = JSON.stringify(scope.access())
    const check = () => {
      const next = JSON.stringify(scope.access())
      if (next !== previous) { previous = next; scope.session.accessChanged() }
    }
    const stopAuth = useAuthStore.subscribe(check)
    const stopProject = useProjectStore.subscribe(check)
    return () => { stopAuth(); stopProject(); scope.session.dispose() }
  }, [scope])
  useProjectRefresh(projectId, MENTION_DICTIONARY_RESOURCES, scope.session.refresh)
  return scope.session
}
