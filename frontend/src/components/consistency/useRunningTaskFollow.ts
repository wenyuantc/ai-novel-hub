import { App } from 'antd'
import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, errorMessage } from '../../api/client'
import { taskApi } from '../../api/tasks'
import type { Task, TaskStatus } from '../../api/types'
import { useTaskStream } from '../../hooks/useTaskStream'
import { useTasksStore } from '../../stores/tasks'
import { CHECK_POLL_MS, streamWatchAction, type TaskEndNotice } from '../../utils/consistency'
import { startVisiblePolling } from '../../utils/visiblePolling'

interface Options {
  /** 接口报告的进行中任务；runningKey 只在任务属于当前用户时有值（订阅进度需要） */
  runningId: number | null
  runningKey: string | null
  /** 报告进行中任务的接口是否已成功加载过（未加载时不能据此判断任务已结束） */
  loaded: boolean
  canUseAI: boolean
  /** 调用方的授权作用域仍有效 */
  isCurrent: () => boolean
  /** 重新读取报告进行中任务的接口 */
  reload: () => void
  endNotice: (status: TaskStatus, detail?: string) => TaskEndNotice | null
  /** 任务结束、订阅失败或轮询发现任务结束后调用（刷新数据） */
  onSettled: () => void
}

/**
 * 跟随一个「同作品同时只有一个」的后台任务，做法与一致性检查页相同：
 * - 进入页面时已有本人发起的任务在进行：读取任务后接上进度流；
 * - 有任务在进行就轮询接口：没有实时进度时（非发起者、订阅失败）它是唯一来源；订阅中也保留，作为订阅流被中断、
 *   报错或长时间没有事件时的兜底；
 * - 订阅仍显示进行中、接口却已不把该任务列为进行中：读取任务真实状态，确认结束后断开订阅并刷新。
 */
export function useRunningTaskFollow({ runningId, runningKey, loaded, canUseAI, isCurrent, reload, endNotice, onSettled }: Options) {
  const { message } = App.useApp()
  const stream = useTaskStream()
  const { follow, detach } = stream
  const trackTask = useTasksStore((state) => state.track)
  const [lost, setLost] = useState<string | null>(null)
  const [cancelling, setCancelling] = useState(false)
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  // 已接上（或正在接上）的任务身份，避免接口每次刷新都重复订阅
  const attachedRef = useRef('')
  const settledRef = useRef(onSettled)
  const noticeRef = useRef(endNotice)
  useEffect(() => { settledRef.current = onSettled; noticeRef.current = endNotice })

  const attach = useCallback(async (task: Task) => {
    attachedRef.current = `${task.id}:${task.task_key ?? ''}`
    setLost(null)
    const outcome = await follow(task)
    if (!alive.current || outcome.status === 'detached') return
    // 订阅失败（如协作者领取瞬间的 permission_denied）：回退到轮询，并允许手动重新连接
    if (outcome.status === 'lost') setLost(outcome.message)
    else {
      const notice = noticeRef.current(outcome.status, 'detail' in outcome ? outcome.detail : '')
      if (notice) message[notice.level](notice.text)
    }
    settledRef.current()
  }, [follow, message])

  useEffect(() => {
    if (runningId === null || runningKey === null || !canUseAI) return
    const key = `${runningId}:${runningKey}`
    if (attachedRef.current === key) return
    attachedRef.current = key
    const controller = new AbortController()
    let handed = false
    void taskApi.get({ id: runningId, task_key: runningKey }, controller.signal).then(
      (task) => { if (!controller.signal.aborted) { handed = true; void attach(task) } },
      (err: unknown) => { if (!controller.signal.aborted) { handed = true; setLost(errorMessage(err)) } },
    )
    return () => {
      controller.abort()
      if (!handed && attachedRef.current === key) attachedRef.current = ''
    }
  }, [runningId, runningKey, canUseAI, attach])

  const streaming = stream.running
  useEffect(() => {
    if (runningId === null && !streaming) return
    return startVisiblePolling(reload, document, CHECK_POLL_MS)
  }, [runningId, streaming, reload])

  const followed = streaming ? stream.task : null
  const followedId = followed?.id ?? null
  const followedKey = followed?.task_key ?? null
  const verifyingRef = useRef(false)
  const streamingRef = useRef(false)
  useEffect(() => { streamingRef.current = streaming }, [streaming])
  useEffect(() => {
    const action = streamWatchAction({ streaming, followedTaskId: followedId, summaryLoaded: loaded, runningTaskId: runningId })
    if (action !== 'verify' || followedId === null || followedKey === null || verifyingRef.current) return
    verifyingRef.current = true
    const identity = `${followedId}:${followedKey}`
    void taskApi.get({ id: followedId, task_key: followedKey }).then((task) => {
      // 期间订阅自己结束或已换成别的任务：由订阅结果处理
      if (!alive.current || !streamingRef.current || attachedRef.current !== identity) return
      const notice = noticeRef.current(task.status, task.error ?? '')
      if (!notice) return
      detach()
      message[notice.level](notice.text)
      settledRef.current()
    }, () => { /* 读取失败：下一次轮询再核对 */ }).finally(() => { verifyingRef.current = false })
    // loaded / runningId 每次轮询后都可能变化：接口仍未列出该任务时下一轮继续核对
  }, [streaming, followedId, followedKey, loaded, runningId, detach, message])

  // 轮询发现任务结束（非发起者或订阅失败时的唯一信号）
  const previousRunning = useRef<number | null>(null)
  useEffect(() => {
    const before = previousRunning.current
    previousRunning.current = runningId
    if (before !== null && runningId === null && !streaming) settledRef.current()
  }, [runningId, streaming])

  /** 本页刚创建了任务：登记到任务面板、刷新接口并接上进度流 */
  const started = useCallback((task: Task) => {
    trackTask(task)
    reload()
    void attach(task)
  }, [trackTask, reload, attach])

  const retrySubscribe = useCallback(() => { attachedRef.current = ''; setLost(null); reload() }, [reload])

  const streamTask = stream.task
  const cancel = useCallback(async () => {
    const identity = streamTask ?? (runningId !== null && runningKey !== null ? { id: runningId, task_key: runningKey } : null)
    if (!identity || !isCurrent()) return
    setCancelling(true)
    try {
      const next = await taskApi.cancel(identity)
      if (alive.current && isCurrent()) trackTask(next)
    } catch (err) {
      // 409 = 任务已结束：订阅或轮询会给出真实终态
      if (alive.current && !(err instanceof ApiError && err.status === 409)) message.error(errorMessage(err))
    } finally {
      if (alive.current) { setCancelling(false); reload() }
    }
  }, [streamTask, runningId, runningKey, isCurrent, trackTask, message, reload])

  return {
    /** 实时订阅中的任务；没有订阅时为 null */
    task: streaming ? stream.task : null,
    streaming,
    reconnecting: stream.reconnecting,
    /** 有任务在进行（接口报告或本页正在订阅） */
    busy: runningId !== null || streaming,
    /** 任务属于当前用户（可取消、可重新订阅） */
    owns: runningKey !== null || streaming,
    lost: streaming ? null : lost,
    cancelling,
    started,
    cancel,
    retrySubscribe,
  }
}
