import { request, type EditorActorIdentity } from './client'

export interface SearchScope {
  query: string
  start_chapter_id?: number
  end_chapter_id?: number
}
export interface SearchRequest extends SearchScope {
  page: number
  page_size: number
  snapshot?: string
}
export interface SearchHit {
  chapter_id: number
  chapter_no: number
  chapter_title: string
  revision: number
  content_hash: string
  start: number
  end: number
  before: string
  after: string
  scene_id: number | null
  unavailable_reason: string | null
  warning: string | null
  token: string | null
}
interface Page<T> { items: T[]; total: number; page: number; page_size: number }
export interface HitPage extends Page<SearchHit> { snapshot: string }
export interface SelectedHit extends Pick<SearchHit, 'chapter_id' | 'chapter_no' | 'chapter_title' | 'revision' | 'start' | 'end'> {
  scene_id: number
  token: string
}
export interface ApplyRevision extends SearchScope { replacement: string; request_id: string; hits: SelectedHit[] }
export interface RevisionBatch {
  id: number
  query: string
  replacement: string
  chapter_count: number
  hit_count: number
  status: 'applied' | 'undone'
  created_at: string
  undone_at: string | null
}
const base = (pid: number) => `/projects/${pid}/book-search`
type IsCurrent = () => boolean
export const bookSearchApi = {
  query: (pid: number, body: SearchRequest, signal: AbortSignal, isCurrent: IsCurrent) =>
    request<HitPage>(`${base(pid)}/query`, { method: 'POST', body, signal, isCurrent }),
  preview: (pid: number, body: SearchRequest & { replacement: string }, signal: AbortSignal, isCurrent: IsCurrent) =>
    request<HitPage>(`${base(pid)}/preview`, { method: 'POST', body, signal, isCurrent }),
  apply: (pid: number, body: ApplyRevision, editorActor: EditorActorIdentity, isCurrent: IsCurrent) =>
    request<RevisionBatch>(`${base(pid)}/apply`, { method: 'POST', body, editorActor, isCurrent }),
  history: (pid: number, page: number, signal: AbortSignal, isCurrent: IsCurrent) =>
    request<Page<RevisionBatch>>(`${base(pid)}/batches`, { query: { page, page_size: 20 }, signal, isCurrent }),
  undo: (pid: number, id: number, editorActor: EditorActorIdentity, isCurrent: IsCurrent) =>
    request<RevisionBatch>(`${base(pid)}/batches/${id}/undo`, { method: 'POST', editorActor, isCurrent }),
}
