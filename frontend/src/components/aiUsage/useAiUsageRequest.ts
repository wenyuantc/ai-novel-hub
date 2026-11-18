import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { App } from 'antd'
import { currentRequestGeneration, errorMessage, watchRequestGeneration, type EditorActorIdentity, type RequestOptions } from '../../api/client'
import type { PermissionCode } from '../../api/types'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey, can } from '../../utils/permissions'

const subscribeSession = (notify: () => void) => watchRequestGeneration(notify).dispose

export interface UsageSession {
  key: string
  actor: EditorActorIdentity
  current: () => boolean
}

/** 页面键与请求落地都核对会话、账号实例和权限版本。 */
export function useAiUsageSession(permission?: PermissionCode): UsageSession | null {
  const user = useAuthStore((state) => state.user)
  const status = useAuthStore((state) => state.status)
  const generation = useSyncExternalStore(subscribeSession, currentRequestGeneration, currentRequestGeneration)
  const id = user?.id
  const createdAt = user?.created_at
  const authorized = !permission || can(user, permission)
  const authorization = authorizationKey(user)
  return useMemo(() => {
    if (id === undefined || createdAt === undefined || status !== 'authenticated' || !authorized) return null
    return {
      key: `${generation}:${id}:${createdAt}:${authorization}`,
      actor: { id, createdAt },
      current: () => {
        const now = useAuthStore.getState()
        return generation === currentRequestGeneration() && now.status === 'authenticated'
          && now.user?.id === id && now.user.created_at === createdAt && authorizationKey(now.user) === authorization
          && (!permission || can(now.user, permission))
      },
    }
  }, [id, createdAt, status, authorized, authorization, generation, permission])
}

export type UsageReadOptions = Required<Pick<RequestOptions, 'signal' | 'isCurrent'>>
type WriteOptions = UsageReadOptions & { editorActor: EditorActorIdentity }
type Loader<T> = (options: UsageReadOptions) => Promise<T>

export function useAiUsageRequest<T>(load: Loader<T>, session: UsageSession, canAccess?: () => boolean) {
  const [revision, setRevision] = useState(0)
  const [result, setResult] = useState<{ load: Loader<T>; revision: number; data?: T; error?: string }>()
  useEffect(() => {
    const controller = new AbortController()
    const isCurrent = () => !controller.signal.aborted && session.current() && (canAccess?.() ?? true)
    if (isCurrent()) {
      void load({ signal: controller.signal, isCurrent }).then(
        (data) => { if (isCurrent()) setResult({ load, revision, data }) },
        (error: unknown) => { if (isCurrent()) setResult({ load, revision, error: errorMessage(error) }) },
      )
    }
    return () => controller.abort()
  }, [load, revision, session, canAccess])
  const current = result?.load === load && result.revision === revision ? result : undefined
  return { data: current?.data, error: current?.error, loading: current === undefined, refresh: () => setRevision((value) => value + 1) }
}

export function useAiUsageMutation(session: UsageSession, canAccess?: () => boolean) {
  const { message } = App.useApp()
  const pending = useRef<AbortController | null>(null)
  const [loading, setLoading] = useState(false)
  useEffect(() => () => pending.current?.abort(), [session, canAccess])

  const commit = async <T,>(write: (options: WriteOptions) => Promise<T>, onSuccess: (value: T) => void, success: string, onConflict?: () => void): Promise<void> => {
    if (pending.current || !session.current() || !(canAccess?.() ?? true)) return
    const controller = new AbortController()
    const isCurrent = () => !controller.signal.aborted && session.current() && (canAccess?.() ?? true)
    pending.current = controller
    setLoading(true)
    try {
      const value = await write({ signal: controller.signal, isCurrent, editorActor: session.actor })
      if (isCurrent()) {
        onSuccess(value)
        message.success(success)
      }
    } catch (error) {
      if (isCurrent()) {
        message.error(errorMessage(error))
        if (error instanceof Error && 'status' in error && error.status === 409) onConflict?.()
      }
    } finally {
      pending.current = null
      if (isCurrent()) setLoading(false)
    }
  }
  return { loading, commit }
}
