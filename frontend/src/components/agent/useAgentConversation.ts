import { App } from 'antd'
import { useCallback, useEffect, useRef, useState } from 'react'
import { agentApi, isAgentAccessLost, isAgentHistorySealed, withAgentRefresh, type AgentTurnAction } from '../../api/agent'
import { ApiError, errorMessage } from '../../api/client'
import { hasTaskIdentity, isActiveTask, isOpenTask, taskApi, type TaskIdentity } from '../../api/tasks'
import type { AgentChange, AgentConversationDetail, AgentConversationUpdate, AgentSendRequest, Task } from '../../api/types'
import { useTaskStream } from '../../hooks/useTaskStream'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { captureProjectScope, isProjectScopeCurrent, useProjectStore } from '../../stores/project'
import { useTasksStore } from '../../stores/tasks'
import {
  AGENT_RETRY_REJECTED_HINT, agentReadOnlyNotice, agentRetryState, changedResources, mergeToolStep, upsertAgentChange,
  type AgentToolStep,
} from '../../utils/agent'
import { invalidateProject } from '../../utils/projectRefresh'
import { startVisiblePolling, type PollingVisibility } from '../../utils/visiblePolling'

/** 未结束轮次的兜底可见轮询间隔（跨标签页确认与流异常兜底；实时进度由 SSE 推送）。 */
export const AGENT_TURN_FALLBACK_POLL_MS = 5000
/** 写入被拒、正在按服务端事实核对权限时的只读提示。 */
export const AGENT_ACCESS_RECHECK_TEXT = '协作权限已变化，正在重新核对，暂不能发送或确认。'
/** 会话已不可访问（撤权 / 删除）时的提示。 */
export const AGENT_ACCESS_LOST_TEXT = '会话已不可访问，可能已失去协作授权。'

/** 协作者通用重试被拒：界面统一提示，不展示 403 / 400 / 409 各自的文案（裁决 24）。 */
class AgentRetryRejected extends Error {
  readonly original: unknown
  constructor(original: unknown) { super(AGENT_RETRY_REJECTED_HINT); this.original = original }
}

const turnKey = (task: Pick<Task, 'id' | 'task_key'>) => `${task.id}:${task.task_key ?? ''}`

/** 会话读取被更新的读取取代（不是失败）：静默丢弃，不覆盖新读取的结果或错误。 */
export class AgentReadSuperseded extends Error {
  constructor() { super('作品权限已变化，请重新读取对话。'); this.name = 'AgentReadSuperseded' }
}

export interface AgentAccessRecoveryDeps {
  alive: () => boolean
  setLost: (lost: boolean) => void
  /** 停止订阅与轮询副作用（轮询随 lost 状态自动停止）。 */
  stop: () => void
  refreshProject: () => void
  /** 权威重读会话详情。 */
  reread: () => Promise<unknown>
  /** 重读仍 404 / 409 permission_denied：会话已不可访问。 */
  onGone: () => void
  onError: (err: unknown) => void
}

/**
 * 中途失权（写入 409 permission_denied / 读取 404）的恢复：只进入一次「核对中」，停止订阅与轮询、刷新作品权限、
 * 权威重读一次。重读成功即以服务端 writable 恢复；仍不可访问则保持停止（不会自动循环重试）；
 * 网络等暂时失败保持停止，之后任何一次成功的权威读取（手动刷新、回前台）调用 settle 恢复。
 */
export function createAgentAccessRecovery(deps: AgentAccessRecoveryDeps) {
  let lost = false
  const settle = () => {
    if (!lost || !deps.alive()) return
    lost = false
    deps.setLost(false)
  }
  return {
    isLost: () => lost,
    settle,
    async markLost() {
      if (lost) return
      lost = true
      deps.setLost(true)
      deps.stop()
      deps.refreshProject()
      try {
        await deps.reread()
        settle()
      } catch (err) {
        if (!deps.alive() || err instanceof AgentReadSuperseded) return
        if (isAgentAccessLost(err)) deps.onGone()
        else deps.onError(err)
      }
    },
  }
}

/**
 * 本轮进度来源：作者与协作者（具备智能体使用权限）均对活动轮次（queued / running）实时订阅（stream）；
 * 待审核（paused）与已结束轮次不订阅，中途失去权限（lost）后立即停止。
 */
