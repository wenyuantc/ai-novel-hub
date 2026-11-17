import { useCallback, useEffect, useState } from 'react'
import { errorMessage } from '../../api/client'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey } from '../../utils/permissions'

/**
 * 结果绑定作品、授权快照与查询：切换其一时旧内容立即不可见，迟到响应不回填（中止 + 作用域比对）。
 * 与 useMentionQuery 的区别：同一作用域内 reload（任务轮询、保存后刷新）保留已显示的数据，只标记 refreshing，避免整页闪烁。
 */
export function useConsistencyQuery<T>(projectId: number, queryKey: string, load: (signal: AbortSignal) => Promise<T>, enabled = true) {
  const identity = useAuthStore((s) => `${s.status}:${s.user?.id ?? ''}:${authorizationKey(s.user)}`)
  const [version, setVersion] = useState(0)
  const scope = `${projectId}:${identity}:${queryKey}`
  const [state, setState] = useState<{ scope: string; version: number; data?: T; error?: string }>({ scope: '', version: -1 })
  useEffect(() => {
    if (!enabled) return
    const controller = new AbortController()
    void load(controller.signal).then(
      (data) => { if (!controller.signal.aborted) setState({ scope, version, data }) },
      (err: unknown) => {
        if (controller.signal.aborted) return
        // 刷新失败保留同一作用域内已显示的数据，只附带错误
        setState((prev) => ({ scope, version, data: prev.scope === scope ? prev.data : undefined, error: errorMessage(err) }))
      },
    )
    return () => controller.abort()
  }, [scope, version, load, enabled])
  const current = enabled && state.scope === scope ? state : undefined
  const reload = useCallback(() => setVersion((v) => v + 1), [])
  return {
    data: current?.data,
    error: current?.error,
    loading: enabled && !current,
    refreshing: !!current && current.version !== version,
    reload,
  }
}
