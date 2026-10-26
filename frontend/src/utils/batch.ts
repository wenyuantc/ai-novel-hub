// 批量顺序生成（chapter.batch）的纯函数：默认范围、清单行文案、耗时文案、汇总收窄；
// 横幅状态 / 续跑位置 / 排队标签在 batchState.ts（章节页在入口 chunk，只引用那个小模块），这里一并重新导出。

import type {
  BatchAnalysisState,
  BatchItem,
  BatchItemState,
  BatchSummary,
  ChapterBrief,
} from '../api/types'

export { batchBannerState, batchResumeIndex, queuedBatchChapterIds, type BatchBannerState } from './batchState'

/** 与后端一致：按 (chapter_no, id) 排序 */
export function sortChapters<T extends Pick<ChapterBrief, 'id' | 'chapter_no'>>(chapters: readonly T[]): T[] {
  return [...chapters].sort((a, b) => a.chapter_no - b.chapter_no || a.id - b.id)
}

/** 默认范围：起点 = 第一个未写章节（没有则第一章），终点 = 最后一章；没有章节时为 null */
export function defaultBatchRange(
  chapters: readonly Pick<ChapterBrief, 'id' | 'chapter_no' | 'word_count'>[],
): { startId: number; endId: number } | null {
  const sorted = sortChapters(chapters)
  if (sorted.length === 0) return null
  const firstEmpty = sorted.find((c) => c.word_count === 0) ?? sorted[0]
  return { startId: firstEmpty.id, endId: sorted[sorted.length - 1].id }
}

/** 「生成到第 N 章」：章节号 ≤ N 的最后一章；没有则 null */
export function endIdUpTo(chapters: readonly Pick<ChapterBrief, 'id' | 'chapter_no'>[], n: number): number | null {
  const hits = sortChapters(chapters).filter((c) => c.chapter_no <= n)
  return hits.length ? hits[hits.length - 1].id : null
}

/** 范围内的章节（按章节号两端包含，与后端求值一致；仅用于即时提示，以预览接口为准） */
export function rangeChapters<T extends Pick<ChapterBrief, 'id' | 'chapter_no'>>(
  chapters: readonly T[],
  startId: number | null | undefined,
  endId: number | null | undefined,
): T[] {
  const start = chapters.find((c) => c.id === startId)
  const end = chapters.find((c) => c.id === endId)
  if (!start || !end || start.chapter_no > end.chapter_no) return []
  return sortChapters(chapters).filter((c) => c.chapter_no >= start.chapter_no && c.chapter_no <= end.chapter_no)
}

/** 已处理章数 / 总章数 / 已生成章数 */
export function batchCounts(items: readonly BatchItem[]): { processed: number; total: number; done: number } {
  return {
    processed: items.filter((it) => it.state !== 'pending').length,
    total: items.length,
    done: items.filter((it) => it.state === 'done').length,
  }
}

export const BATCH_ITEM_STATE: Record<BatchItemState, { label: string; color: string }> = {
  pending: { label: '待生成', color: 'default' },
  done: { label: '已完成', color: 'success' },
  skipped_written: { label: '跳过（已写）', color: 'default' },
  skipped_missing: { label: '跳过（已删除）', color: 'default' },
  failed: { label: '失败', color: 'error' },
}

export const BATCH_ANALYSIS: Record<BatchAnalysisState, { label: string; color: string }> = {
  ok: { label: '已分析', color: 'green' },
  skipped: { label: '未分析', color: 'default' },
  failed: { label: '分析失败', color: 'orange' },
}

export interface BatchRowView {
  label: string
  color: string
  /** 字数 / 目标、分析结果等简述，可能为空 */
  detail: string
  /** 失败原因或警告（续写失败、分析失败），可能为空 */
  note: string
}

