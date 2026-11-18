import { authApi } from '../../api/auth'
import { ApiError } from '../../api/client'
import { useAuthStore } from '../../stores/auth'
import { isProjectScopeCurrent, verifyProjectScope, type ProjectScope } from '../../stores/project'
import { authorizationKey } from '../../utils/permissions'

/** 跨标签页可能更换 Cookie，不能用旧的前端身份快照展示本机草稿。 */
export async function verifyDraftScope(
  scope: ProjectScope | null,
  isCurrent: () => boolean,
  signal?: AbortSignal,
): Promise<boolean> {
  const current = () => !signal?.aborted && isCurrent() && isProjectScopeCurrent(scope)
  const original = useAuthStore.getState().user
  if (!scope || !original || !current()) return false
  try {
    const actual = await authApi.me(signal)
    if (!current()) return false
    if (actual.id !== original.id || actual.created_at !== original.created_at) {
      useAuthStore.getState().clear()
      return false
    }
    if (authorizationKey(actual) !== authorizationKey(original)) {
      useAuthStore.getState().applyAuthorization(actual)
      return false
    }
    return await verifyProjectScope(scope) === 'confirmed' && current()
  } catch (error) {
    if (current() && error instanceof ApiError && error.status === 401) useAuthStore.getState().clear()
    return false
  }
}

export function invalidateMismatchedEditorActor(error: unknown, isCurrent: () => boolean): void {
  if (error instanceof ApiError && error.body?.code === 'editor_actor_changed' && isCurrent()) {
    useAuthStore.getState().clear()
  }
}
