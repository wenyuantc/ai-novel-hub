import { api, ApiError } from './client'

export interface ProjectNote {
  id: number
  project_id: number
  title: string
  content: string
  revision: number
  is_favorite: boolean
  is_pinned: boolean
  created_at: string
  updated_at: string
}

export interface NoteSummary extends Omit<ProjectNote, 'content'> {
  excerpt: string
}

export interface NotePage {
  items: NoteSummary[]
  total: number
  page: number
  page_size: number
}

export interface NoteInput { title: string; content: string }
export interface NoteUpdate { expected_revision: number; title?: string; content?: string }
/** 收藏/固定只按 true 过滤；false 与缺省都不过滤。 */
export interface NoteQuery { q?: string; favorite?: boolean; pinned?: boolean; page?: number; page_size?: number }
/** 标记是幂等设置，不携带修订号，也不改变笔记 revision/updated_at。 */
export interface NoteFlags { is_favorite?: boolean; is_pinned?: boolean }

const base = (pid: number) => `/projects/${pid}/notes`

export const noteApi = {
  list: (pid: number, query: NoteQuery = {}) => api.get<NotePage>(base(pid), {
    q: query.q?.trim() || undefined, favorite: query.favorite || undefined, pinned: query.pinned || undefined,
    page: query.page, page_size: query.page_size,
  }),
  get: (pid: number, id: number) => api.get<ProjectNote>(`${base(pid)}/${id}`),
  create: (pid: number, body: NoteInput) => api.post<ProjectNote>(base(pid), body),
  update: (pid: number, id: number, body: NoteUpdate) => api.patch<ProjectNote>(`${base(pid)}/${id}`, body),
  setFlags: (pid: number, id: number, flags: NoteFlags) => api.put<ProjectNote>(`${base(pid)}/${id}/flags`, flags),
  remove: (pid: number, id: number, expectedRevision: number) =>
    api.del(`${base(pid)}/${id}`, { expected_revision: expectedRevision }),
}

export function isNoteConflict(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409 && error.body?.code === 'stale_revision'
}
