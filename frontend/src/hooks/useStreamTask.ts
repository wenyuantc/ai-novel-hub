import { useCallback, useEffect, useRef, useState } from 'react'
import { errorMessage } from '../api/client'
import { isAbortError } from '../api/sse'

export type StreamOutcome<T> = { status: 'done'; value: T } | { status: 'aborted' } | { status: 'error'; message: string }

/**
 * 管理一次流式生成任务：累积增量文本、运行状态、AbortController 中止。
 * 组件卸载时自动中止进行中的请求。
 */
export function useStreamTask() {
  const [text, setText] = useState('')
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const controllerRef = useRef<AbortController | null>(null)

  useEffect(() => () => controllerRef.current?.abort(), [])

  const start = useCallback(
    async <T>(runner: (onDelta: (delta: string) => void, signal: AbortSignal) => Promise<T>): Promise<StreamOutcome<T>> => {
      controllerRef.current?.abort()
      const controller = new AbortController()
      controllerRef.current = controller
      setText('')
      setError(null)
      setRunning(true)
      try {
        const value = await runner((delta) => setText((prev) => prev + delta), controller.signal)
        return { status: 'done', value }
      } catch (err) {
        if (isAbortError(err) || controller.signal.aborted) return { status: 'aborted' }
        const message = errorMessage(err)
        setError(message)
        return { status: 'error', message }
      } finally {
        if (controllerRef.current === controller) {
          controllerRef.current = null
          setRunning(false)
        }
      }
    },
    [],
  )

  const stop = useCallback(() => {
    controllerRef.current?.abort()
  }, [])

  const reset = useCallback(() => {
    setText('')
    setError(null)
  }, [])

  return { text, running, error, start, stop, reset }
}
