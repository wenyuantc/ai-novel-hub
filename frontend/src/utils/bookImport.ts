// 拆书导入页的纯函数：上传前检查、提示标签与筛选、分卷展示、校正操作可用性、步骤流转、导入选项默认值与范围校验、
// 修订号冲突（409）后重新拉取、结果汇总文案。只被懒加载的拆书页与结果组件使用。

import { ApiError } from '../api/client'
import type {
  BookChapterView,
  BookChapterWarning,
  BookImport,
  BookImportSkipped,
  BookImportSummary,
  BookOp,
  BookRange,
  BookWarningCounts,
} from '../api/types'
import { formatWordCount } from './projectFields'
import { parseServerTime } from './time'

import { bookImportPause, bookImportPauseText } from './bookImportState'

export { canSkipBookImport } from './bookImportState'

/** 与后端 splitter.EXTENSIONS 一致（PDF 不支持） */
export const BOOK_EXTENSIONS = ['.txt', '.text', '.md', '.markdown', '.docx', '.epub'] as const
export const BOOK_ACCEPT = BOOK_EXTENSIONS.join(',')
/** 单文件上限 20MB（与后端 kb_parser.MAX_FILE_BYTES 一致） */
export const BOOK_MAX_FILE_BYTES = 20 * 1024 * 1024
/** 每个用户最多保留的未导入解析（与后端 MAX_PENDING 一致） */
export const BOOK_MAX_PENDING = 5
/** 导入章节数不超过它时默认勾选逐章深度分析 */
export const DEEP_ANALYZE_DEFAULT_MAX = 100
/** 自定义正则的长度上限（与后端 REGEX_MAX_LEN 一致） */
export const REGEX_MAX_LEN = 100

function extOf(name: string): string {
  const i = name.lastIndexOf('.')
  return i < 0 ? '' : name.slice(i).toLowerCase()
}

/** 上传前检查（后端仍是最终权威）：扩展名、非空、≤ 20MB、未导入的解析 < 5。通过时为 null。 */
export function precheckBookFile(file: { name: string; size: number }, pendingCount: number): string | null {
  if (!(BOOK_EXTENSIONS as readonly string[]).includes(extOf(file.name))) {
    return `《${file.name}》：不支持的文件类型，仅支持 txt、md、docx、epub`
  }
  if (file.size === 0) return `《${file.name}》：文件为空`
  if (file.size > BOOK_MAX_FILE_BYTES) return `《${file.name}》：文件过大，单个文件上限为 20MB`
  if (pendingCount >= BOOK_MAX_PENDING) {
    return `未导入的解析最多保留 ${BOOK_MAX_PENDING} 个，请先导入或删除已有的解析`
  }
  return null
}

/** 去掉扩展名的文件名（书名缺省值） */
export function stripExtension(filename: string): string {
  const i = filename.lastIndexOf('.')
  return (i > 0 ? filename.slice(0, i) : filename).trim()
}

/** 解析记录剩余天数（向上取整，过期为 0） */
export function remainingDays(expiresAt: string, now: number = Date.now()): number {
  const ms = parseServerTime(expiresAt).valueOf() - now
  return ms <= 0 ? 0 : Math.ceil(ms / 86_400_000)
}

export const formatChars = formatWordCount

// ---------------- 提示 ----------------

export const BOOK_WARNING_LABELS: Record<BookChapterWarning, { label: string; color: string; hint: string }> = {
  short: { label: '过短', color: 'gold', hint: '少于 300 字，可能是误切的标题或目录，可与下一章合并或删除' },
  long: { label: '过长', color: 'orange', hint: '超过 2 万字，可能漏切了标题，可打开全文在某一行拆分' },
  dup_title: { label: '标题重复', color: 'magenta', hint: '与其他章节标题相同（忽略序号）' },
  gap: { label: '序号断档', color: 'volcano', hint: '与上一章的序号不连续，可能缺章或误切' },
}

export const BOOK_WARNING_ORDER: BookChapterWarning[] = ['short', 'long', 'dup_title', 'gap']

/** 章节列表筛选：全部 / 有任意提示 / 某一类提示 */
export type ChapterFilter = 'all' | 'any' | BookChapterWarning

export function totalWarnings(counts: BookWarningCounts): number {
  return BOOK_WARNING_ORDER.reduce((sum, k) => sum + (counts[k] ?? 0), 0)
}