export function agentProgressMode(_collaborator: boolean, lost: boolean, active: Pick<Task, 'status'> | null | undefined): 'stream' | 'poll' | 'none' {
  if (lost || !active) return 'none'
  return isActiveTask(active.status) ? 'stream' : 'none'
}

/**
 * 会话详情的刷新节奏：存在未结束轮次（含待确认跨标签页确认及流异常兜底）时仅在页面可见时每 5 秒核对一次，
 * 切回前台立即刷新；无未结束轮次时只在回到前台时刷新。失去权限（lost）后不启动。返回值完整清理。
 */
export function startAgentTurnPolling(
  refresh: () => void, input: { collaborator: boolean; lost: boolean; activeId: number | undefined; activeOpen: boolean },
  visibility: PollingVisibility,
): (() => void) | undefined {
  if (input.lost) return undefined
  if (input.activeId && input.activeOpen) return startVisiblePolling(refresh, visibility, AGENT_TURN_FALLBACK_POLL_MS)
  const onVisible = () => { if (visibility.visibilityState === 'visible') refresh() }
  visibility.addEventListener('visibilitychange', onVisible)
  return () => { visibility.removeEventListener('visibilitychange', onVisible) }
}

export interface AgentActionResult {
  /** 服务端已受理本次操作（请求成功返回）。 */
  accepted: boolean
  /** 受理后的权威重读也已完成并应用。 */
  ok: boolean
}

export interface AgentActionDeps {
  operation: () => Promise<unknown>
  /** 写入被拒（403 / 404 / 409 / 422 / 身份缺失）后按服务端事实重读一次，不重放操作。 */
  refreshOnReject: () => Promise<void>
  /** 本次操作所在的挂载与作品作用域仍有效。 */
  current: () => boolean
  reread: () => Promise<AgentConversationDetail>
  settle: (next: AgentConversationDetail) => void
  fail: (err: unknown) => void
}

/**
 * 执行一次智能体写操作并重读会话。请求一旦成功返回即视为服务端已受理（accepted）：之后的重读被更新的读取取代、
 * 重读失败或作用域失效都只影响 ok，不能让调用方把已发送内容当作未发送（E4i-h：发送后输入框偶发未清空）。
 * 被取代的重读最多再读两次，确保已执行修改的资源失效与会话列表刷新照常进行；卸载 / 作用域失效时静默。
 */
export async function runAgentAction(deps: AgentActionDeps): Promise<AgentActionResult> {
  let accepted = false
  try {
    await withAgentRefresh(async () => {
      const result = await deps.operation()
      accepted = true
      return result
    }, deps.refreshOnReject)
    if (!deps.current()) return { accepted, ok: false }
    let next: AgentConversationDetail | undefined
    for (let attempt = 0; next === undefined; attempt += 1) {
      try { next = await deps.reread() }
      catch (err) { if (!(err instanceof AgentReadSuperseded) || attempt >= 2 || !deps.current()) throw err }
    }
    if (!deps.current()) return { accepted, ok: false }
    deps.settle(next)
    return { accepted, ok: true }
  } catch (err) {
    if (!(accepted && err instanceof AgentReadSuperseded)) deps.fail(err)
    return { accepted, ok: false }
  }
}

