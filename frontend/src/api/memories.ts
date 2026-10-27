// 剧情记忆：列表筛选、手动记忆增删改、钉住 / 屏蔽、检索调试；章节的注入预览

import { api } from './client'
import type {
  ContextPreview,
  Memory,
  MemoryCreate,
  MemoryListQuery,
  MemoryPage,
  MemorySearchRequest,
  MemorySearchResult,
  MemoryUpdate,
} from './types'

const base = (pid: number) => `/projects/${pid}/memories`

export const memoryApi = {
  list: (pid: number, query: MemoryListQuery = {}) =>
    api.get<MemoryPage>(base(pid), {
      kind: query.kind,
      origin: query.origin,
      chapter_from: query.chapter_from,
      chapter_to: query.chapter_to,
      pinned: query.pinned,
      muted: query.muted,
      q: query.q?.trim() || undefined,
      page: query.page,
      page_size: query.page_size,
    }),
  create: (pid: number, body: MemoryCreate) => api.post<Memory>(base(pid), body),
  /** 自动记忆只能改 pinned / muted（改内容 → 400） */
  update: (pid: number, id: number, body: MemoryUpdate) => api.patch<Memory>(`${base(pid)}/${id}`, body),
  /** 自动记忆不能删除（400） */
  remove: (pid: number, id: number) => api.del(`${base(pid)}/${id}`),
  search: (pid: number, body: MemorySearchRequest) => api.post<MemorySearchResult>(`${base(pid)}/search`, body),
  /** 本次生成会注入的【参考资料】与【相关旧情节】（裁剪前） */
  contextPreview: (chapterId: number) => api.get<ContextPreview>(`/chapters/${chapterId}/context-preview`),
}
