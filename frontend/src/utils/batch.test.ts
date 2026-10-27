import { describe, expect, it } from 'vitest'
import type { BatchCheckpoint, BatchItem, Task } from '../api/types'
import {
  batchBannerState,
  batchCheckpoint,
  batchCounts,
  batchCurrentPhase,
  batchResumeIndex,
  batchRowView,
  batchStageIndex,
  batchSummaryText,
  defaultBatchRange,
  endIdUpTo,
  formatEta,
  parseBatchSummary,
  queuedBatchChapterIds,
  rangeChapters,
} from './batch'

const ch = (id: number, no: number, words = 0) => ({ id, chapter_no: no, word_count: words })

function item(id: number, no: number, over: Partial<BatchItem> = {}): BatchItem {
  return {
    chapter_id: id,
    chapter_no: no,
    title: `第${no}章`,
    state: 'pending',
    target_words: 0,
    words: 0,
    analysis: null,
    warning: '',
    error: '',
    ...over,
  }
}

function batch(status: Task['status'], items: BatchItem[], over: Partial<BatchCheckpoint> = {}, cursor = 0) {
  return {
    kind: 'chapter.batch' as const,
    status,
    step_cursor: cursor,
    checkpoint: { phase: 'generate' as const, current: null, since_review: 0, items, ...over },
  }
}

describe('批量范围', () => {
  it('默认起点是第一个未写章节，终点是最后一章；乱序输入按章节号排序', () => {
    expect(defaultBatchRange([ch(3, 3), ch(1, 1, 500), ch(2, 2)])).toEqual({ startId: 2, endId: 3 })
    // 全部已写：起点退回第一章
    expect(defaultBatchRange([ch(1, 1, 10), ch(2, 2, 10)])).toEqual({ startId: 1, endId: 2 })
    expect(defaultBatchRange([])).toBeNull()
  })

  it('生成到第 N 章：章节号 ≤ N 的最后一章', () => {
    const list = [ch(1, 1), ch(2, 2), ch(5, 5)]
    expect(endIdUpTo(list, 4)).toBe(2)
    expect(endIdUpTo(list, 5)).toBe(5)
    expect(endIdUpTo(list, 0)).toBeNull()
  })

  it('范围按章节号两端包含；起始晚于结束为空', () => {
    const list = [ch(1, 1), ch(2, 2), ch(3, 3), ch(4, 3)]
    expect(rangeChapters(list, 2, 4).map((c) => c.id)).toEqual([2, 3, 4])
    expect(rangeChapters(list, 3, 1)).toEqual([])
    expect(rangeChapters(list, 99, 1)).toEqual([])
  })
})

describe('批量横幅与断点', () => {
  it('横幅状态：运行中 / 待审核 / 失败或中断可续跑 / 其余不显示', () => {
    expect(batchBannerState(batch('queued', []))).toBe('running')
    expect(batchBannerState(batch('running', []))).toBe('running')
    expect(batchBannerState(batch('paused', []))).toBe('paused')
    expect(batchBannerState(batch('failed', []))).toBe('resumable')
    expect(batchBannerState(batch('interrupted', []))).toBe('resumable')
    expect(batchBannerState(batch('cancelled', []))).toBe('none')
    expect(batchBannerState(batch('succeeded', []))).toBe('none')
    expect(batchBannerState({ kind: 'chapter.generate', status: 'running' })).toBe('none')
    expect(batchBannerState(null)).toBe('none')
  })

  it('续跑位置：失败停下的章节 / 中断时的下一章 / 只差分析的章节', () => {
    const failedStop = batch(
      'failed',
      [item(1, 1, { state: 'done', analysis: 'ok' }), item(2, 2, { state: 'failed', error: '额度不足' }), item(3, 3)],
      {},
      1,
    )
    expect(batchResumeIndex(failedStop)).toBe(1)
    // skip_on_fail：失败章节已跳过，之后因别的原因失败 → 从下一章
    const skipped = batch(
      'failed',
      [item(1, 1, { state: 'failed', error: 'x' }), item(2, 2), item(3, 3)],
      {},
      1,
    )
    expect(batchResumeIndex(skipped)).toBe(1)
    const interrupted = batch('interrupted', [item(1, 1, { state: 'skipped_written' }), item(2, 2), item(3, 3)], {}, 1)
    expect(batchResumeIndex(interrupted)).toBe(1)
    const analyzing = batch(
      'interrupted',
      [item(1, 1, { state: 'done', analysis: 'ok' }), item(2, 2, { state: 'done', analysis: null }), item(3, 3)],
      { phase: 'analyze' },
      1,
    )
    expect(batchResumeIndex(analyzing)).toBe(1)
    expect(batchResumeIndex(batch('failed', [item(1, 1, { state: 'done' })], {}, 1))).toBeNull()
    expect(batchResumeIndex({ checkpoint: null, step_cursor: 0 })).toBeNull()
  })

  it('排队批量标签：未结束的批量中断点之后的待生成章节；已结束 / 无断点为空', () => {
    const running = batch('running', [item(1, 1, { state: 'done' }), item(2, 2), item(3, 3)], {}, 1)
    expect([...queuedBatchChapterIds(running)]).toEqual([2, 3])
    expect([...queuedBatchChapterIds(batch('paused', running.checkpoint.items, {}, 1))]).toEqual([2, 3])
    expect(queuedBatchChapterIds(batch('failed', running.checkpoint.items, {}, 1)).size).toBe(0)
    expect(queuedBatchChapterIds({ kind: 'chapter.batch', status: 'queued', checkpoint: null, step_cursor: 0 }).size).toBe(0)
    expect(queuedBatchChapterIds(null).size).toBe(0)
    // 失败停下后重试（排队中、沿用断点）：断点处的失败章节会重新生成
    const retried = batch('queued', [item(1, 1, { state: 'done' }), item(2, 2, { state: 'failed' }), item(3, 3)], {}, 1)
    expect([...queuedBatchChapterIds(retried)]).toEqual([2, 3])
  })

  it('计数、阶段文案解析', () => {
    expect(
      batchCounts([item(1, 1, { state: 'done' }), item(2, 2, { state: 'skipped_written' }), item(3, 3)]),
    ).toEqual({ processed: 2, total: 3, done: 1 })
    expect(batchStageIndex('第 3/20 章 · 分析中')).toBe(2)
    expect(batchStageIndex('排队中')).toBeNull()
    expect(batchCurrentPhase('第 3/20 章 · 分析中')).toBe('analyzing')
    expect(batchCurrentPhase('第 3/20 章 · 生成失败，第 1 次重试')).toBe('generating')
    expect(batchCurrentPhase(undefined)).toBe('generating')
  })
})