export function useAgentConversation(pid: number, cid: number, onUpdated: () => void) {
  const { message } = App.useApp()
  const access = useProjectAccess(pid)
  const collaborator = access.isCollaborator
  // 权限快照就绪且具备智能体使用权限（作者或 accepted editor）时才订阅实时流，避免未就绪或 viewer 误发请求。
  const canStream = access.ready && access.canUseAgent
  const [detail, setDetail] = useState<AgentConversationDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [sealedError, setSealedError] = useState(false)
  // 中途失去写权限 / 会话不可见：停止订阅与轮询，等权威读取或作品权限刷新决定只读还是离开。
  const [lost, setLost] = useState(false)
  const [rejectedRetry, setRejectedRetry] = useState<string | null>(null)
  // 正在确认 / 拒绝 / 撤销的提案：只有这张卡片显示加载状态（外部调用确认最长约 30 秒）
  const [actingKey, setActingKey] = useState<string | null>(null)
  const [lastTask, setLastTask] = useState<Task | null>(null)
  const [steps, setSteps] = useState<AgentToolStep[]>([])
  const stream = useTaskStream()
  const { follow, detach } = stream
  const requestSeq = useRef(0)
  const alive = useRef(true)
  const following = useRef<string | null>(null)
  const settledTurn = useRef<string | null>(null)
  const polledTurn = useRef<{ id: number; key: string | null } | null>(null)
  const busyRef = useRef(false)
  const track = useTasksStore((s) => s.track)

  const refresh = useCallback(async () => {
    const seq = ++requestSeq.current
    const scope = captureProjectScope(pid)
    const next = await agentApi.get(pid, cid)
    if (!alive.current || seq !== requestSeq.current) throw new AgentReadSuperseded()
    if (!isProjectScopeCurrent(scope, 'canViewAgent')) throw new Error('作品权限已变化，请重新读取对话。')
    setDetail(next); setError(null)
    return next
  }, [pid, cid])
  // 作品权限快照变化会按作品 + 授权轮次重挂整个智能体面板；这里先撤下可写状态，再按服务端事实读一次。
  const latest = useRef({ refresh, onUpdated, detach })
  useEffect(() => { latest.current = { refresh, onUpdated, detach } }, [refresh, onUpdated, detach])
  // 只在事件与异步回调中取用（不在渲染期读取）；每次挂载一个实例，lost 状态不跨会话 / 授权轮次。
  const recoveryRef = useRef<ReturnType<typeof createAgentAccessRecovery> | null>(null)
  const recovery = useCallback(() => {
    recoveryRef.current ??= createAgentAccessRecovery({
      alive: () => alive.current,
      setLost,
      stop: () => { following.current = null; latest.current.detach() },
      refreshProject: () => { void useProjectStore.getState().refresh() },
      reread: () => latest.current.refresh(),
      onGone: () => { setDetail(null); setError(AGENT_ACCESS_LOST_TEXT); latest.current.onUpdated() },
      onError: (err) => setError(errorMessage(err)),
    })
    return recoveryRef.current
  }, [])
  const accessLost = useCallback(() => recovery().markLost(), [recovery])
  const refreshSafe = useCallback(() => {
    void refresh().then(() => recovery().settle(), (err: unknown) => {
      if (!alive.current || err instanceof AgentReadSuperseded) return
      if (isAgentAccessLost(err)) void accessLost()
      else setError(errorMessage(err))
    })
  }, [refresh, accessLost, recovery])
  useProjectRefresh(pid, ['setting-ai-visibility'], refreshSafe)
  const historySealed = sealedError || detail?.history_sealed === true
  const writable = detail?.writable === true && !lost && access.canUseAgent
  const invalidate = useCallback((changes: AgentChange[]) => {
    invalidateProject(pid, changedResources(changes))
    void useProjectStore.getState().refresh()
    void useTasksStore.getState().refresh()
  }, [pid])
  const updateChange = useCallback((change: AgentChange) => {
    setDetail((prev) => {
      if (!prev) return prev
      const current = prev.changes.find((item) => item.id === change.id)
      // 迟到事件不能覆盖已显示的另一实例或另一预览；权威读取负责引入新快照。
      if (current && (current.change_key !== change.change_key || current.view_version !== change.view_version)) return prev
      return { ...prev, changes: upsertAgentChange(prev.changes, change) }
    })
  }, [])

  useEffect(() => {
    alive.current = true
    queueMicrotask(() => { if (alive.current) refreshSafe() })
    return () => { alive.current = false; requestSeq.current += 1; detach() }
  }, [refreshSafe, detach])

  const latestTurn = detail?.messages.filter((m) => m.turn_task_id !== null).at(-1)
  const latestTurnId = latestTurn?.turn_task_id
  const latestTurnKey = latestTurn?.turn_task_key
  useEffect(() => {
    const identity = { id: latestTurnId ?? 0, task_key: latestTurnKey ?? null }
    if (!hasTaskIdentity(identity)) return
    let current = true
    void taskApi.get(identity).then((task) => { if (current) setLastTask(task) }).catch(() => { if (current) setLastTask(null) })
    return () => { current = false }
  }, [latestTurnId, latestTurnKey, detail?.active_task?.status])
  const active = detail?.active_task
  // 作者与具备智能体使用权限的协作者均实时订阅活动轮次（协作者经后端受控 SSE 管线）。
  useEffect(() => {
    if (!canStream || agentProgressMode(collaborator, lost, active) !== 'stream' || !active || !hasTaskIdentity(active)) return
    const identity = `${active.id}:${active.task_key}`
    if (following.current === identity) return
    following.current = identity
    void follow(active, {
      onSnapshot: refreshSafe,
      onToolStart: (e) => setSteps((prev) => mergeToolStep(prev, { ...e, turn_task_id: active.id, turn_task_key: active.task_key })),
      onToolEnd: (e) => setSteps((prev) => mergeToolStep(prev, { ...e, turn_task_id: active.id, turn_task_key: active.task_key })),
      onProposal: (change) => { updateChange(change); refreshSafe() },
      onExecuted: (change) => { updateChange(change); refreshSafe(); invalidate([change]) },
    }).then(async (end) => {
      if (end.status === 'detached' || !alive.current) return
      if (end.status === 'lost') {
        if (following.current === identity) following.current = null
        setSteps([])
        if (end.accessLost) void accessLost()
        return
      }
      try {
        const next = await refresh()
        if (!alive.current) return
        setSteps([])
        settledTurn.current = identity
        invalidate(next.changes.filter((c) => c.turn_task_id === active.id && c.turn_task_key === active.task_key && c.status === 'executed'))
        onUpdated()
      } catch (err) {
        if (!alive.current || err instanceof AgentReadSuperseded) return
        if (isAgentAccessLost(err)) void accessLost()
        else setError(errorMessage(err))
      }
      finally { if (following.current === identity) following.current = null }
    })
  }, [active, canStream, collaborator, lost, follow, refreshSafe, refresh, updateChange, invalidate, onUpdated, accessLost])

  // 兜底轮询发现本轮结束（如网络断流后由可见轮询观察到完成）：失效已执行修改涉及的资源并刷新会话列表。
  useEffect(() => {
    if (!collaborator || !detail) return
    const open = detail.active_task && isOpenTask(detail.active_task.status) ? detail.active_task : null
    const previous = polledTurn.current
    polledTurn.current = open ? { id: open.id, key: open.task_key } : null
    if (!previous || (open && open.id === previous.id && open.task_key === previous.key)) return
    const prevKey = `${previous.id}:${previous.key ?? ''}`
    if (settledTurn.current === prevKey) return
    invalidate(detail.changes.filter((c) => c.turn_task_id === previous.id && c.turn_task_key === previous.key && c.status === 'executed'))
    onUpdated()
  }, [collaborator, detail, invalidate, onUpdated])

  // 待确认轮次可能在另一设备继续；重回页面也重新核对持久化历史。失去权限后停止。
  const activeId = active?.id
  const activeOpen = !!active && isOpenTask(active.status)
  useEffect(() => startAgentTurnPolling(refreshSafe, { collaborator, lost, activeId, activeOpen }, document),
    [activeId, activeOpen, collaborator, lost, refreshSafe])

  const act = async (operation: () => Promise<unknown>, notice?: string): Promise<AgentActionResult> => {
    const rejected = { accepted: false, ok: false }
    if (busyRef.current || recovery().isLost() || !writable) return rejected
    const scope = captureProjectScope(pid)
    if (!isProjectScopeCurrent(scope, 'canUseAgent')) return rejected
    const current = () => alive.current && isProjectScopeCurrent(scope, 'canUseAgent')
    busyRef.current = true
    setBusy(true)
    try {
      return await runAgentAction({
        operation: async () => {
          try { return await operation() }
          catch (err) {
            if (alive.current && isAgentHistorySealed(err)) setSealedError(true)
            throw err
          }
        },
        refreshOnReject: async () => { if (current()) await refresh() },
        current,
        reread: refresh,
        settle: (next) => {
          invalidate(next.changes.filter((c) => c.status === 'executed' || c.status === 'undone'))
          onUpdated()
          if (notice) message.success(notice)
        },
        fail: (err) => {
          const original = err instanceof AgentRetryRejected ? err.original : err
          if (alive.current && isAgentAccessLost(original)) {
            // 中途撤权 / 降级：不重放操作，停止轮询并刷新权限，界面随后切为只读或离开。
            message.error(errorMessage(err))
            void accessLost()
          } else if (current()) message.error(errorMessage(err))
        },
      })
    }
    finally { busyRef.current = false; if (alive.current) setBusy(false) }
  }
  const run = async (operation: () => Promise<unknown>, notice?: string) => (await act(operation, notice)).ok
  // 服务端受理即返回 true（调用方据此清空输入）：受理后的重读 / 作用域变化不保留已发送文本；被拒绝或失败时返回 false。
  const send = async (body: AgentSendRequest) => historySealed ? false : (await act(async () => {
    const scope = captureProjectScope(pid)
    const result = await agentApi.send(pid, cid, body)
    if (!isProjectScopeCurrent(scope, 'canUseAgent')) return
    track(result.task)
    setSteps([])
    stream.reset()
  })).accepted
  const change = async (item: AgentChange, action: 'confirm' | 'reject' | 'undo') => {
    if (busy || !writable || (historySealed && action !== 'undo')) return false
    setActingKey(item.change_key)
    try {
      return await run(async () => {
        await agentApi.change(pid, item, action)
      }, action === 'undo' ? '已撤销修改' : undefined)
    } finally { if (alive.current) setActingKey(null) }
  }
  const turn = (task: TaskIdentity, action: AgentTurnAction, shown: AgentChange[]) => historySealed && action !== 'undo' ? Promise.resolve(false) : run(async () => {
    const scope = captureProjectScope(pid)
    const result = await agentApi.turn(pid, task, action, shown)
    if (!isProjectScopeCurrent(scope, 'canUseAgent')) return
    if (result.error) message.warning(result.error)
    if (result.notice) message.info(result.notice)
  })
  const cancel = (task: Task) => run(async () => {
    const scope = captureProjectScope(pid)
    const updated = await taskApi.cancel(task)
    if (isProjectScopeCurrent(scope, 'canUseAgent')) track(updated)
  }, '已取消本轮对话')
  const resume = (task: Task) => historySealed ? Promise.resolve(false) : run(async () => {
    const scope = captureProjectScope(pid)
    const updated = await taskApi.resume(task)
    if (!isProjectScopeCurrent(scope, 'canUseAgent')) return
    // 同一个任务身份会开始新一轮执行，先卸下待审核前的订阅。
    following.current = null
    detach()
    track(updated)
  }, '已继续本轮对话')
  const retry = (task: Task) => historySealed ? Promise.resolve(false) : run(async () => {
    const scope = captureProjectScope(pid)
    let updated: Task
    try { updated = await taskApi.retry(task) }
    catch (err) {
      if (collaborator && err instanceof ApiError && [400, 403, 409].includes(err.status)) {
        if (alive.current) setRejectedRetry(turnKey(task))
        throw new AgentRetryRejected(err)
      }
      throw err
    }
    if (isProjectScopeCurrent(scope, 'canUseAgent')) track(updated)
  })
  const update = (body: AgentConversationUpdate) => historySealed ? Promise.resolve(false) : run(async () => { await agentApi.update(pid, cid, body) })

  const currentLast = lastTask && lastTask.id === latestTurnId && lastTask.task_key === latestTurnKey ? lastTask : null
  const shownTask = stream.task ?? currentLast
  const retryState = shownTask
    ? agentRetryState(shownTask, collaborator, rejectedRetry === turnKey(shownTask)) : { retry: false, hint: null }
  return {
    detail, error, busy, historySealed, actingKey, steps, stream, lastTask: shownTask, refresh: refreshSafe,
    collaborator, writable, lost, canCreate: access.canUseAgent, readOnlyNotice: !detail ? null : lost ? AGENT_ACCESS_RECHECK_TEXT : agentReadOnlyNotice(writable, access.canUseAgent),
    retryAllowed: retryState.retry, retryHint: retryState.hint,
    send, change, turn, cancel, resume, retry, update,
  }
}
