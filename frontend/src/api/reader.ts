// 阅读器：目录、阅读进度（reader-ui）
import { api, ApiError, buildUrl, request } from './client'
import type { ReaderEmotion } from '../utils/readerDialogue'
import type { ReaderVoice } from '../utils/readerVoices'
import type { Chapter, ReaderTocItem, ReadingProgressUpdate } from './types'

export interface ReaderRun { start: number; end: number; bold: boolean; italic: boolean }
export interface ReaderBlock {
  kind: 'paragraph' | 'heading' | 'bullet_list' | 'ordered_list' | 'blockquote'
  start: number; end: number; level: number | null; runs: ReaderRun[]
}
export interface ReaderChapter { chapter: Chapter; blocks: ReaderBlock[] | null; content_hash: string }

export interface ReaderAICastingRequest {
  content_hash: string
  cues: { start: number; end: number }[]
  voices: ReaderVoice[]
  ai_config_id: number | null
}

export interface ReaderAICastingResult {
  content_hash: string
  cues: { start: number; speaker: string | null; emotion: ReaderEmotion }[]
  voices: { speaker: string; voice_id: string }[]
}

export function castingContentChanged(error: unknown) {
  return error instanceof ApiError && error.status === 409 && error.body?.code === 'reader_casting_content_changed'
}

export const readerApi = {
  toc: (pid: number) => api.get<ReaderTocItem[]>(`/projects/${pid}/reader/toc`),
  chapter: (pid: number, cid: number) => api.get<ReaderChapter>(`/projects/${pid}/reader/chapters/${cid}`),
  aiCasting: (pid: number, cid: number, body: ReaderAICastingRequest, signal: AbortSignal, isCurrent: () => boolean) =>
    request<ReaderAICastingResult>(`/projects/${pid}/reader/chapters/${cid}/ai-casting`, { method: 'POST', body, signal, isCurrent }),
  saveProgress: (pid: number, body: ReadingProgressUpdate) => api.put<void>(`/projects/${pid}/reading-progress`, body),
}

/**
 * 页面隐藏 / 卸载时上报进度：fetch keepalive（页面关闭后请求仍会发出），失败静默。
 * 不走 api 客户端：卸载时不需要读响应，也不触发 401 处理。
 */
export function saveProgressKeepalive(pid: number, body: ReadingProgressUpdate): void {
  try {
    void fetch(buildUrl(`/projects/${pid}/reading-progress`), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body),
      credentials: 'include',
      keepalive: true,
    }).catch(() => {})
  } catch {
    // 忽略
  }
}
