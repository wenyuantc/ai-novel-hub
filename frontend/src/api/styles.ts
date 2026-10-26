// 写作风格库（/api/v1/styles）：CRUD、复制、导入导出；提取 / 试写为同步 SSE（POST，与局部改写相同）

import { api } from './client'
import { runGeneration } from './sse'
import type {
  StyleCreate,
  StyleDraft,
  StyleExtractDone,
  StyleExtractRequest,
  StyleImportResult,
  StyleTrialDone,
  StyleTrialRequest,
  StyleUpdate,
  WritingStyle,
} from './types'

export const styleApi = {
  /** 预设在前，其后为我的风格 */
  list: () => api.get<WritingStyle[]>('/styles'),
  get: (id: number) => api.get<WritingStyle>(`/styles/${id}`),
  create: (body: StyleCreate) => api.post<WritingStyle>('/styles', body),
  /** 预设 → 403 */
  update: (id: number, body: StyleUpdate) => api.patch<WritingStyle>(`/styles/${id}`, body),
  /** 有引用且未 force → 409，ApiError.body.references = {projects, chapters} */
  remove: (id: number, force = false) => api.del(`/styles/${id}`, force ? { force: true } : undefined),
  copy: (id: number) => api.post<WritingStyle>(`/styles/${id}/copy`),
  /** 不给 ids = 导出我的全部风格 */
  export: (ids?: number[]) => api.get<Record<string, unknown>>('/styles/export', ids?.length ? { ids } : undefined),
  import: (data: unknown) => api.post<StyleImportResult>('/styles/import', { data }),
}

/** 与后端 MAX_STYLE_IMPORT_BYTES 一致 */
export const MAX_STYLE_IMPORT_BYTES = 1024 * 1024

/** 字段长度上限（与 schemas/style.py 一致） */
export const STYLE_LIMITS = {
  name: 100,
  description: 200,
  dimension: 500,
  taboos: 1000,
  sample: 2000,
  extra: 5000,
} as const

export const EXTRACT_MIN_CHARS = 200
export const EXTRACT_MAX_CHARS = 10000

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

const DRAFT_KEYS = [
  'name',
  'description',
  'pov',
  'rhythm',
  'dialogue',
  'description_density',
  'tone',
  'diction',
  'taboos',
  'sample',
  'extra',
] as const

/** 空白草稿 */
export function emptyStyleDraft(): StyleDraft {
  return Object.fromEntries(DRAFT_KEYS.map((k) => [k, ''])) as unknown as StyleDraft
}

/** 收窄提取的 done 载荷：缺字段补空串 */
export function parseExtractDone(data: unknown): StyleExtractDone {
  const raw = isRecord(data) && isRecord(data.draft) ? data.draft : {}
  const draft = emptyStyleDraft()
  for (const k of DRAFT_KEYS) {
    const v = raw[k]
    if (typeof v === 'string') draft[k] = v
  }
  const warnings =
    isRecord(data) && Array.isArray(data.warnings) ? data.warnings.filter((w): w is string => typeof w === 'string') : []
  return { draft, warnings }
}

export function parseTrialDone(data: unknown): StyleTrialDone {
  return { text: isRecord(data) && typeof data.text === 'string' ? data.text : '' }
}

/** 从样文提取风格（同步流式）：done 只给草稿，确认后再调用 create 保存 */
export function extractStyle(
  body: StyleExtractRequest,
  onDelta: (text: string) => void,
  signal?: AbortSignal,
): Promise<StyleExtractDone> {
  return runGeneration('/styles/extract', body, { onDelta, signal, parseDone: parseExtractDone })
}

/** 用风格试写一段（同步流式）：不写入任何章节 */
export function trialStyle(
  body: StyleTrialRequest,
  onDelta: (text: string) => void,
  signal?: AbortSignal,
): Promise<StyleTrialDone> {
  return runGeneration('/styles/trial', body, { onDelta, signal, parseDone: parseTrialDone })
}
