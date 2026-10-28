import { api, ApiError } from './client'
import type { AgentChange, AgentConversation, AgentConversationDetail, AgentSendRequest, AgentSendResult, AgentTurnResult } from './types'

const base = (pid: number) => `/projects/${pid}/agent`
export const agentApi = {
  list: (pid: number) => api.get<AgentConversation[]>(`${base(pid)}/conversations`),
  create: (pid: number, title?: string) => api.post<AgentConversation>(`${base(pid)}/conversations`, { title }),
  get: (pid: number, cid: number) => api.get<AgentConversationDetail>(`${base(pid)}/conversations/${cid}`),
  update: (pid: number, cid: number, body: { title?: string; auto_execute?: boolean }) =>
    api.patch<AgentConversation>(`${base(pid)}/conversations/${cid}`, body),
  remove: (pid: number, cid: number) => api.del(`${base(pid)}/conversations/${cid}`),
  send: (pid: number, cid: number, body: AgentSendRequest) =>
    api.post<AgentSendResult>(`${base(pid)}/conversations/${cid}/messages`, body),
  change: (pid: number, id: number, action: 'confirm' | 'reject' | 'undo') =>
    api.post<AgentChange>(`${base(pid)}/changes/${id}/${action}`),
  turn: (pid: number, id: number, action: 'confirm-all' | 'reject-all' | 'undo') =>
    api.post<AgentTurnResult>(`${base(pid)}/turns/${id}/${action}`),
}

/** 409 后重新获取服务端提案；不重放用户的确认，必须再次确认新预览。 */
export async function withAgentRefresh<T>(operation: () => Promise<T>, refresh: () => Promise<void>): Promise<T> {
  try {
    return await operation()
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) await refresh()
    throw err
  }
}