/** 清单行文案；current = 该章正在处理（状态显示为「生成中」/「分析中」） */
export function batchRowView(item: BatchItem, current: 'generating' | 'analyzing' | null = null): BatchRowView {
  const base = BATCH_ITEM_STATE[item.state] ?? { label: item.state, color: 'default' }
  let { label, color } = base
  if (current === 'generating') {
    label = '生成中'
    color = 'processing'
  } else if (current === 'analyzing') {
    label = '分析中'
    color = 'processing'
  }
  const parts: string[] = []
  if (item.state === 'done') {
    parts.push(`${item.words} 字`)
    if (item.analysis) parts.push(BATCH_ANALYSIS[item.analysis].label)
  } else if (item.target_words > 0 && (item.state === 'pending' || item.state === 'failed')) {
    parts.push(`目标 ${item.target_words} 字${item.words_source === 'plan' ? '（细纲）' : ''}`)
  }
  const note = item.state === 'failed' ? item.error : item.warning
  return { label, color, detail: parts.join(' · '), note: note || '' }
}

/** 预计耗时文案（后端 estimated_minutes） */
export function formatEta(minutes: number): string {
  if (!Number.isFinite(minutes) || minutes <= 0) return '不到 1 分钟'
  const m = Math.ceil(minutes)
  if (m < 60) return `约 ${m} 分钟`
  const h = Math.floor(m / 60)
  const rest = m % 60
  return rest ? `约 ${h} 小时 ${rest} 分钟` : `约 ${h} 小时`
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

/** 收窄 done / paused / 结束后的汇总；结构不符返回 null */
export function parseBatchSummary(result: unknown): BatchSummary | null {
  if (!isRecord(result) || typeof result.total !== 'number' || typeof result.done !== 'number') return null
  const arr = (v: unknown) => (Array.isArray(v) ? v.filter(isRecord) : [])
  return {
    total: result.total,
    done: result.done,
    skipped_written: typeof result.skipped_written === 'number' ? result.skipped_written : 0,
    skipped_missing: typeof result.skipped_missing === 'number' ? result.skipped_missing : 0,
    failed: arr(result.failed) as unknown as BatchSummary['failed'],
    analysis_failed: arr(result.analysis_failed) as unknown as BatchSummary['analysis_failed'],
    remaining: typeof result.remaining === 'number' ? result.remaining : 0,
    warnings: Array.isArray(result.warnings) ? result.warnings.filter((w): w is string => typeof w === 'string') : [],
    ended_early: result.ended_early === true,
  }
}

/** 汇总一行：「已生成 5 章 · 跳过已写 2 章 · 失败 1 章 · 剩余 3 章（提前结束）」 */
export function batchSummaryText(s: BatchSummary): string {
  const parts = [`共 ${s.total} 章`, `已生成 ${s.done} 章`]
  if (s.skipped_written) parts.push(`跳过已写 ${s.skipped_written} 章`)
  if (s.skipped_missing) parts.push(`跳过已删除 ${s.skipped_missing} 章`)
  if (s.failed.length) parts.push(`失败 ${s.failed.length} 章`)
  if (s.analysis_failed.length) parts.push(`分析失败 ${s.analysis_failed.length} 章`)
  if (s.remaining) parts.push(`剩余 ${s.remaining} 章`)
  return parts.join(' · ') + (s.ended_early ? '（提前结束）' : '')
}

/** 由阶段文案（「第 3/20 章 · 分析中」）判断当前章处于生成还是分析 */
export function batchCurrentPhase(stage: string | null | undefined): 'generating' | 'analyzing' {
  return stage?.includes('分析中') ? 'analyzing' : 'generating'
}

/** 由阶段文案（「第 3/20 章 · 生成中」）取当前章下标（从 0 起）；中途接上订阅、还没收到 chapter_start 时使用 */
export function batchStageIndex(stage: string | null | undefined): number | null {
  const m = /第\s*(\d+)\s*\/\s*\d+\s*章/.exec(stage ?? '')
  return m ? Number(m[1]) - 1 : null
}
