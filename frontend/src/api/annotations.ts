import { selectedHash } from '../utils/textOffset'
import { request } from './client'

export interface AnnotationMessage { id: number; body: string; author_name: string; created_at: string }
export interface AnnotationThread {
  id: number; chapter_id: number; scene_id: number; content_revision: number
  start: number | null; end: number | null; excerpt: string; selection_hash: string
  anchor_valid: boolean; resolved: boolean; created_at: string; first_message: AnnotationMessage
}
type SelectionEvidence = { selection_hash: string } | { selected_text: string }
export type AnnotationSelection = SelectionEvidence & {
  scene_id: number; expected_content_revision: number; start: number; end: number
}

export async function annotationEvidence(text: string): Promise<SelectionEvidence> {
  // 普通 HTTP 的浏览器没有 SubtleCrypto；由服务端在同一版本和选区逐字核对。
  if (!globalThis.crypto?.subtle || typeof globalThis.crypto.randomUUID !== 'function') return { selected_text: text }
  return { selection_hash: await selectedHash(text) }
}
export interface AnnotationPage<T> { items: T[]; total: number }
interface MessageInput { body: string; operation_id: string }
const base = (pid: number, cid: number) => `/projects/${pid}/chapters/${cid}/annotations`
export const annotationApi = {
  list: (pid: number, cid: number, offset: number, limit: number, isCurrent: () => boolean, signal?: AbortSignal) =>
    request<AnnotationPage<AnnotationThread>>(base(pid, cid), { query: { offset, limit }, isCurrent, signal }),
  create: (pid: number, cid: number, body: AnnotationSelection & MessageInput, isCurrent: () => boolean) =>
    request<AnnotationThread>(base(pid, cid), { method: 'POST', body, isCurrent }),
  messages: (pid: number, cid: number, id: number, offset: number, limit: number, isCurrent: () => boolean, signal?: AbortSignal) =>
    request<AnnotationPage<AnnotationMessage>>(`${base(pid, cid)}/${id}/messages`, { query: { offset, limit }, isCurrent, signal }),
  reply: (pid: number, cid: number, id: number, body: MessageInput, isCurrent: () => boolean) =>
    request<AnnotationMessage>(`${base(pid, cid)}/${id}/messages`, { method: 'POST', body, isCurrent }),
  resolve: (pid: number, cid: number, id: number, isCurrent: () => boolean) =>
    request<AnnotationThread>(`${base(pid, cid)}/${id}/resolve`, { method: 'POST', isCurrent }),
}
