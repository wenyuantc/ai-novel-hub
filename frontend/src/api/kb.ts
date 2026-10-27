// 知识库：设置、文档上传（multipart）/ 注入开关 / 删除 / 重建索引、分块、检索调试、索引状态与重建

import { ApiError, api } from './client'
import type {
  IndexStatus,
  KbChunkPage,
  KbDocument,
  KbSearchRequest,
  KbSearchResult,
  KbSettings,
  KbSettingsUpdate,
  KbUploadResult,
  Task,
} from './types'

const base = (pid: number) => `/projects/${pid}/kb`

export const kbApi = {
  getSettings: (pid: number) => api.get<KbSettings>(`${base(pid)}/settings`),
  updateSettings: (pid: number, body: KbSettingsUpdate) => api.patch<KbSettings>(`${base(pid)}/settings`, body),
  listDocuments: (pid: number) => api.get<KbDocument[]>(`${base(pid)}/documents`),
  /** 202：解析在请求内同步完成（格式 / 大小 / 扫描件 → 400），随后排队建索引任务；重复上传 → 409（body.document_id） */
  upload: (pid: number, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return api.upload<KbUploadResult>(`${base(pid)}/documents`, form)
  },
  setInject: (pid: number, id: number, inject: boolean) =>
    api.patch<KbDocument>(`${base(pid)}/documents/${id}`, { inject }),
  /** 有活动建索引任务时 409 */
  remove: (pid: number, id: number) => api.del(`${base(pid)}/documents/${id}`),
  /** 删除分块后重新排队（202）；有活动任务时 409 */
  reindex: (pid: number, id: number) => api.post<Task>(`${base(pid)}/documents/${id}/reindex`),
  chunks: (pid: number, id: number, page = 1, pageSize = 20) =>
    api.get<KbChunkPage>(`${base(pid)}/documents/${id}/chunks`, { page, page_size: pageSize }),
  search: (pid: number, body: KbSearchRequest) => api.post<KbSearchResult>(`${base(pid)}/search`, body),
  indexStatus: (pid: number) => api.get<IndexStatus>(`${base(pid)}/index-status`),
  /** 为全部缺少当前向量的内容生成向量（202）；没有向量模型 → 400 */
  rebuild: (pid: number) => api.post<Task>(`${base(pid)}/rebuild`),
}

/** 重复上传 409 时响应体带的已有文档 id；其他错误为 null */
export function duplicateDocumentId(err: unknown): number | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null
  const id = err.body?.document_id
  return typeof id === 'number' ? id : null
}