describe('清单行与文案', () => {
  it('清单行：已完成带字数与分析，失败带原因，待生成带目标字数，当前章显示生成中 / 分析中', () => {
    expect(batchRowView(item(1, 1, { state: 'done', words: 3120, analysis: 'ok', warning: '自动续写失败：超时' }))).toEqual({
      label: '已完成',
      color: 'success',
      detail: '3120 字 · 已分析',
      note: '自动续写失败：超时',
    })
    expect(batchRowView(item(1, 1, { state: 'failed', error: '额度不足', target_words: 2000 }))).toMatchObject({
      label: '失败',
      note: '额度不足',
      detail: '目标 2000 字',
    })
    expect(batchRowView(item(1, 1, { target_words: 2500, words_source: 'plan' }), 'generating')).toMatchObject({
      label: '生成中',
      color: 'processing',
      detail: '目标 2500 字（细纲）',
    })
    expect(batchRowView(item(1, 1, { state: 'done', words: 10 }), 'analyzing').label).toBe('分析中')
    expect(batchRowView(item(1, 1, { state: 'skipped_written' }))).toMatchObject({ label: '跳过（已写）', detail: '' })
    expect(batchRowView(item(1, 1, { state: 'skipped_missing' })).label).toBe('跳过（已删除）')
  })

  it('预计耗时文案', () => {
    expect(formatEta(0)).toBe('不到 1 分钟')
    expect(formatEta(7)).toBe('约 7 分钟')
    expect(formatEta(60)).toBe('约 1 小时')
    expect(formatEta(85)).toBe('约 1 小时 25 分钟')
  })

  it('汇总收窄与文案', () => {
    expect(parseBatchSummary(null)).toBeNull()
    expect(parseBatchSummary({ total: '3' })).toBeNull()
    const s = parseBatchSummary({
      total: 5,
      done: 2,
      skipped_written: 1,
      failed: [{ chapter_id: 3, chapter_no: 3, title: 'c', error: 'x' }],
      analysis_failed: [],
      remaining: 1,
      warnings: ['第 2 章：分析失败', 7],
      ended_early: true,
    })
    expect(s).toMatchObject({ skipped_missing: 0, warnings: ['第 2 章：分析失败'], ended_early: true })
    expect(batchSummaryText(s!)).toBe('共 5 章 · 已生成 2 章 · 跳过已写 1 章 · 失败 1 章 · 剩余 1 章（提前结束）')
  })
})

describe('断点按类型收窄', () => {
  it('只有含 items 的断点才是批量断点；拆书摘要不会被当成批量断点', () => {
    const cp: BatchCheckpoint = { phase: 'generate', current: null, since_review: 0, items: [item(1, 1)] }
    expect(batchCheckpoint(cp)).toBe(cp)
    expect(batchCheckpoint(null)).toBeNull()
    expect(batchCheckpoint(undefined)).toBeNull()
    const summary = {
      phase: 'digest' as const,
      batches_total: 3,
      batches_done: 1,
      batches_skipped: 0,
      stats: { summaries: 0, characters: 0, organizations: 0, world: 0, careers: 0, relations: 0, analyses_queued: 0, style_id: null, project_fields: [] },
      skipped: [],
      warnings: [],
      pause_reason: null,
    }
    expect(batchCheckpoint(summary)).toBeNull()
    expect(batchResumeIndex({ checkpoint: summary, step_cursor: 1 })).toBeNull()
  })
})
