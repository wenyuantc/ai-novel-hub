import { api, request } from './client'
import type {
  WorkshopCategory, WorkshopImportResult, WorkshopItem, WorkshopItemList, WorkshopKind,
  WorkshopMetadata, WorkshopSubmission, WorkshopSubmissionList, WorkshopSubmissionStatus, WorkshopSubmit,
} from './types'

export interface WorkshopQuery {
  kind?: WorkshopKind
  category?: WorkshopCategory
  tag?: string
  prompt_key?: string
  q?: string
  sort?: 'newest' | 'likes' | 'downloads'
  page?: number
  page_size?: number
}
export interface SubmissionQuery {
  status?: WorkshopSubmissionStatus
  page?: number
  page_size?: number
}

const base = '/workshop'
export const workshopApi = {
  list: (query: WorkshopQuery = {}) => api.get<WorkshopItemList>(`${base}/items`, { ...query }),
  mine: (page = 1, pageSize = 12) => api.get<WorkshopItemList>(`${base}/mine/items`, { page, page_size: pageSize }),
  get: (id: number) => api.get<WorkshopItem>(`${base}/items/${id}`),
  submit: (body: WorkshopSubmit) => api.post<WorkshopSubmission>(`${base}/submissions`, body),
  submissions: (query: SubmissionQuery = {}) => api.get<WorkshopSubmissionList>(`${base}/submissions`, { ...query }),
  submission: (id: number) => api.get<WorkshopSubmission>(`${base}/submissions/${id}`),
  withdraw: (id: number) => api.post<WorkshopSubmission>(`${base}/submissions/${id}/withdraw`),
  hide: (id: number) => api.post<WorkshopItem>(`${base}/items/${id}/hide`),
  unhide: (id: number) => api.post<WorkshopItem>(`${base}/items/${id}/unhide`),
  like: (id: number) => api.post<WorkshopItem>(`${base}/items/${id}/like`),
  unlike: (id: number) => request<WorkshopItem>(`${base}/items/${id}/like`, { method: 'DELETE' }),
  import: (id: number) => api.post<WorkshopImportResult>(`${base}/items/${id}/import`),
  pending: (query: SubmissionQuery = {}) => api.get<WorkshopSubmissionList>(`${base}/admin/submissions`, { ...query }),
  approve: (id: number, body: Partial<WorkshopMetadata>) => api.post<WorkshopSubmission>(`${base}/admin/submissions/${id}/approve`, body),
  reject: (id: number, note: string) => api.post<WorkshopSubmission>(`${base}/admin/submissions/${id}/reject`, { note }),
  publish: (body: WorkshopSubmit) => api.post<WorkshopItem>(`${base}/admin/items`, body),
  metadata: (id: number, body: Partial<WorkshopMetadata>) => api.patch<WorkshopItem>(`${base}/admin/items/${id}`, body),
  remove: (id: number) => api.del(`${base}/admin/items/${id}`),
}
