// 灵感模式（/api/v1/inspiration）：会话 CRUD、步骤决定、补充想法、规模建议、转为项目 / 应用到已有项目；生成选项为同步 SSE（POST）

import { api, request } from './client'
import { runGeneration } from './sse'
import type {
  InspirationApplyPreviewItem,
  InspirationApplyRequest,
  InspirationConvertRequest,
  InspirationGenerateDone,
  InspirationGenerateRequest,
  InspirationRetryEvent,
  InspirationRound,
  InspirationScale,
  InspirationSession,
  InspirationSessionBrief,
  InspirationSessionUpdate,
  InspirationStepKey,
  InspirationStepUpdate,
  Project,
} from './types'

const S = '/inspiration/sessions'

export const inspirationApi = {
  list: () => api.get<InspirationSessionBrief[]>(S),
  create: (idea: string) => api.post<InspirationSession>(S, { idea }),
  /** 同一项目已有进行中的会话时返回那个会话 */
  fromProject: (projectId: number) => api.post<InspirationSession>(`${S}/from-project/${projectId}`),
  get: (id: number) => api.get<InspirationSession>(`${S}/${id}`),
  update: (id: number, body: InspirationSessionUpdate) => api.patch<InspirationSession>(`${S}/${id}`, body),
  remove: (id: number) => api.del(`${S}/${id}`),
  updateStep: (id: number, step: InspirationStepKey, body: InspirationStepUpdate) =>
    api.patch<InspirationSession>(`${S}/${id}/steps/${step}`, body),
  addNote: (id: number, text: string) => api.post<InspirationSession>(`${S}/${id}/notes`, { text }),
  /** 返回最新会话；越界 → 404 */
  deleteNote: (id: number, index: number) =>
    request<InspirationSession>(`${S}/${id}/notes/${index}`, { method: 'DELETE' }),
  scaleSuggest: (targetWords: number) =>
    api.get<InspirationScale>('/inspiration/scale-suggest', { target_words: targetWords }),
  /** 201 新项目；已转换且项目仍在 → 409 */
  convert: (id: number, body: InspirationConvertRequest) => api.post<Project>(`${S}/${id}/convert`, body),
  applyPreview: (id: number, projectId: number) =>
    api.get<InspirationApplyPreviewItem[]>(`${S}/${id}/apply-preview`, { project_id: projectId }),
  apply: (id: number, body: InspirationApplyRequest) => api.post<Project>(`${S}/${id}/apply`, body),
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** 收窄 done 载荷 {round, session} */
export function parseGenerateDone(data: unknown): InspirationGenerateDone {
  if (!isRecord(data) || !isRecord(data.round) || !isRecord(data.session)) throw new Error('生成结果格式不正确')
  const r = data.round
  const round: InspirationRound = {
    options: Array.isArray(r.options) ? r.options.filter((o): o is string => typeof o === 'string') : [],
    feedback: typeof r.feedback === 'string' ? r.feedback : '',
    created_at: typeof r.created_at === 'string' ? r.created_at : '',
  }
  return { round, session: data.session as unknown as InspirationSession }
}

/** 收窄 retry 事件 */
export function parseRetry(data: unknown): InspirationRetryEvent {
  const d = isRecord(data) ? data : {}
  return {
    attempt: typeof d.attempt === 'number' ? d.attempt : 1,
    max: typeof d.max === 'number' ? d.max : 2,
    reason: typeof d.reason === 'string' ? d.reason : '',
  }
}

/** 重试提示文案 */
export function retryText(ev: InspirationRetryEvent): string {
  return `输出格式不对，正在重试（第 ${ev.attempt} 次）`
}

/**
 * 生成某一步的选项（同步流式）：delta 为模型原文；retry 时调用方应清空已显示的原文；
 * done 时选项已存进会话（返回最新会话）。中途中止不保存。
 */
export function generateOptions(
  sessionId: number,
  step: InspirationStepKey,
  body: InspirationGenerateRequest,
  handlers: { onDelta: (text: string) => void; onRetry: (ev: InspirationRetryEvent) => void; signal?: AbortSignal },
): Promise<InspirationGenerateDone> {
  return runGeneration(`${S}/${sessionId}/steps/${step}/generate`, body, {
    onDelta: handlers.onDelta,
    signal: handlers.signal,
    parseDone: parseGenerateDone,
    onEvent: (event, data) => {
      if (event === 'retry') handlers.onRetry(parseRetry(data))
    },
  })
}
