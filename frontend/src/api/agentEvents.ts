import type { AgentChange, AgentToolEnd, AgentToolStart } from './types'

const record = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
export function isAgentToolStart(v: unknown): v is AgentToolStart {
  return record(v) && typeof v.call_id === 'string' && typeof v.name === 'string' && typeof v.label === 'string'
}
export function isAgentToolEnd(v: unknown): v is AgentToolEnd {
  return record(v) && typeof v.call_id === 'string' && typeof v.ok === 'boolean' && typeof v.summary === 'string'
}
export function readAgentChange(v: unknown): AgentChange | null {
  if (!record(v) || !record(v.change)) return null
  const c = v.change
  if (typeof c.id !== 'number' || typeof c.tool_call_id !== 'string' || typeof c.tool_name !== 'string' || typeof c.conversation_id !== 'number' || typeof c.seq !== 'number') return null
  if (!['proposed', 'executing', 'executed', 'rejected', 'failed', 'undone'].includes(String(c.status)) || !['small', 'delete', 'task', 'text'].includes(String(c.kind))) return null
  if (!record(c.preview) || typeof c.preview.title !== 'string' || !Array.isArray(c.preview.diff) || !Array.isArray(c.preview.warnings)) return null
  if (!c.preview.diff.every((d) => record(d) && typeof d.field === 'string' && typeof d.label === 'string') || !c.preview.warnings.every((w) => typeof w === 'string')) return null
  return c as unknown as AgentChange
}
