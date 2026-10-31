import { api } from './client'
import type { Page, RecycleProject, RecycleProjectListQuery, RecyclePurgeRequest, RecycleRestoreRequest } from './types'

const base = '/admin/recycle-bin/projects'

export const recycleBinApi = {
  list: ({ q, owner_user_id, page, page_size }: RecycleProjectListQuery = {}) =>
    api.get<Page<RecycleProject>>(base, { q: q?.trim() || undefined, owner_user_id, page, page_size }),
  restore: (id: number, body: RecycleRestoreRequest) => api.post<void>(`${base}/${id}/restore`, body),
  purge: (id: number, body: RecyclePurgeRequest) => api.post<void>(`${base}/${id}/purge`, body),
}
