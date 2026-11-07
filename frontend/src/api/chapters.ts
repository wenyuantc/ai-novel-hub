import { api } from './client'
import type {
  BatchGenerateRequest,
  BatchPreview,
  Chapter,
  ChapterBrief,
  ChapterCreate,
  ChapterDone,
  ChapterStatus,
  ChapterUpdate,
  GenerateChapterRequest,
  PolishChapterRequest,
  Task,
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

/** 整章生成：创建后台任务（202），章节立即进入 generating；实时输出用 subscribeTask 订阅。 */
export function generateChapter(cid: number, body: GenerateChapterRequest): Promise<Task> {
  return api.post<Task>(`/chapters/${cid}/generate`, body)
}

/** 润色：同上。 */
export function polishChapter(cid: number, body: PolishChapterRequest): Promise<Task> {
  return api.post<Task>(`/chapters/${cid}/polish`, body)
}

/** 批量顺序生成预览：不建任务、不校验模型配置 */
export function previewBatch(pid: number, body: BatchGenerateRequest): Promise<BatchPreview> {
  return api.post<BatchPreview>(`/projects/${pid}/batch-generate/preview`, body)
}

/** 发起批量顺序生成：创建 chapter.batch 任务（202）；继续 / 结束 / 取消 / 重试走 taskApi */
export function startBatch(pid: number, body: BatchGenerateRequest): Promise<Task> {
  return api.post<Task>(`/projects/${pid}/batch-generate`, body)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

/** 校验章节任务的 done.result：{chapter, warning?} */
export function parseChapterDone(result: unknown): ChapterDone | null {
  if (!isRecord(result) || !isRecord(result.chapter) || typeof result.chapter.id !== 'number') return null
  const warning = typeof result.warning === 'string' && result.warning ? result.warning : undefined
  const scene = isRecord(result.scene) && typeof result.scene.id === 'number' && typeof result.scene.chapter_id === 'number' ? result.scene as unknown as import('./types').Scene : undefined
  const scene_ids = Array.isArray(result.scene_ids) && result.scene_ids.every((id) => typeof id === 'number' && Number.isSafeInteger(id) && id > 0) ? result.scene_ids : undefined
  return { chapter: result.chapter as unknown as Chapter, warning, scene, scene_ids }
}

export const CHAPTER_STATUS: Record<ChapterStatus, { label: string; color: string }> = {
  draft: { label: '草稿', color: 'default' },
  generating: { label: '生成中', color: 'processing' },
  done: { label: '已完成', color: 'success' },
}

/** 字数统计：与后端一致，按非空白字符的码点计（emoji / 扩展 B 区生僻字算 1 字）。 */
export function countWords(text: string): number {
  return Array.from(text.replace(/\s+/g, '')).length
}