export function filterChapters(chapters: BookChapterView[], filter: ChapterFilter): BookChapterView[] {
  if (filter === 'all') return chapters
  if (filter === 'any') return chapters.filter((c) => c.warnings.length > 0)
  return chapters.filter((c) => c.warnings.includes(filter))
}

/** 筛选下拉的选项（带数量；数量为 0 的类别禁用） */
export function filterOptions(counts: BookWarningCounts): { value: ChapterFilter; label: string; disabled?: boolean }[] {
  const total = totalWarnings(counts)
  return [
    { value: 'all', label: '全部章节' },
    { value: 'any', label: `有提示（${total}）`, disabled: total === 0 },
    ...BOOK_WARNING_ORDER.map((k) => ({
      value: k,
      label: `${BOOK_WARNING_LABELS[k].label}（${counts[k] ?? 0}）`,
      disabled: (counts[k] ?? 0) === 0,
    })),
  ]
}

// ---------------- 分卷与校正操作 ----------------

/** 章节所在卷的展示名（不属于任何卷 / 全书没有分卷时为空串） */
export function volumeLabel(ch: Pick<BookChapterView, 'volume_no' | 'volume_title'>): string {
  if (!ch.volume_title) return ''
  return ch.volume_title
}

/** 是否为某一卷的第一章（与上一章卷号不同且本章在卷内） */
export function isVolumeStart(chapters: BookChapterView[], position: number): boolean {
  const ch = chapters[position]
  if (!ch || !ch.volume_title) return false
  return position === 0 || chapters[position - 1].volume_no !== ch.volume_no
}

export const BOOK_OP_LABELS: Record<BookOp, string> = {
  rename: '改标题',
  merge_next: '与下一章合并',
  split: '在某一行拆分',
  delete: '删除本章',
  start_volume: '从本章起新开一卷',
  rename_volume: '改卷名',
  clear_volume: '取消分卷（并入上一卷）',
}

/** 需要填写名称的操作 */
export function opNeedsTitle(op: BookOp): boolean {
  return op === 'rename' || op === 'start_volume' || op === 'rename_volume'
}

/** 某章可用的操作（与后端 apply_op 的 400 条件一致：最后一章不能合并、只剩 1 章不能删、不在卷内不能改卷名 / 取消分卷） */
export function availableOps(ch: Pick<BookChapterView, 'index' | 'volume_title'>, total: number): BookOp[] {
  const ops: BookOp[] = ['rename']
  if (ch.index < total) ops.push('merge_next')
  ops.push('split')
  if (total > 1) ops.push('delete')
  ops.push('start_volume')
  if (ch.volume_title) ops.push('rename_volume', 'clear_volume')
  return ops
}

/** 全文中可以作为拆分点的行（行号从 1 起；第 1 行与空行不行） */
export function canSplitAt(lines: string[], line: number): boolean {
  return line >= 2 && line <= lines.length && lines[line - 1].trim() !== ''
}

// ---------------- 修订号冲突 ----------------

/** 修订号过期的 409 在响应体里带的标记（后端 book_import.service.STALE_CODE，经 AppError.extra 输出） */
export const STALE_REVISION_CODE = 'stale_revision'

/** 修订号过期：409 且响应体 `code === 'stale_revision'`；其他 409（如已有拆书任务、解析满 5 个）不带标记，不算。不匹配 detail 文案 */
export function isStaleRevision(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409 && err.body?.code === STALE_REVISION_CODE
}

export type RefreshOutcome<T> = { ok: true; value: T } | { ok: false; record: BookImport }

/**
 * 执行一次带修订号的写操作；修订号过期（409）时重新拉取解析记录并返回 `{ok: false, record}`，由页面替换本地数据并提示。
 * 其他错误原样抛出。
 */
export async function withStaleRefresh<T>(action: () => Promise<T>, refetch: () => Promise<BookImport>): Promise<RefreshOutcome<T>> {
  try {
    return { ok: true, value: await action() }
  } catch (err) {
    if (!isStaleRevision(err)) throw err
    return { ok: false, record: await refetch() }
  }
}

// ---------------- 步骤 ----------------

/** 0 上传 → 1 校正 → 2 选项与预估 → 3 开始 */
export type BookStep = 0 | 1 | 2 | 3

export const BOOK_STEPS: { title: string }[] = [{ title: '上传' }, { title: '校正' }, { title: '选项与预估' }, { title: '开始' }]

export interface StepContext {
  record: Pick<BookImport, 'chapter_count'> | null
  /** 选项校验错误（null = 合法） */
  optionsError: string | null
}

