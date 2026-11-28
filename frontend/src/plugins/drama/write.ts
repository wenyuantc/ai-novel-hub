import { ApiError, currentRequestGeneration, request, type RequestOptions } from '../../api/client'
import { invalidateMismatchedEditorActor } from '../../components/editorDrafts/verifyDraftScope'
import { useAuthStore } from '../../stores/auth'

/** 浏览器各标签页共享 Cookie。涉及个人配置或内容写入的请求携带本页所见的用户身份。 */
export async function dramaActorRequest<T>(path: string, options: RequestOptions): Promise<T> {
  const user = useAuthStore.getState().user
  if (!user?.created_at) throw new ApiError(401, '登录状态已变化，请重新登录')
  const generation = currentRequestGeneration()
  const isCurrent = () => {
    const current = useAuthStore.getState().user
    return (
      generation === currentRequestGeneration() &&
      current?.id === user.id &&
      current.created_at === user.created_at &&
      (options.isCurrent?.() ?? true)
    )
  }
  try {
    if (!isCurrent()) throw new DOMException('已离开编辑页面', 'AbortError')
    const result = await request<T>(path, {
      ...options,
      isCurrent,
      editorActor: { id: user.id, createdAt: user.created_at },
    })
    if (!isCurrent()) throw new DOMException('已离开编辑页面', 'AbortError')
    return result
  } catch (error) {
    invalidateMismatchedEditorActor(error, isCurrent)
    throw error
  }
}

export function dramaWrite<T>(path: string, options: RequestOptions & { method: 'POST' | 'PUT' | 'PATCH' | 'DELETE' }) {
  return dramaActorRequest<T>(path, options)
}
