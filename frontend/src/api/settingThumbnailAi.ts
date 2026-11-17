import { request } from './client'
import { thumbnailPath, type ThumbnailTarget } from './settingThumbnails'
import type { SettingThumbnailInfo } from './types'

export interface ThumbnailPrompt { prompt: string; prompt_token: string }
export interface ThumbnailGenerateRequest {
  prompt: string
  prompt_token: string
  config_id?: number
  expected_version: string
  expected_revision?: number
}
export interface ThumbnailPreview {
  image_base64: string
  mime_type: string
  model_source: 'actor' | 'owner'
  preview_token: string
}
export type ThumbnailApplyResult = SettingThumbnailInfo | { thumbnail: SettingThumbnailInfo; revision: number }

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value)
}
function isThumbnail(value: unknown): value is SettingThumbnailInfo {
  return isRecord(value) && typeof value.version === 'string' && !!value.version
    && value.mime_type === 'image/webp' && typeof value.width === 'number' && value.width > 0
    && typeof value.height === 'number' && value.height > 0
}

export async function loadThumbnailPrompt(target: ThumbnailTarget, current: () => boolean, signal: AbortSignal): Promise<ThumbnailPrompt | null> {
  if (!current() || signal.aborted) return null
  const result = await request<unknown>(`${thumbnailPath(target)}/ai/prompt`, { signal, isCurrent: current })
  if (!current() || signal.aborted) return null
  if (!isRecord(result) || typeof result.prompt !== 'string' || !result.prompt.trim()
    || typeof result.prompt_token !== 'string' || !result.prompt_token) throw new Error('生成提示词响应无效，请重新打开。')
  return { prompt: result.prompt, prompt_token: result.prompt_token }
}

/** 只暴露有效图片、凭据及模型来源，丢弃服务端其他字段。 */
export async function generateThumbnailAI(target: ThumbnailTarget, body: ThumbnailGenerateRequest, mode: 'actor' | 'owner',
  current: () => boolean, signal: AbortSignal): Promise<ThumbnailPreview | null> {
  if (!current() || signal.aborted) return null
  const result = await request<unknown>(`${thumbnailPath(target)}/ai/generate`, { method: 'POST', body, signal, isCurrent: current })
  if (!current() || signal.aborted) return null
  if (!isRecord(result) || typeof result.image_base64 !== 'string' || !result.image_base64
    || result.mime_type !== 'image/webp' || typeof result.preview_token !== 'string' || !result.preview_token
    || result.model_source !== mode || (target.scope === 'series' && mode !== 'actor')) {
    throw new Error('图片或模型来源无法确认，请刷新后重新操作。')
  }
  return { image_base64: result.image_base64, mime_type: result.mime_type, model_source: mode, preview_token: result.preview_token }
}

export async function applyThumbnailAI(target: ThumbnailTarget, body: { image_base64: string; preview_token: string },
  current: () => boolean, signal: AbortSignal): Promise<ThumbnailApplyResult | null> {
  if (!current() || signal.aborted) return null
  const result = await request<unknown>(`${thumbnailPath(target)}/ai/apply`, { method: 'POST', body, signal, isCurrent: current })
  if (!current() || signal.aborted) return null
  if (target.scope === 'project' && isThumbnail(result)) return result
  if (target.scope === 'series' && isRecord(result) && isThumbnail(result.thumbnail)
    && Number.isSafeInteger(result.revision) && (result.revision as number) >= 0) {
    return { thumbnail: result.thumbnail, revision: result.revision as number }
  }
  throw new Error('图片保存响应无效，请先读取当前图片。')
}
