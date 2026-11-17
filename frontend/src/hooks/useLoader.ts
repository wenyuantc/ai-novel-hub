import { useCallback, useEffect, useRef, useState } from 'react'
import { useProjectRefresh } from './useProjectRefresh'
import type { ProjectResource } from '../utils/projectRefresh'
import { errorMessage } from '../api/client'

interface LoaderState<T> {
  data: T | undefined
  loading: boolean
  error: string | null
}

/**
 * 简单的异步数据加载：fn 需由调用方用 useCallback 固定，fn 变化时自动重新加载。
 */
export function useLoader<T>(fn: () => Promise<T>, scope?: { projectId: number; resources: readonly ProjectResource[] }) {
  const [state, setState] = useState<LoaderState<T>>({ data: undefined, loading: true, error: null })
  const [version, setVersion] = useState(0)
  const requestVersion = useRef(0)

  useEffect(() => {
    let alive = true
    const request = ++requestVersion.current
    fn().then(
      (data) => {
        if (alive && request === requestVersion.current) setState({ data, loading: false, error: null })
      },
      (err: unknown) => {
        if (alive && request === requestVersion.current) setState((s) => ({ ...s, loading: false, error: errorMessage(err) }))
      },
    )
    return () => {
      alive = false
    }
  }, [fn, version])

  const reload = useCallback(() => {
    requestVersion.current += 1
    setState((s) => ({ ...s, loading: true }))
    setVersion((v) => v + 1)
  }, [])

  useProjectRefresh(scope?.projectId, scope?.resources ?? [], reload)

  const setData = useCallback((updater: (prev: T | undefined) => T, options?: { invalidatePending?: boolean }) => {
    if (options?.invalidatePending) requestVersion.current += 1
    setState((s) => ({ ...s, data: updater(s.data), ...(options?.invalidatePending ? { loading: false } : {}) }))
  }, [])

  return { ...state, reload, setData }
}
