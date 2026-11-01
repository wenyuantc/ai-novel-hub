import { useEffect, useRef, useState } from 'react'
import type { PermissionCode } from '../api/types'
import { useAuthStore } from '../stores/auth'
import { authorizationKey, can, isSuperAdmin } from '../utils/permissions'
import { createRecycleRequestGate } from '../utils/recycleBin'

/** 管理弹窗自己的请求代次，覆盖StrictMode、同actor撤权和卸载。 */
export function useAuthorizationScope(permission?: PermissionCode, superOnly = false, trackAuthorization = true) {
  const user = useAuthStore((state) => state.user)
  const key = (value: typeof user) => `${value?.id}:${trackAuthorization ? authorizationKey(value) : ''}`
  const identity = key(user)
  const [scope] = useState(() => ({ gate: createRecycleRequestGate() }))
  const active = useRef(false)
  useEffect(() => { active.current = true; return () => { active.current = false; scope.gate.invalidate() } }, [scope, identity])
  const allowed = () => {
    const auth = useAuthStore.getState()
    return active.current && auth.status === 'authenticated' && key(auth.user) === identity && (superOnly ? isSuperAdmin(auth.user) : !permission || can(auth.user, permission))
  }
  return { identity, allowed, begin: () => scope.gate.begin(), current: (ticket: number) => allowed() && scope.gate.isCurrent(ticket) }
}
