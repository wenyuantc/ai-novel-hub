// 提示词模板：系统默认 + 当前用户的覆盖（/api/v1/prompt-templates）

import { api } from './client'
import type {
  PromptImportResult,
  PromptPatch,
  PromptPreview,
  PromptPreviewRequest,
  PromptSave,
  PromptTemplateBrief,
  PromptTemplateDetail,
  PromptVersion,
} from './types'

const base = '/prompt-templates'
const keyPath = (key: string) => `${base}/${encodeURIComponent(key)}`

export const promptApi = {
  list: () => api.get<PromptTemplateBrief[]>(base),
  get: (key: string) => api.get<PromptTemplateDetail>(keyPath(key)),
  /** 保存覆盖（不合法 → 400，ApiError.body.errors 为逐条位置） */
  save: (key: string, body: PromptSave) => api.put<PromptTemplateDetail>(keyPath(key), body),
  patch: (key: string, body: PromptPatch) => api.patch<PromptTemplateDetail>(keyPath(key), body),
  /** 恢复默认：删除覆盖及其历史 */
  reset: (key: string) => api.del(keyPath(key)),
  versions: (key: string) => api.get<PromptVersion[]>(`${keyPath(key)}/versions`),
  restore: (key: string, versionId: number) =>
    api.post<PromptTemplateDetail>(`${keyPath(key)}/versions/${versionId}/restore`),
  preview: (key: string, body: PromptPreviewRequest, signal?: AbortSignal) =>
    api.post<PromptPreview>(`${keyPath(key)}/preview`, body, signal),
  export: () => api.get<Record<string, unknown>>(`${base}/export`),
  import: (data: unknown) => api.post<PromptImportResult>(`${base}/import`, { data }),
}

/** 与后端 MAX_IMPORT_BYTES 一致（紧凑序列化后的 UTF-8 字节数） */
export const MAX_PROMPT_IMPORT_BYTES = 1024 * 1024
