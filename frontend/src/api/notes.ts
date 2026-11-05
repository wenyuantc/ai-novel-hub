import { api, ApiError } from './client'

export interface ProjectNote {
  id: number
  project_id: number
  title: string
  content: string
  revision: number
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
export interface NoteQuery { q?: string; page?: number; page_size?: number }

const base = (pid: number) => `/projects/${pid}/notes`

export const noteApi = {
  list: (pid: number, query: NoteQuery = {}) => api.get<NotePage>(base(pid), {
    q: query.q?.trim() || undefined, page: query.page, page_size: query.page_size,
  }),
  get: (pid: number, id: number) => api.get<ProjectNote>(`${base(pid)}/${id}`),
  create: (pid: number, body: NoteInput) => api.post<ProjectNote>(base(pid), body),
  update: (pid: number, id: number, body: NoteUpdate) => api.patch<ProjectNote>(`${base(pid)}/${id}`, body),
  remove: (pid: number, id: number, expectedRevision: number) =>
    api.del(`${base(pid)}/${id}`, { expected_revision: expectedRevision }),
}

export function isNoteConflict(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409 && error.body?.code === 'stale_revision'
}
