import { useCallback, useEffect, useRef, useState } from 'react'
import { currentRequestGeneration, watchRequestGeneration } from '../../api/client'
import { useAuthStore } from '../../stores/auth'
import { emptyFacts, FactsController, type FactsState } from './factsController'

/** 将请求代次、actor 和资源绑定；旧请求、卸载或会话切换后不落地。 */
export function useCollaborationFacts<T>(
  resourceKey: string, enabled: boolean, read: (signal: AbortSignal) => Promise<T>, onSettled?: () => void,
) {
  const actorId = useAuthStore((s) => s.status === 'authenticated' ? s.user?.id : undefined)
  const authorizationRevision = useAuthStore((s) => s.user?.authorization_revision)
  const [generation, setGeneration] = useState(currentRequestGeneration)
  const key = `${resourceKey}:${actorId ?? ''}:${authorizationRevision ?? ''}:${generation}:${enabled}`
  const [snapshot, setSnapshot] = useState<{ key: string; state: FactsState<T> }>({ key: '', state: emptyFacts<T>() })
  const current = useRef<{ key: string; controller: FactsController<T> } | null>(null)
  const settled = useRef(onSettled)
  useEffect(() => { settled.current = onSettled }, [onSettled])

  useEffect(() => {
    const watcher = watchRequestGeneration(() => {
      current.current?.controller.dispose()
      current.current = null
      setSnapshot({ key: '', state: emptyFacts<T>() })
      setGeneration(currentRequestGeneration())
    })
    return watcher.dispose
  }, [])

  useEffect(() => {
    if (!enabled || actorId === undefined) return
    const controller = new FactsController<T>(read, () => {
      const auth = useAuthStore.getState()
      return auth.status === 'authenticated' && auth.user?.id === actorId
    }, (state) => setSnapshot({ key, state }), () => settled.current?.())
    current.current = { key, controller }
    void controller.refresh()
    return () => {
      controller.dispose()
      if (current.current?.controller === controller) current.current = null
    }
  }, [actorId, enabled, key, read])

  const refresh = useCallback(() => {
    if (current.current?.key === key) void current.current.controller.refresh()
  }, [key])
  const mutate = useCallback((action: (signal: AbortSignal) => Promise<unknown>, success: string) => {
    if (current.current?.key !== key) return Promise.resolve(false)
    return current.current.controller.mutate(action, success)
  }, [key])
  const isCurrent = useCallback(() => current.current?.key === key && current.current.controller.isCurrent(), [key])
  const state = snapshot.key === key ? snapshot.state : emptyFacts<T>()
  return { ...state, identityKey: `${key}:${state.accessEpoch}`, refresh, mutate, isCurrent }
}
