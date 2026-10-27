import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/client'
import type { BookChapterView, BookImport, Task } from '../api/types'
import {
  availableOps,
  bookImportSummary,
  canEnterStep,
  canSkipBookImport,
  canSplitAt,
  clampRange,
  compressNumbers,
  defaultDeepAnalyze,
  defaultOptions,
  filterChapters,
  filterOptions,
  isStaleRevision,
  isVolumeStart,
  optionsError,
  pauseText,
  precheckBookFile,
  remainingDays,
  resolveStep,
  skippedText,
  statRows,
  stripExtension,
  summaryLine,
  withImportRange,
  withStaleRefresh,
} from './bookImport'
import { bookImportBatches } from './bookImportState'

function chapter(index: number, over: Partial<BookChapterView> = {}): BookChapterView {
  return {
    index,
    title: `第${index}章`,
    volume_no: 1,
    volume_title: '',
    source_no: index,
    chars: 3000,
    first_line: '开头',
    warnings: [],
    ...over,
  }
}

function record(count: number, over: Partial<BookImport> = {}): BookImport {
  return {
    id: 1,
    filename: '凡人修仙传.txt',
    kind: 'txt',
    size: 1024,
    book_title: '',
    split_mode: 'auto',
    split_pattern: null,
    revision: 3,
    notices: [],
    chapters: Array.from({ length: Math.min(count, 5) }, (_, i) => chapter(i + 1)),
    warning_counts: { short: 0, long: 0, dup_title: 0, gap: 0 },
    chapter_count: count,
    total_chars: count * 3000,
    too_many: count > 3000,
    max_import_chapters: 3000,
    expires_at: '2026-11-03T08:00:00',
    created_at: '2026-10-27T08:00:00',
    updated_at: '2026-10-27T08:00:00',
    ...over,
  }
}

const STALE = new ApiError(409, '解析结果已被修改，请刷新后重试', { detail: '解析结果已被修改，请刷新后重试', code: 'stale_revision' })

describe('上传前检查', () => {
  it('扩展名 / 空文件 / 20MB / 5 个上限，文案与后端一致', () => {
    expect(precheckBookFile({ name: '书.TXT', size: 10 }, 0)).toBeNull()
    expect(precheckBookFile({ name: '书.epub', size: 10 }, 4)).toBeNull()
    expect(precheckBookFile({ name: '书.pdf', size: 10 }, 0)).toContain('不支持的文件类型')
    expect(precheckBookFile({ name: '书.txt', size: 0 }, 0)).toContain('文件为空')
    expect(precheckBookFile({ name: '书.txt', size: 20 * 1024 * 1024 + 1 }, 0)).toContain('上限为 20MB')
    expect(precheckBookFile({ name: '书.txt', size: 10 }, 5)).toBe('未导入的解析最多保留 5 个，请先导入或删除已有的解析')
  })

  it('书名缺省值与剩余天数', () => {
    expect(stripExtension('凡人修仙传.txt')).toBe('凡人修仙传')
    expect(stripExtension('.txt')).toBe('.txt')
    const now = Date.UTC(2026, 9, 27, 8)
    expect(remainingDays('2026-11-03T08:00:00', now)).toBe(7)
    expect(remainingDays('2026-10-27T09:00:00', now)).toBe(1)
    expect(remainingDays('2026-10-26T08:00:00', now)).toBe(0)
  })
})

describe('提示筛选与校正操作', () => {
  const chapters = [
    chapter(1, { warnings: ['short'] }),
    chapter(2),
    chapter(3, { warnings: ['long', 'gap'] }),
  ]

  it('按提示类型筛选，选项带数量、0 个的禁用', () => {
    expect(filterChapters(chapters, 'all')).toHaveLength(3)
    expect(filterChapters(chapters, 'any').map((c) => c.index)).toEqual([1, 3])
    expect(filterChapters(chapters, 'gap').map((c) => c.index)).toEqual([3])
    const opts = filterOptions({ short: 1, long: 1, dup_title: 0, gap: 1 })
    expect(opts.find((o) => o.value === 'any')?.label).toBe('有提示（3）')
    expect(opts.find((o) => o.value === 'dup_title')?.disabled).toBe(true)
  })

  it('可用操作：最后一章不能合并、只剩 1 章不能删、不在卷内不能改卷名 / 取消分卷', () => {
    expect(availableOps(chapter(1), 3)).toEqual(['rename', 'merge_next', 'split', 'delete', 'start_volume'])
    expect(availableOps(chapter(3), 3)).not.toContain('merge_next')
    expect(availableOps(chapter(1), 1)).toEqual(['rename', 'split', 'start_volume'])
    expect(availableOps(chapter(2, { volume_title: '第一卷' }), 3)).toContain('clear_volume')
    expect(availableOps(chapter(2, { volume_title: '第一卷' }), 3)).toContain('rename_volume')
  })

  it('拆分行：第 1 行、空行、越界不行', () => {
    const lines = ['第一章', '', '正文', '  ']
    expect(canSplitAt(lines, 1)).toBe(false)
    expect(canSplitAt(lines, 2)).toBe(false)
    expect(canSplitAt(lines, 3)).toBe(true)
    expect(canSplitAt(lines, 4)).toBe(false)
    expect(canSplitAt(lines, 5)).toBe(false)
  })

  it('卷首章：卷名非空且与上一章卷号不同', () => {
    const list = [
      chapter(1, { volume_no: 0 }),
      chapter(2, { volume_no: 1, volume_title: '第一卷' }),
      chapter(3, { volume_no: 1, volume_title: '第一卷' }),
      chapter(4, { volume_no: 2, volume_title: '第二卷' }),
    ]
    expect(list.map((_, i) => isVolumeStart(list, i))).toEqual([false, true, false, true])
  })
})

