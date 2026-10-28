import { App } from 'antd'
import { useCallback, useEffect, useRef, useState } from 'react'
import { agentApi, withAgentRefresh } from '../../api/agent'
import { errorMessage } from '../../api/client'
import { isActiveTask, taskApi } from '../../api/tasks'
import type { AgentChange, AgentConversationDetail, AgentConversationUpdate, AgentSendRequest, Task } from '../../api/types'
import { useTaskStream } from '../../hooks/useTaskStream'
import { useProjectStore } from '../../stores/project'
import { useTasksStore } from '../../stores/tasks'
import { changedResources, mergeToolStep, upsertAgentChange, type AgentToolStep } from '../../utils/agent'
import { invalidateProject } from '../../utils/projectRefresh'

export function useAgentConversation(pid: number, cid: number, onUpdated: () => void) {
  const { message } = App.useApp()
  const [detail, setDetail] = useState<AgentConversationDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // 正在确认 / 拒绝 / 撤销的提案：只有这张卡片显示加载状态（外部调用确认最长约 30 秒）
  const [actingId, setActingId] = useState<number | null>(null)
  const [lastTask, setLastTask] = useState<Task | null>(null)
  const [steps, setSteps] = useState<AgentToolStep[]>([])
  const stream = useTaskStream()
  const { follow, detach } = stream
  const requestSeq = useRef(0)
  const alive = useRef(true)
  const following = useRef<number | null>(null)
  const track = useTasksStore((s) => s.track)

  const refresh = useCallback(async () => {
    const seq = ++requestSeq.current
    const next = await agentApi.get(pid, cid)
    if (alive.current && seq === requestSeq.current) { setDetail(next); setError(null) }
    return next
  }, [pid, cid])
  const refreshSafe = useCallback(() => {
    void refresh().catch((err: unknown) => { if (alive.current) setError(errorMessage(err)) })
  }, [refresh])
  const invalidate = useCallback((changes: AgentChange[]) => {
    invalidateProject(pid, changedResources(changes))
    void useProjectStore.getState().refresh()
    void useTasksStore.getState().refresh()
  }, [pid])
  const updateChange = useCallback((change: AgentChange) => {
    setDetail((prev) => prev ? { ...prev, changes: upsertAgentChange(prev.changes, change) } : prev)
  }, [])

  useEffect(() => {
    alive.current = true
    refreshSafe()
    return () => { alive.current = false; requestSeq.current += 1; detach() }
  }, [refreshSafe, detach])

  const latestTurnId = detail?.messages.filter((m) => m.turn_task_id !== null).at(-1)?.turn_task_id
  useEffect(() => {
    if (!latestTurnId) return
    let current = true
    void taskApi.get(latestTurnId).then((task) => { if (current) setLastTask(task) }).catch(() => { /* 已裁剪的历史任务不影响对话内容 */ })
    return () => { current = false }
  }, [latestTurnId, detail?.active_task?.status])
  const active = detail?.active_task
  useEffect(() => {
    if (!active || !isActiveTask(active.status) || following.current === active.id) return
    following.current = active.id
    void follow(active, {
      onSnapshot: refreshSafe,
      onToolStart: (e) => setSteps((prev) => mergeToolStep(prev, { ...e, turn_task_id: active.id })),
      onToolEnd: (e) => setSteps((prev) => mergeToolStep(prev, { ...e, turn_task_id: active.id })),
      onProposal: updateChange,
      onExecuted: (change) => { updateChange(change); invalidate([change]) },
    }).then(async (end) => {
      if (end.status === 'detached' || !alive.current) return
      if (end.status === 'lost') { following.current = null; return }
      try {
        const next = await refresh()
        if (!alive.current) return
        setSteps([])
        invalidate(next.changes.filter((c) => c.turn_task_id === active.id && c.status === 'executed'))
        onUpdated()
      } catch (err) { if (alive.current) setError(errorMessage(err)) }
      finally { following.current = null }
    })
  }, [active, follow, refreshSafe, refresh, updateChange, invalidate, onUpdated])

  // 待确认轮次可能在另一设备继续；重回页面也重新核对持久化历史。
  const activeId = active?.id
  useEffect(() => {
    const timer = activeId ? setInterval(refreshSafe, 5000) : undefined
    const onVisible = () => { if (document.visibilityState === 'visible') refreshSafe() }
    document.addEventListener('visibilitychange', onVisible)
    return () => { clearInterval(timer); document.removeEventListener('visibilitychange', onVisible) }
  }, [activeId, refreshSafe])

  const act = async (operation: () => Promise<unknown>, notice?: string) => {
    if (busy) return false
    setBusy(true)
    try {
      await withAgentRefresh(operation, async () => { await refresh() })
      const next = await refresh()
      invalidate(next.changes.filter((c) => c.status === 'executed' || c.status === 'undone'))
      onUpdated()
      if (notice) message.success(notice)
      return true
    } catch (err) { message.error(errorMessage(err)); return false }
    finally { if (alive.current) setBusy(false) }
  }
  const send = (body: AgentSendRequest) => act(async () => {
    const result = await agentApi.send(pid, cid, body)
    track(result.task)
    setSteps([])
    stream.reset()
  })
  const change = async (item: AgentChange, action: 'confirm' | 'reject' | 'undo') => {
    if (busy) return false
    setActingId(item.id)
    try {
      return await act(async () => {
        updateChange(await agentApi.change(pid, item.id, action))
      }, action === 'undo' ? '已撤销修改' : undefined)
    } finally { if (alive.current) setActingId(null) }
  }
  const turn = (id: number, action: 'confirm-all' | 'reject-all' | 'undo') => act(async () => {
    const result = await agentApi.turn(pid, id, action)
    result.changes.forEach(updateChange)
    if (result.task) track(result.task)
    if (result.error) message.warning(result.error)
    if (result.notice) message.info(result.notice)
  })
  const cancel = () => act(async () => {
    const task = detail?.active_task
    if (task) track(await taskApi.cancel(task.id))
  }, '已取消本轮对话')
  const retry = (task: Task) => act(async () => { track(await taskApi.retry(task.id)) })
  const update = (body: AgentConversationUpdate) => act(async () => { await agentApi.update(pid, cid, body) })

  return { detail, error, busy, actingId, steps, stream, lastTask: stream.task ?? lastTask, refresh: refreshSafe, send, change, turn, cancel, retry, update }
}
