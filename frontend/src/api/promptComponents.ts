// 提示词组件库（/api/v1/prompt-components）：作者本人的可复用片段，在 Skill 正文与模板覆盖中以 {{> 标识 }} 引用

import { api } from './client'
import type {
  PromptComponent,
  PromptComponentBrief,
  PromptComponentCreate,
  PromptComponentImportResult,
  PromptComponentUpdate,
} from './types'

const base = '/prompt-components'

export const promptComponentApi = {
  /** q 为空时不带参数；按标识 / 名称 / 说明 / 内容搜索 */
  list: (q = '') => api.get<PromptComponentBrief[]>(base, { q: q.trim() || undefined }),
  get: (id: number) => api.get<PromptComponent>(`${base}/${id}`),
  create: (body: PromptComponentCreate) => api.post<PromptComponent>(base, body),
  /** 409 stale_revision / component_in_use（改标识）；400 component_breaks_usages（改内容） */
  update: (id: number, body: PromptComponentUpdate) => api.put<PromptComponent>(`${base}/${id}`, body),
  /** 被引用 → 409 component_in_use */
  remove: (id: number) => api.del(`${base}/${id}`),
  export: () => api.get<Record<string, unknown>>(`${base}/export`),
  import: (data: unknown) => api.post<PromptComponentImportResult>(`${base}/import`, { data }),
}

/** 与后端 schemas/prompt_component.py 一致 */
export const COMPONENT_LIMITS = { name: 100, description: 500, content: 20000, slug: 48, count: 200 } as const
export const MAX_COMPONENT_IMPORT_BYTES = 1024 * 1024
export const COMPONENT_SLUG_RE = /^[a-z0-9][a-z0-9-]{0,47}$/
