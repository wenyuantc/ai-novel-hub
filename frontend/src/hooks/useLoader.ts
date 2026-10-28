import { useCallback, useEffect, useState } from 'react'
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

  useEffect(() => {
    let alive = true
    fn().then(
      (data) => {
        if (alive) setState({ data, loading: false, error: null })
      },
      (err: unknown) => {
        if (alive) setState((s) => ({ ...s, loading: false, error: errorMessage(err) }))
      },
    )
    return () => {
      alive = false
    }
  }, [fn, version])

  const reload = useCallback(() => {
    setState((s) => ({ ...s, loading: true }))
    setVersion((v) => v + 1)
  }, [])

  useProjectRefresh(scope?.projectId, scope?.resources ?? [], reload)

  const setData = useCallback((updater: (prev: T | undefined) => T) => {
    setState((s) => ({ ...s, data: updater(s.data) }))
  }, [])

  return { ...state, reload, setData }
}
