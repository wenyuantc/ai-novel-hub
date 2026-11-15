import { api, ApiError } from './client'
import { hasTaskIdentity, type TaskIdentity } from './tasks'
import type { AgentChange, AgentConversation, AgentConversationDetail, AgentConversationUpdate, AgentSendRequest, AgentSendResult, AgentTurnResult } from './types'

const base = (pid: number) => `/projects/${pid}/agent`
export type AgentTurnAction = 'confirm-all' | 'reject-all' | 'undo'
export type AgentChangeAction = 'confirm' | 'reject' | 'undo'
export interface AgentChangeIdentity { id: number; change_key: string; view_version: string }
export class AgentIdentityError extends Error {
  constructor() { super('提案或任务身份无法确认，请刷新后重新确认。'); this.name = 'AgentIdentityError' }
}
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
export function agentChangeIdentity(change: Pick<AgentChange, 'id' | 'change_key' | 'view_version'> | null | undefined): AgentChangeIdentity | null {
  return !!change && Number.isSafeInteger(change.id) && change.id > 0 && typeof change.change_key === 'string' && UUID.test(change.change_key)
    && typeof change.view_version === 'string' && /^[0-9a-f]{64}$/.test(change.view_version)
    ? { id: change.id, change_key: change.change_key, view_version: change.view_version } : null
}

/** 只快照本轮可操作且已展示的提案；任何缺身份的候选均阻断整轮提交。 */
export function snapshotAgentTurn(task: TaskIdentity, action: AgentTurnAction, shown: readonly AgentChange[]): AgentChangeIdentity[] {
  if (!hasTaskIdentity(task)) throw new AgentIdentityError()
  const candidates = shown.filter((change) => change.turn_task_id === task.id && (action === 'undo'
    ? change.status === 'executed' && change.kind !== 'task' && change.kind !== 'external'
    : change.status === 'proposed'))
  if (candidates.some((change) => !change.turn_task_key)) throw new AgentIdentityError()
  const entries = candidates.filter((change) => change.turn_task_key === task.task_key).map((change) => {
    const identity = agentChangeIdentity(change)
    if (!identity) throw new AgentIdentityError()
    return identity
  })
  if (new Set(entries.map((entry) => entry.id)).size !== entries.length) throw new AgentIdentityError()
  return entries
}
export const agentApi = {
  list: (pid: number) => api.get<AgentConversation[]>(`${base(pid)}/conversations`),
  create: (pid: number, title?: string) => api.post<AgentConversation>(`${base(pid)}/conversations`, { title }),
  get: (pid: number, cid: number) => api.get<AgentConversationDetail>(`${base(pid)}/conversations/${cid}`),
  /** mcp_plugin_ids：每个 id 须属于本人，否则 404 */
  update: (pid: number, cid: number, body: AgentConversationUpdate) =>
    api.patch<AgentConversation>(`${base(pid)}/conversations/${cid}`, body),
  remove: (pid: number, cid: number) => api.del(`${base(pid)}/conversations/${cid}`),
  send: (pid: number, cid: number, body: AgentSendRequest) =>
    api.post<AgentSendResult>(`${base(pid)}/conversations/${cid}/messages`, body),
  change: async (pid: number, change: AgentChange, action: AgentChangeAction) => {
    const identity = agentChangeIdentity(change)
    if (!identity || !hasTaskIdentity({ id: change.turn_task_id ?? 0, task_key: change.turn_task_key ?? null })) throw new AgentIdentityError()
    const expected = { conversationId: change.conversation_id, turnId: change.turn_task_id, turnKey: change.turn_task_key }
    const result = await api.post<AgentChange>(`${base(pid)}/changes/${identity.id}/${action}`,
      { change_key: identity.change_key, view_version: identity.view_version })
    if (!agentChangeIdentity(result) || result.id !== identity.id || result.change_key !== identity.change_key
      || result.project_id !== pid || result.conversation_id !== expected.conversationId
      || result.turn_task_id !== expected.turnId || result.turn_task_key !== expected.turnKey) throw new AgentIdentityError()
    return result
  },
  turn: async (pid: number, task: TaskIdentity, action: AgentTurnAction, shown: readonly AgentChange[]) => {
    const captured = { id: task.id, task_key: task.task_key }
    const changes = snapshotAgentTurn(captured, action, shown)
    const result = await api.post<AgentTurnResult>(`${base(pid)}/turns/${captured.id}/${action}`, { task_key: captured.task_key, changes })
    if (!result || !Array.isArray(result.changes) || (result.task && (result.task.id !== captured.id || result.task.task_key !== captured.task_key))
      || result.changes.some((change) => !agentChangeIdentity(change) || change.turn_task_id !== captured.id || change.turn_task_key !== captured.task_key
        || !changes.some((expected) => expected.id === change.id && expected.change_key === change.change_key))) throw new AgentIdentityError()
    return result
  },
}

/** 会话中途失去写权限（409 permission_denied，裁决 14）或已不可见（404）：停止轮询并刷新作品权限。 */
export const isAgentAccessLost = (err: unknown): boolean =>
  err instanceof ApiError && (err.status === 404 || (err.status === 409 && err.body?.code === 'permission_denied'))

export const isAgentHistorySealed = (err: unknown): boolean =>
  err instanceof ApiError && err.status === 409 && err.body?.code === 'ai_history_sealed'

/** 409 后重新获取服务端提案；不重放用户的确认，必须再次确认新预览。 */
export async function withAgentRefresh<T>(operation: () => Promise<T>, refresh: () => Promise<void>): Promise<T> {
  try {
    return await operation()
  } catch (err) {
    if (err instanceof AgentIdentityError || (err instanceof ApiError && [403, 404, 409, 422].includes(err.status))) {
      try { await refresh() } catch { /* 保留原失败，不重放用户操作。 */ }
    }
    throw err
  }
}
