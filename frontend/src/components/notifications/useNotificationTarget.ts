import { useEffect, useState } from 'react'
import { ApiError, errorMessage } from '../../api/client'
import { startForegroundPolling } from '../../utils/visiblePolling'

type Result<T> = { data: T } | { missing: true } | { error: string }

/** 详情独立复验权限。离开页面时中止请求，切换目标时立即隐藏上一条结果。 */
export function useNotificationTarget<T>(read: (signal: AbortSignal) => Promise<T>) {
  const [state, setState] = useState<{ read: typeof read; result: Result<T> }>()
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    let controller: AbortController
    const refresh = () => {
      controller?.abort()
      controller = new AbortController()
      const { signal } = controller
      const settle = (result: Result<T>) => { if (!signal.aborted) setState({ read, result }) }
      void read(signal).then((data) => settle({ data }), (error: unknown) => {
        settle(error instanceof ApiError && [403, 404, 409].includes(error.status) ? { missing: true } : { error: errorMessage(error) })
      })
    }
    refresh()
    const stop = startForegroundPolling(refresh, document, window, 30_000)
    return () => { stop(); controller.abort() }
  }, [read, attempt])
  return { result: state?.read === read ? state.result : undefined, retry: () => setAttempt((v) => v + 1) }
}