/** 能否进入某一步：校正需要解析记录，选项需要至少 1 章，开始需要选项合法 */
export function canEnterStep(step: BookStep, ctx: StepContext): boolean {
  if (step === 0) return true
  if (!ctx.record) return false
  if (step === 1) return true
  if (ctx.record.chapter_count === 0) return false
  if (step === 2) return true
  return ctx.optionsError === null
}

/** 想去 desired，实际能到的最远一步（不满足前置条件时退回；如解析记录被删除后回到上传） */
export function resolveStep(desired: BookStep, ctx: StepContext): BookStep {
  for (let s = desired; s > 0; s--) {
    if (canEnterStep(s as BookStep, ctx)) return s as BookStep
  }
  return 0
}

// ---------------- 导入选项 ----------------

export interface BookImportOptions {
  title: string
  genre: string
  importRange: BookRange
  reverseRange: BookRange
  extractStyle: boolean
  deepAnalyze: boolean
  aiConfigId: number | null
}

export function rangeSize(range: BookRange): number {
  return Math.max(0, range[1] - range[0] + 1)
}

/** 深度分析默认值：导入章节数 ≤ 100 时勾选 */
export function defaultDeepAnalyze(importChapters: number): boolean {
  return importChapters <= DEEP_ANALYZE_DEFAULT_MAX
}

/** 默认选项：书名取解析出的书名（缺省为去扩展名的文件名）、导入全部（最多 3000 章）、反推范围 = 导入范围 */
export function defaultOptions(record: Pick<BookImport, 'book_title' | 'filename' | 'chapter_count' | 'max_import_chapters'>): BookImportOptions {
  const end = Math.max(1, Math.min(record.chapter_count, record.max_import_chapters))
  const range: BookRange = [1, end]
  return {
    title: (record.book_title || stripExtension(record.filename)).slice(0, 200),
    genre: '',
    importRange: range,
    reverseRange: range,
    extractStyle: true,
    deepAnalyze: defaultDeepAnalyze(rangeSize(range)),
    aiConfigId: null,
  }
}

function clamp(n: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, Math.round(n)))
}

/** 范围规整到 [lo, hi] 内并保证起 ≤ 止 */
export function clampRange(range: BookRange, lo: number, hi: number): BookRange {
  const a = clamp(range[0], lo, hi)
  const b = clamp(range[1], lo, hi)
  return a <= b ? [a, b] : [b, a]
}

/**
 * 修改导入范围：反推范围收进新的导入范围（原先等于导入范围时跟随）；用户没有手动改过深度分析时按新章数重新取默认值。
 */
export function withImportRange(opts: BookImportOptions, range: BookRange, deepTouched: boolean): BookImportOptions {
  const followed = opts.reverseRange[0] === opts.importRange[0] && opts.reverseRange[1] === opts.importRange[1]
  const reverseRange = followed ? range : clampRange(opts.reverseRange, range[0], range[1])
  return {
    ...opts,
    importRange: range,
    reverseRange,
    deepAnalyze: deepTouched ? opts.deepAnalyze : defaultDeepAnalyze(rangeSize(range)),
  }
}

/** 选项校验（与后端 400 一致）；合法时为 null */
export function optionsError(opts: BookImportOptions, record: Pick<BookImport, 'chapter_count' | 'max_import_chapters'>): string | null {
  if (!opts.title.trim()) return '请填写书名'
  if (opts.title.trim().length > 200) return '书名不超过 200 字'
  if (opts.genre.trim().length > 100) return '题材不超过 100 字'
  const [a, b] = opts.importRange
  if (!(Number.isInteger(a) && Number.isInteger(b) && a >= 1 && a <= b && b <= record.chapter_count)) return '导入范围不正确'
  if (rangeSize(opts.importRange) > record.max_import_chapters) {
    return `一次最多导入 ${record.max_import_chapters} 章，请缩小导入范围`
  }
  const [c, d] = opts.reverseRange
  if (!(Number.isInteger(c) && Number.isInteger(d) && c <= d && c >= a && d <= b)) return '反推范围必须在导入范围之内'
  return null
}


// ---------------- 断点摘要 ----------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

const PHASES = new Set(['digest', 'synthesize', 'apply', 'style', 'analyze', 'done'])

/**
 * 把 TaskOut.checkpoint / done.result / paused.result 收窄为拆书摘要；结构不符时为 null。
 * 缺省的计数 / 列表补默认值，避免旧数据或部分字段缺失时渲染报错。
 */