describe('步骤流转', () => {
  it('上传 → 校正需要解析记录；选项需要至少 1 章；开始需要选项合法', () => {
    const none = { record: null, optionsError: null }
    expect(canEnterStep(0, none)).toBe(true)
    expect(canEnterStep(1, none)).toBe(false)
    const ok = { record: { chapter_count: 10 }, optionsError: null }
    expect([0, 1, 2, 3].map((s) => canEnterStep(s as 0 | 1 | 2 | 3, ok))).toEqual([true, true, true, true])
    const bad = { record: { chapter_count: 10 }, optionsError: '请填写书名' }
    expect(canEnterStep(2, bad)).toBe(true)
    expect(canEnterStep(3, bad)).toBe(false)
    expect(canEnterStep(2, { record: { chapter_count: 0 }, optionsError: null })).toBe(false)
  })

  it('前置条件不满足时退回能到的最远一步（解析被删 → 上传；选项变得不合法 → 选项）', () => {
    expect(resolveStep(3, { record: { chapter_count: 10 }, optionsError: null })).toBe(3)
    expect(resolveStep(3, { record: { chapter_count: 10 }, optionsError: '导入范围不正确' })).toBe(2)
    expect(resolveStep(2, { record: { chapter_count: 0 }, optionsError: null })).toBe(1)
    expect(resolveStep(3, { record: null, optionsError: null })).toBe(0)
  })
})

describe('导入选项', () => {
  it('深度分析默认值以 100 章为界', () => {
    expect(defaultDeepAnalyze(100)).toBe(true)
    expect(defaultDeepAnalyze(101)).toBe(false)
    expect(defaultOptions(record(100)).deepAnalyze).toBe(true)
    expect(defaultOptions(record(101)).deepAnalyze).toBe(false)
  })

  it('默认值：书名取解析出的书名或去扩展名的文件名，导入全部（最多 3000 章），反推 = 导入', () => {
    const o = defaultOptions(record(50))
    expect(o).toMatchObject({ title: '凡人修仙传', importRange: [1, 50], reverseRange: [1, 50], extractStyle: true, aiConfigId: null })
    expect(defaultOptions(record(50, { book_title: '原书名' })).title).toBe('原书名')
    const big = defaultOptions(record(3500))
    expect(big.importRange).toEqual([1, 3000])
    expect(big.deepAnalyze).toBe(false)
  })

  it('改导入范围：反推范围跟随或收进新范围；没手动改过时深度分析按新章数重算，改过后保持', () => {
    const o = defaultOptions(record(300))
    expect(o.deepAnalyze).toBe(false)
    const narrowed = withImportRange(o, [1, 80], false)
    expect(narrowed.reverseRange).toEqual([1, 80])
    expect(narrowed.deepAnalyze).toBe(true)
    expect(withImportRange(o, [1, 80], true).deepAnalyze).toBe(false)
    const custom = { ...o, reverseRange: [50, 200] as [number, number] }
    expect(withImportRange(custom, [100, 150], false).reverseRange).toEqual([100, 150])
    expect(withImportRange(custom, [1, 120], false).reverseRange).toEqual([50, 120])
  })

  it('校验与范围规整', () => {
    const r = record(4000)
    const o = defaultOptions(r)
    expect(optionsError(o, r)).toBeNull()
    expect(optionsError({ ...o, title: '  ' }, r)).toBe('请填写书名')
    expect(optionsError({ ...o, importRange: [1, 3001] }, r)).toBe('一次最多导入 3000 章，请缩小导入范围')
    expect(optionsError({ ...o, importRange: [5, 4001] }, r)).toBe('导入范围不正确')
    expect(optionsError({ ...o, reverseRange: [0, 10] }, r)).toBe('反推范围必须在导入范围之内')
    expect(clampRange([9, 2], 1, 5)).toEqual([2, 5])
    expect(clampRange([-3, 2.6], 1, 5)).toEqual([1, 3])
  })
})

