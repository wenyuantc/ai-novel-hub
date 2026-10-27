import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, errorMessage } from '../api/client'
import { isAbortError } from '../api/sse'
import { subscribeTask, taskApi, type TaskEnd, type TaskStreamHandlers } from '../api/tasks'
import type { ContinueEvent, Task } from '../api/types'

/**
 * follow 的结果：
 * - succeeded / failed / cancelled / interrupted：任务已结束（TaskEnd）
 * - paused：多步任务到达审核点（待审核），订阅结束，任务可继续 / 结束
 * - detached：本地断开订阅（再次 follow / detach / 卸载），任务仍在后台继续
 * - lost：订阅失败（任务被删除 404、重连耗尽等），任务状态未知
 */
export type TaskOutcome = TaskEnd | { status: 'detached' } | { status: 'lost'; message: string }

/** follow 的附加回调（批量生成的章节事件等）；在 hook 自身处理之后调用，仅当前订阅有效时触发 */
export type TaskFollowExtra = Pick<TaskStreamHandlers, 'onSnapshot' | 'onChapterStart' | 'onChapterEnd' | 'onBatchEnd'>

/**
 * 订阅一个后台任务：累积输出文本（snapshot 覆盖）、实时 task 状态 / 进度 / 排队位置、续写轮次。
 * 卸载或 detach 只断开订阅，**不取消任务**；取消请用 cancel()。
 */
export function useTaskStream() {
  const [task, setTask] = useState<Task | null>(null)
  const [text, setText] = useState('')
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [continueInfo, setContinueInfo] = useState<ContinueEvent | null>(null)
  const [reconnecting, setReconnecting] = useState(false)
  const controllerRef = useRef<AbortController | null>(null)
  const taskIdRef = useRef<number | null>(null)

  useEffect(() => () => controllerRef.current?.abort(), [])

  const follow = useCallback(async (initial: Task, extra?: TaskFollowExtra): Promise<TaskOutcome> => {
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    taskIdRef.current = initial.id
    setTask(initial)
    setText('')
    setError(null)
    setContinueInfo(null)
    setReconnecting(false)
    setRunning(true)
    const isCurrent = () => controllerRef.current === controller
    try {
      const end = await subscribeTask(
        initial.id,
        {
          onSnapshot: (t, snapshotText) => {
            if (!isCurrent()) return
            setReconnecting(false)
            setText(snapshotText)
            if (t) setTask(t)
            extra?.onSnapshot?.(t, snapshotText)
          },
          onDelta: (delta) => {
            if (isCurrent()) setText((prev) => prev + delta)
          },
          onReset: () => {
            if (!isCurrent()) return
            setText('')
            setContinueInfo(null)
          },
          onStage: (stage) => {
            if (isCurrent()) setTask((prev) => (prev ? { ...prev, stage } : prev))
          },
          onChapterStart: (e) => {
            if (isCurrent()) extra?.onChapterStart?.(e)
          },
          onChapterEnd: (e) => {
            if (isCurrent()) extra?.onChapterEnd?.(e)
          },
          onBatchEnd: (e) => {
            if (isCurrent()) extra?.onBatchEnd?.(e)
          },
          onContinue: (e) => {
            if (isCurrent()) setContinueInfo(e)
          },
          onProgress: (p) => {
            if (!isCurrent()) return
            setTask((prev) =>
              prev
                ? {
                    ...prev,
                    status: p.status,
                    progress: p.progress,
                    stage: p.stage,
                    queue_position:
                      p.queue_position !== undefined ? p.queue_position : p.status === 'queued' ? prev.queue_position : null,
                  }
                : prev,
            )
          },
          onReconnect: () => {
            if (isCurrent()) setReconnecting(true)
          },
        },
        controller.signal,
      )
      if (!isCurrent()) return { status: 'detached' }
      if (end.task) setTask(end.task)
      else setTask((prev) => (prev ? { ...prev, status: end.status } : prev))
      if (end.status === 'failed' || end.status === 'interrupted') setError(end.detail)
      return end
    } catch (err) {
      if (isAbortError(err) || controller.signal.aborted || !isCurrent()) return { status: 'detached' }
      const message = errorMessage(err)
      setError(message)
      return { status: 'lost', message }
    } finally {
      if (isCurrent()) {
        controllerRef.current = null
        setRunning(false)
        setReconnecting(false)
        setContinueInfo(null)
      }
    }
  }, [])

  /** 只断开订阅（任务继续） */
  const detach = useCallback(() => {
    controllerRef.current?.abort()
  }, [])

  /**
   * 取消当前任务。订阅随后收到 error（status = cancelled）并结束 follow。
   * 任务已结束（409）时静默——订阅会收到真实的终止事件；其他错误抛出。
   */
  const cancel = useCallback(async () => {
    const id = taskIdRef.current
    if (id === null) return
    try {
      await taskApi.cancel(id)
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) return
      throw err
    }
  }, [])

  const reset = useCallback(() => {
    setText('')
    setError(null)
    setTask(null)
  }, [])

  return { task, text, running, error, continueInfo, reconnecting, follow, detach, cancel, reset }
}