export function bookImportSummary(value: unknown): BookImportSummary | null {
  if (!isRecord(value) || typeof value.phase !== 'string' || !PHASES.has(value.phase)) return null
  const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : 0)
  const stats = isRecord(value.stats) ? value.stats : {}
  return {
    phase: value.phase as BookImportSummary['phase'],
    batches_total: num(value.batches_total),
    batches_done: num(value.batches_done),
    batches_skipped: num(value.batches_skipped),
    stats: {
      summaries: num(stats.summaries),
      characters: num(stats.characters),
      organizations: num(stats.organizations),
      world: num(stats.world),
      careers: num(stats.careers),
      relations: num(stats.relations),
      analyses_queued: num(stats.analyses_queued),
      style_id: typeof stats.style_id === 'number' ? stats.style_id : null,
      project_fields: Array.isArray(stats.project_fields) ? stats.project_fields.filter((f) => typeof f === 'string') : [],
    },
    skipped: Array.isArray(value.skipped)
      ? (value.skipped.filter((s) => isRecord(s) && (s.stage === 'digest' || s.stage === 'synthesize')) as BookImportSummary['skipped'])
      : [],
    warnings: Array.isArray(value.warnings) ? value.warnings.filter((w): w is string => typeof w === 'string') : [],
    pause_reason: bookImportPause(value),
    ...(value.ended_early === true ? { ended_early: true } : {}),
  }
}

// ---------------- 结果汇总 ----------------

export const BOOK_IMPORT_PHASE_LABELS: Record<BookImportSummary['phase'], string> = {
  digest: '通读',
  synthesize: '汇总',
  apply: '写入设定',
  style: '提取风格',
  analyze: '排队深度分析',
  done: '完成',
}

const PROJECT_FIELD_LABELS: Record<string, string> = {
  synopsis: '简介',
  theme: '主题',
  tags: '类型标签',
  selling_points: '核心卖点',
  audience: '目标读者',
  genre: '题材',
  premise: '一句话设定',
}

export function projectFieldLabels(fields: string[]): string[] {
  return fields.map((f) => PROJECT_FIELD_LABELS[f] ?? f)
}

/** 写入统计（固定顺序） */
export function statRows(summary: BookImportSummary): { key: string; label: string; value: number | string }[] {
  const s = summary.stats
  return [
    { key: 'summaries', label: '章节摘要', value: s.summaries },
    { key: 'characters', label: '角色', value: s.characters },
    { key: 'organizations', label: '组织', value: s.organizations },
    { key: 'world', label: '世界观', value: s.world },
    { key: 'careers', label: '职业体系', value: s.careers },
    { key: 'relations', label: '角色关系', value: s.relations },
    { key: 'style', label: '写作风格', value: s.style_id !== null ? '已创建' : '—' },
    { key: 'analyses', label: '排队分析', value: s.analyses_queued },
  ]
}

/** 章号列表压缩为区间文案：[1,2,3,5] → 「1–3、5」 */
export function compressNumbers(nos: number[]): string {
  const sorted = [...new Set(nos)].sort((a, b) => a - b)
  const parts: string[] = []
  let i = 0
  while (i < sorted.length) {
    let j = i
    while (j + 1 < sorted.length && sorted[j + 1] === sorted[j] + 1) j++
    parts.push(i === j ? `${sorted[i]}` : `${sorted[i]}–${sorted[j]}`)
    i = j + 1
  }
  return parts.join('、')
}

/** 跳过项的说明 */
export function skippedText(item: BookImportSkipped): string {
  if (item.stage === 'synthesize') return '汇总被跳过，已使用各批结果的代码合并'
  const nos = item.chapter_nos.length ? `（第 ${compressNumbers(item.chapter_nos)} 章，未写摘要）` : ''
  return `通读第 ${item.batch_index + 1} 批被跳过${nos}`
}

/** 进度一句话：通读 x/y 批、跳过 n 批、当前阶段 */
export function summaryLine(summary: BookImportSummary): string {
  const parts = [`通读 ${summary.batches_done}/${summary.batches_total} 批`]
  if (summary.batches_skipped > 0) parts.push(`跳过 ${summary.batches_skipped} 批`)
  parts.push(`阶段：${BOOK_IMPORT_PHASE_LABELS[summary.phase]}`)
  if (summary.ended_early) parts.push('已提前结束')
  return parts.join(' · ')
}

/** 暂停原因说明 */
export function pauseText(summary: BookImportSummary): string | null {
  return summary.pause_reason ? bookImportPauseText(summary.pause_reason) : null
}