describe('修订号冲突', () => {
  it('409 带 code=stale_revision 时重新拉取并返回最新记录；其他错误原样抛出', async () => {
    const latest = record(10, { revision: 4 })
    const refetch = vi.fn(async () => latest)
    const stale = await withStaleRefresh(async () => {
      throw STALE
    }, refetch)
    expect(stale).toEqual({ ok: false, record: latest })
    expect(refetch).toHaveBeenCalledTimes(1)

    const ok = await withStaleRefresh(async () => 'x', refetch)
    expect(ok).toEqual({ ok: true, value: 'x' })
    expect(refetch).toHaveBeenCalledTimes(1)

    const conflict = new ApiError(409, '已有进行中或待处理的拆书任务，请等待其完成或结束后再试')
    await expect(withStaleRefresh(async () => Promise.reject(conflict), refetch)).rejects.toBe(conflict)
    await expect(withStaleRefresh(async () => Promise.reject(new ApiError(400, '标题不能为空')), refetch)).rejects.toThrow('标题不能为空')
    expect(refetch).toHaveBeenCalledTimes(1)
    expect(isStaleRevision(STALE)).toBe(true)
    expect(isStaleRevision(conflict)).toBe(false)
    // 只认标记、不认文案：同样的文案但没有 code 不算，文案改动但带 code 仍算
    expect(isStaleRevision(new ApiError(409, '解析结果已被修改，请刷新后重试'))).toBe(false)
    expect(isStaleRevision(new ApiError(409, '别的说法', { code: 'stale_revision' }))).toBe(true)
    expect(isStaleRevision(new ApiError(400, 'x', { code: 'stale_revision' }))).toBe(false)
  })
})

describe('拆书摘要与跳过本批', () => {
  const raw = {
    phase: 'digest',
    batches_total: 4,
    batches_done: 2,
    batches_skipped: 1,
    stats: { summaries: 30, characters: 5, organizations: 1, world: 3, careers: 1, relations: 4, analyses_queued: 0, style_id: 9, project_fields: ['synopsis', 'tags', 'x'] },
    skipped: [
      { stage: 'digest', batch_index: 1, chapter_nos: [11, 12, 13, 15] },
      { stage: 'synthesize', chapter_nos: [] },
      { stage: 'other' },
    ],
    warnings: ['风格提取失败', 3],
    pause_reason: { reason: 'failed', phase: 'digest', batch_index: 2, error: '模型调用失败：超时' },
  }

  it('收窄并补默认值；结构不符为 null', () => {
    const s = bookImportSummary(raw)!
    expect(s.skipped).toHaveLength(2)
    expect(s.warnings).toEqual(['风格提取失败'])
    expect(s.stats.style_id).toBe(9)
    expect(bookImportSummary({ phase: 'x' })).toBeNull()
    expect(bookImportSummary(null)).toBeNull()
    expect(bookImportSummary({ phase: 'done' })?.stats.summaries).toBe(0)
    expect(bookImportSummary({ ...raw, ended_early: true })?.ended_early).toBe(true)
    expect(bookImportBatches(raw)).toEqual({ done: 3, total: 4 })
    expect(bookImportBatches({ batches_total: 0 })).toBeNull()
  })

  it('文案：进度、暂停原因、跳过项、统计', () => {
    const s = bookImportSummary(raw)!
    expect(summaryLine(s)).toBe('通读 2/4 批 · 跳过 1 批 · 阶段：通读')
    expect(pauseText(s)).toBe('通读第 3 批失败（已自动重试 2 次）：模型调用失败：超时')
    expect(skippedText(s.skipped[0])).toBe('通读第 2 批被跳过（第 11–13、15 章，未写摘要）')
    expect(skippedText(s.skipped[1])).toBe('汇总被跳过，已使用各批结果的代码合并')
    expect(compressNumbers([5, 1, 2, 3, 3])).toBe('1–3、5')
    const rows = statRows(s)
    expect(rows.find((r) => r.key === 'style')?.value).toBe('已创建')
    expect(rows.find((r) => r.key === 'summaries')?.value).toBe(30)
  })

  it('「跳过本批」只在 book.import 因失败暂停时出现', () => {
    const t = (over: Partial<Task>) => ({ kind: 'book.import' as const, status: 'paused' as const, checkpoint: null, ...over })
    expect(canSkipBookImport(t({ checkpoint: bookImportSummary(raw) }))).toBe(true)
    expect(canSkipBookImport(t({ checkpoint: bookImportSummary({ ...raw, pause_reason: null }) }))).toBe(false)
    expect(canSkipBookImport(t({ checkpoint: bookImportSummary({ ...raw, pause_reason: { reason: 'review' } }) }))).toBe(false)
    expect(canSkipBookImport(t({ status: 'running', checkpoint: bookImportSummary(raw) }))).toBe(false)
    expect(canSkipBookImport(t({ status: 'failed', checkpoint: bookImportSummary(raw) }))).toBe(false)
    expect(canSkipBookImport({ kind: 'chapter.batch', status: 'paused', checkpoint: null })).toBe(false)
    // 其他类型的断点碰巧带同样结构的 pause_reason 也不显示（按 kind 判定）
    expect(canSkipBookImport({ kind: 'chapter.batch', status: 'paused', checkpoint: bookImportSummary(raw) })).toBe(false)
    expect(canSkipBookImport(t({ checkpoint: null }))).toBe(false)
  })
})
