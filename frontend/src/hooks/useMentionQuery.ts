import { useCallback, useEffect, useState } from 'react'
import { errorMessage } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { authorizationKey } from '../utils/permissions'

/** 结果绑定作品、授权快照和查询；切换时旧内容立即不可见，晚到响应不回填。 */
export function useMentionQuery<T>(projectId: number, queryKey: string, load: (signal: AbortSignal) => Promise<T>, enabled = true, delay = 0) {
  const identity = useAuthStore((s) => `${s.status}:${s.user?.id ?? ''}:${authorizationKey(s.user)}`)
  const [version, setVersion] = useState(0)
  const key = `${projectId}:${identity}:${queryKey}:${version}`
  const [state, setState] = useState<{ key: string; data?: T; error?: string }>({ key: '' })
  useEffect(() => {
    if (!enabled) return
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      void load(controller.signal).then((data) => { if (!controller.signal.aborted) setState({ key, data }) }).catch((err: unknown) => { if (!controller.signal.aborted) setState({ key, error: errorMessage(err) }) })
    }, delay)
    return () => { window.clearTimeout(timer); controller.abort() }
  }, [key, load, enabled, delay])
  const current = enabled && state.key === key ? state : undefined
  const reload = useCallback(() => setVersion((v) => v + 1), [])
  return { data: current?.data, error: current?.error, loading: enabled && !current, reload }
}
