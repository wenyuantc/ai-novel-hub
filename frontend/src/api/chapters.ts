import { api } from './client'
import { runGeneration } from './sse'
import type {
  Chapter,
  ChapterBrief,
  ChapterCreate,
  ChapterStatus,
  ChapterUpdate,
  GenerateChapterRequest,
  PolishChapterRequest,
} from './types'

export const chapterApi = {
  list: (pid: number) => api.get<ChapterBrief[]>(`/projects/${pid}/chapters`),
  get: (pid: number, cid: number) => api.get<Chapter>(`/projects/${pid}/chapters/${cid}`),
  create: (pid: number, body: ChapterCreate) => api.post<Chapter>(`/projects/${pid}/chapters`, body),
  fromOutlines: (pid: number, outlineIds: number[] | null) =>
    api.post<ChapterBrief[]>(`/projects/${pid}/chapters/from-outlines`, { outline_ids: outlineIds }),
  update: (pid: number, cid: number, body: ChapterUpdate) =>
    api.patch<Chapter>(`/projects/${pid}/chapters/${cid}`, body),
  remove: (pid: number, cid: number) => api.del(`/projects/${pid}/chapters/${cid}`),
}

interface ChapterDone {
  chapter: Chapter
}

export function generateChapter(
  cid: number,
  body: GenerateChapterRequest,
  onDelta: (text: string) => void,
  signal?: AbortSignal,
): Promise<Chapter> {
  return runGeneration<ChapterDone>(`/chapters/${cid}/generate`, body, { onDelta, signal }).then((d) => d.chapter)
}

export function polishChapter(
  cid: number,
  body: PolishChapterRequest,
  onDelta: (text: string) => void,
  signal?: AbortSignal,
): Promise<Chapter> {
  return runGeneration<ChapterDone>(`/chapters/${cid}/polish`, body, { onDelta, signal }).then((d) => d.chapter)
}

export const CHAPTER_STATUS: Record<ChapterStatus, { label: string; color: string }> = {
  draft: { label: '草稿', color: 'default' },
  generating: { label: '生成中', color: 'processing' },
  done: { label: '已完成', color: 'success' },
}

/** 字数统计：与后端一致，按非空白字符计。 */
export function countWords(text: string): number {
  return text.replace(/\s+/g, '').length
}
