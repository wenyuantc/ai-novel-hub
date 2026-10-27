// 拆书导入接口（backend/app/api/v1/book_imports.py）：上传解析、未完成的解析、单章全文、重切、校正、预估、开始导入、删除。
// 只被懒加载的拆书页使用；任务面板的「跳过本批」在 api/tasks.ts（taskApi.skipBookImport），不引用本模块。

import { api } from './client'
import type {
  BookChapterText,
  BookEstimate,
  BookEstimateRequest,
  BookImport,
  BookImportBrief,
  BookOpRequest,
  BookResplitRequest,
  BookStartRequest,
  BookStartResult,
} from './types'

const base = '/book-imports'

export const bookImportApi = {
  /** multipart 上传，201；解析在请求内同步完成（格式 / 大小 / 字数不符 → 400，未导入的解析满 5 个 → 409） */
  upload: (file: File, signal?: AbortSignal) => {
    const form = new FormData()
    form.append('file', file)
    return api.upload<BookImport>(base, form, signal)
  },
  list: () => api.get<BookImportBrief[]>(base),
  get: (id: number) => api.get<BookImport>(`${base}/${id}`),
  /** 单章全文（按行），index 从 1 起 */
  chapterText: (id: number, index: number) => api.get<BookChapterText>(`${base}/${id}/chapters/${index}`),
  /** 重切（auto / regex）：清空之前的手工校正；修订号过期 → 409 */
  resplit: (id: number, body: BookResplitRequest) => api.post<BookImport>(`${base}/${id}/resplit`, body),
  /** 校正操作：响应替换整个章节列表；修订号过期 → 409 */
  op: (id: number, body: BookOpRequest) => api.post<BookImport>(`${base}/${id}/ops`, body),
  estimate: (id: number, body: BookEstimateRequest, signal?: AbortSignal) =>
    api.post<BookEstimate>(`${base}/${id}/estimate`, body, signal),
  /** 202：同一事务新建项目 + 章节 + 拆书任务，并删除这条解析记录 */
  start: (id: number, body: BookStartRequest) => api.post<BookStartResult>(`${base}/${id}/start`, body),
  remove: (id: number) => api.del(`${base}/${id}`),
}
