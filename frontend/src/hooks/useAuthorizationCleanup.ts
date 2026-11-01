import { useEffect } from 'react'
import { useAuthStore } from '../stores/auth'
import { authorizationKey } from '../utils/permissions'

/** 仅撤下当前页面拥有的目标/弹窗，不清理仍有读取权限的列表。 */
export function useAuthorizationCleanup(cleanup: () => void) {
  useEffect(() => {
    let identity = `${useAuthStore.getState().user?.id}:${authorizationKey(useAuthStore.getState().user)}`
    return useAuthStore.subscribe((auth) => {
      const next = `${auth.user?.id}:${authorizationKey(auth.user)}`
      if (next !== identity) { identity = next; cleanup() }
    })
  }, [cleanup])
}
