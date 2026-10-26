// 伏笔管理：CRUD、快捷操作、历史、统计、章节提醒，以及表单 / 时间线 / 提醒 / 确认表单用的纯函数

import { api } from './client'
import { foreshadowStatusLabel } from './foreshadowLabels'
import type {
  Foreshadow,
  ForeshadowCandidate,
  ForeshadowHistory,
  ForeshadowInput,
  ForeshadowListQuery,
  ForeshadowOverrides,
  ForeshadowReminders,
  ForeshadowStats,
  ForeshadowStatus,
  ForeshadowUpdate,
} from './types'

const base = (pid: number) => `/projects/${pid}/foreshadows`

export const foreshadowApi = {
  list: (pid: number, query: ForeshadowListQuery = {}) =>
    api.get<Foreshadow[]>(base(pid), {
      status: query.status,
      overdue: query.overdue || undefined,
      long_term: query.long_term || undefined,
      character_id: query.character_id,
      sort: query.sort,
    }),
  get: (pid: number, id: number) => api.get<Foreshadow>(`${base(pid)}/${id}`),
  create: (pid: number, body: ForeshadowInput) => api.post<Foreshadow>(base(pid), body),
  update: (pid: number, id: number, body: ForeshadowUpdate) => api.patch<Foreshadow>(`${base(pid)}/${id}`, body),
  remove: (pid: number, id: number) => api.del(`${base(pid)}/${id}`),
  /** 标记已埋：chapter_no 省略时沿用已有的实际埋设章，都没有 → 400 */
  plant: (pid: number, id: number, chapterNo: number | null) =>
    api.post<Foreshadow>(`${base(pid)}/${id}/plant`, { chapter_no: chapterNo }),
  /** 标记回收：partial = 部分回收 */
  resolve: (pid: number, id: number, chapterNo: number | null, partial = false) =>
    api.post<Foreshadow>(`${base(pid)}/${id}/resolve`, { chapter_no: chapterNo, partial }),
  abandon: (pid: number, id: number) => api.post<Foreshadow>(`${base(pid)}/${id}/abandon`),
  /** 新在前 */
  history: (pid: number, id: number) => api.get<ForeshadowHistory[]>(`${base(pid)}/${id}/history`),
  stats: (pid: number) => api.get<ForeshadowStats>(`${base(pid)}/stats`),
  reminders: (pid: number, cid: number) =>
    api.get<ForeshadowReminders>(`/projects/${pid}/chapters/${cid}/foreshadow-reminders`),
}

// ---------------- 标签 ----------------

export { FORESHADOW_STATUS, FORESHADOW_STATUSES, foreshadowStatusLabel } from './foreshadowLabels'

export const FORESHADOW_SOURCE_LABELS: Record<string, string> = { manual: '手动', analysis: '章节分析' }

export const FORESHADOW_FIELD_LABELS: Record<string, string> = {
  title: '标题',
  content: '内容',
  plant_hint: '埋法要点',
  reveal_hint: '回收要点',
  note: '备注',
  status: '状态',
  planned_plant_chapter: '计划埋设章',
  actual_plant_chapter: '实际埋设章',
  planned_resolve_chapter: '计划回收章',
  actual_resolve_chapter: '实际回收章',
  importance: '重要度',
  include_in_context: '注入上下文',
  characters: '关联角色',
}

const CHAPTER_FIELDS = new Set([
  'planned_plant_chapter',
  'actual_plant_chapter',
  'planned_resolve_chapter',
  'actual_resolve_chapter',
])

/** 是否处于未收束状态（已埋 / 部分回收）：超期、长线判定的前提 */
export function isOpenForeshadow(f: Pick<Foreshadow, 'status'>): boolean {
  return f.status === 'planted' || f.status === 'partial'
}

/** 与后端 overdue_by 同口径：已埋 / 部分回收、有计划回收章且早于参照章时的超期章数，否则 0 */
export function overdueBy(
  f: Pick<Foreshadow, 'status' | 'planned_resolve_chapter'>,
  currentChapter: number,
): number {
  if (!isOpenForeshadow(f) || f.planned_resolve_chapter === null) return 0
  return f.planned_resolve_chapter < currentChapter ? currentChapter - f.planned_resolve_chapter : 0
}

/** 与后端 is_long_term 同口径：已埋 / 部分回收且没有计划回收章 */
export function isLongTerm(f: Pick<Foreshadow, 'status' | 'planned_resolve_chapter'>): boolean {
  return isOpenForeshadow(f) && f.planned_resolve_chapter === null
}

function chapterText(n: number | null | undefined): string {
  return typeof n === 'number' ? `第 ${n} 章` : '-'
}

/** 列表里的章节说明：「埋：第 3 章（计划第 2 章）· 收：计划第 8 章」 */
export function foreshadowChapterSummary(
  f: Pick<
    Foreshadow,
    'planned_plant_chapter' | 'actual_plant_chapter' | 'planned_resolve_chapter' | 'actual_resolve_chapter'
  >,
): { plant: string; resolve: string } {
  const pair = (actual: number | null, planned: number | null, none: string) => {
    if (actual !== null) return planned !== null && planned !== actual ? `第 ${actual} 章（计划第 ${planned} 章）` : `第 ${actual} 章`
    if (planned !== null) return `计划第 ${planned} 章`
    return none
  }
  return {
    plant: pair(f.actual_plant_chapter, f.planned_plant_chapter, '未定'),
    resolve: pair(f.actual_resolve_chapter, f.planned_resolve_chapter, '长线（未定）'),
  }
}

/** 历史记录里的值展示 */
export function formatForeshadowValue(field: string, value: unknown): string {
  if (value === null || value === undefined) return '（无）'
  if (field === 'status') return foreshadowStatusLabel(String(value)).label
  if (CHAPTER_FIELDS.has(field)) return typeof value === 'number' ? chapterText(value) : String(value)
  if (field === 'include_in_context') return value ? '开启' : '关闭'
  if (field === 'importance') return `${String(value)} 星`
  if (field === 'characters') return Array.isArray(value) ? value.map(String).join('、') || '（无）' : String(value)
  const s = typeof value === 'string' ? value : JSON.stringify(value)
  return s.trim() ? s : '（空）'
}

// ---------------- 表单 ----------------

export interface ForeshadowFormValues {
  title: string
  content: string
  plant_hint: string
  reveal_hint: string
  status: ForeshadowStatus
  planned_plant_chapter: number | null
  actual_plant_chapter: number | null
  planned_resolve_chapter: number | null
  actual_resolve_chapter: number | null
  importance: number
  include_in_context: boolean
  note: string
  character_ids: number[]
}

export const EMPTY_FORESHADOW_FORM: ForeshadowFormValues = {
  title: '',
  content: '',
  plant_hint: '',
  reveal_hint: '',
  status: 'pending',
  planned_plant_chapter: null,
  actual_plant_chapter: null,
  planned_resolve_chapter: null,
  actual_resolve_chapter: null,
  importance: 3,
  include_in_context: true,
  note: '',
  character_ids: [],
}

export function foreshadowToForm(f: Foreshadow): ForeshadowFormValues {
  return {
    title: f.title,
    content: f.content,
    plant_hint: f.plant_hint,
    reveal_hint: f.reveal_hint,
    status: f.status,
    planned_plant_chapter: f.planned_plant_chapter,
    actual_plant_chapter: f.actual_plant_chapter,
    planned_resolve_chapter: f.planned_resolve_chapter,
    actual_resolve_chapter: f.actual_resolve_chapter,
    importance: f.importance,
    include_in_context: f.include_in_context,
    note: f.note,
    character_ids: [...f.character_ids],
  }
}

function chapterOrNull(v: unknown): number | null {
  return typeof v === 'number' && Number.isInteger(v) && v >= 1 ? v : null
}

/** 表单预检（文案与后端 400 一致）；无错误返回空串 */
export function foreshadowFormError(v: Pick<ForeshadowFormValues, 'title' | 'status' | 'actual_plant_chapter' | 'actual_resolve_chapter'>): string {
  if (!(v.title ?? '').trim()) return '伏笔标题不能为空'
  if (v.status === 'planted' && chapterOrNull(v.actual_plant_chapter) === null) return '请填写实际埋设章节'
  if ((v.status === 'resolved' || v.status === 'partial') && chapterOrNull(v.actual_resolve_chapter) === null)
    return '请填写实际回收章节'
  return ''
}

/** 表单 → 创建 / 更新请求体：文本去首尾空白，章节字段一律显式给出（null = 清空），角色去重 */
export function formToForeshadowInput(v: ForeshadowFormValues): Required<ForeshadowInput> {
  return {
    title: (v.title ?? '').trim(),
    content: (v.content ?? '').trim(),
    plant_hint: (v.plant_hint ?? '').trim(),
    reveal_hint: (v.reveal_hint ?? '').trim(),
    status: v.status,
    planned_plant_chapter: chapterOrNull(v.planned_plant_chapter),
    actual_plant_chapter: chapterOrNull(v.actual_plant_chapter),
    planned_resolve_chapter: chapterOrNull(v.planned_resolve_chapter),
    actual_resolve_chapter: chapterOrNull(v.actual_resolve_chapter),
    importance: Math.min(5, Math.max(1, Math.round(v.importance || 3))),
    include_in_context: v.include_in_context !== false,
    note: (v.note ?? '').trim(),
    character_ids: [...new Set(v.character_ids ?? [])],
  }
}

// ---------------- 快捷操作的默认章节 ----------------

/** 标记已埋时的默认章节：已有实际埋设章 → 计划埋设章 → 项目当前章（0 时为 null） */
export function defaultPlantChapter(f: Foreshadow, currentChapter: number): number | null {
  return f.actual_plant_chapter ?? f.planned_plant_chapter ?? (currentChapter > 0 ? currentChapter : null)
}

/** 标记回收时的默认章节：项目当前章（不早于实际埋设章）→ 计划回收章 */
export function defaultResolveChapter(f: Foreshadow, currentChapter: number): number | null {
  if (currentChapter > 0) return Math.max(currentChapter, f.actual_plant_chapter ?? 0)
  return f.planned_resolve_chapter ?? f.actual_plant_chapter
}

// ---------------- 分析：确认新伏笔 ----------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function str(value: unknown): string {
  return typeof value === 'string' ? value : typeof value === 'number' ? String(value) : ''
}

export interface NewForeshadowFormValues {
  title: string
  content: string
  plant_hint: string
  reveal_hint: string
  planned_resolve_chapter: number | null
  importance: number
  character_ids: number[]
}

/** foreshadow_new 条目的 after（{title, content, plant_hint, planned_resolve_chapter, importance}）→ 确认表单初值 */
export function newForeshadowDraft(after: unknown): NewForeshadowFormValues {
  const a = isRecord(after) ? after : {}
  const importance = typeof a.importance === 'number' ? Math.min(5, Math.max(1, Math.round(a.importance))) : 3
  return {
    title: str(a.title),
    content: str(a.content),
    plant_hint: str(a.plant_hint),
    reveal_hint: str(a.reveal_hint),
    planned_resolve_chapter: chapterOrNull(a.planned_resolve_chapter),
    importance,
    character_ids: Array.isArray(a.character_ids) ? a.character_ids.filter((x): x is number => typeof x === 'number') : [],
  }
}

/**
 * 确认表单 → overrides：只发与初值不同的字段（未改动 = 用分析给出的值）；
 * 计划回收章被清空时显式发 null；关联角色在用户动过该字段（charactersTouched）或与初值不同时提交——
 * 含空列表（选了又清空 = 明确不关联）。标题为空返回 error。
 */
export function buildForeshadowOverrides(
  initial: NewForeshadowFormValues,
  v: NewForeshadowFormValues,
  opts: { charactersTouched?: boolean } = {},
): { ok: true; overrides: ForeshadowOverrides } | { ok: false; error: string } {
  const title = (v.title ?? '').trim()
  if (!title) return { ok: false, error: '伏笔标题不能为空' }
  if (title.length > 200) return { ok: false, error: '标题不超过 200 字' }
  const out: ForeshadowOverrides = {}
  if (title !== initial.title.trim()) out.title = title
  for (const key of ['content', 'plant_hint', 'reveal_hint'] as const) {
    const val = (v[key] ?? '').trim()
    if (val !== initial[key].trim()) out[key] = val
  }
  const planned = chapterOrNull(v.planned_resolve_chapter)
  if (planned !== initial.planned_resolve_chapter) out.planned_resolve_chapter = planned
  const importance = Math.min(5, Math.max(1, Math.round(v.importance || 3)))
  if (importance !== initial.importance) out.importance = importance
  const chars = [...new Set(v.character_ids ?? [])]
  const initialChars = [...new Set(initial.character_ids)]
  const charsChanged = chars.length !== initialChars.length || chars.some((id) => !initialChars.includes(id))
  if (opts.charactersTouched || charsChanged) out.character_ids = chars
  return { ok: true, overrides: out }
}

/** 分析详情的 foreshadow_candidates → 规范化结构（丢弃不合法项，兼容没有 ref_id 的旧数据） */
export function readCandidates(raw: unknown): ForeshadowCandidate[] {
  if (!Array.isArray(raw)) return []
  const out: ForeshadowCandidate[] = []
  for (const item of raw) {
    if (!isRecord(item)) continue
    const action = item.action === 'resolve' ? 'resolve' : item.action === 'plant' ? 'plant' : null
    if (!action) continue
    out.push({
      action,
      ref_id: typeof item.ref_id === 'number' && Number.isInteger(item.ref_id) ? item.ref_id : null,
      text: str(item.text),
      keyword: str(item.keyword),
      expected_resolve_chapter: chapterOrNull(item.expected_resolve_chapter),
      partial: item.partial === true,
    })
  }
  return out
}

/** 候选的一行说明：「回收 #5（部分）」「埋下 #3」「新埋设」 */
export function candidateLabel(c: ForeshadowCandidate): string {
  if (c.action === 'resolve') return `回收${c.ref_id !== null ? ` #${c.ref_id}` : ''}${c.partial ? '（部分）' : ''}`
  return c.ref_id !== null ? `埋下 #${c.ref_id}` : '新埋设'
}

// ---------------- 章节提醒 ----------------

export type ReminderKey = 'to_plant' | 'must_resolve' | 'overdue' | 'upcoming' | 'long_term'

export interface ReminderSection {
  key: ReminderKey
  label: string
  items: Array<{ foreshadow: Foreshadow; detail: string }>
}

const REMINDER_LABELS: Record<ReminderKey, string> = {
  to_plant: '本章计划埋设',
  must_resolve: '本章必须回收',
  overdue: '超期未回收',
  upcoming: '即将到期（可适当铺垫）',
  long_term: '长线伏笔',
}

const REMINDER_ORDER: ReminderKey[] = ['to_plant', 'must_resolve', 'overdue', 'upcoming', 'long_term']

/** 提醒徽标数 = 本章计划埋设 + 本章必须回收 */
export function reminderBadgeCount(rem: ForeshadowReminders | null | undefined): number {
  return rem ? rem.to_plant.length + rem.must_resolve.length : 0
}

/** 按上下文【伏笔提醒】的五档顺序展示，只返回非空分档；detail 为该档的附加说明 */
export function reminderSections(rem: ForeshadowReminders | null | undefined): ReminderSection[] {
  if (!rem) return []
  const detailOf = (key: ReminderKey, f: Foreshadow): string => {
    switch (key) {
      case 'to_plant':
        return f.plant_hint ? `埋法：${f.plant_hint}` : ''
      case 'must_resolve':
        return f.reveal_hint ? `回收：${f.reveal_hint}` : ''
      case 'overdue':
        return `已超期 ${f.overdue_by} 章`
      case 'upcoming':
        return f.planned_resolve_chapter !== null ? `第 ${f.planned_resolve_chapter} 章到期` : ''
      default:
        return f.content.length > 40 ? `${f.content.slice(0, 40)}…` : f.content
    }
  }
  return REMINDER_ORDER.map((key) => ({
    key,
    label: REMINDER_LABELS[key],
    items: (rem[key] ?? []).map((f) => ({ foreshadow: f, detail: detailOf(key, f) })),
  })).filter((s) => s.items.length > 0)
}

// ---------------- 时间线 ----------------

export type SegmentKind = 'solid' | 'dashed' | 'overdue'

export interface TimelineSegment {
  from: number
  to: number
  kind: SegmentKind
}

export interface TimelineMarker {
  chapter: number
  /** plant = 埋设点，resolve = 回收点 */
  type: 'plant' | 'resolve'
  /** true = 实际章节（实心），false = 计划章节（空心） */
  actual: boolean
}

export interface TimelineBar {
  id: number
  title: string
  status: ForeshadowStatus
  overdue: number
  /** 长线伏笔：线段延伸到当前章并以箭头结尾 */
  arrow: boolean
  segments: TimelineSegment[]
  markers: TimelineMarker[]
}

/**
 * 每条伏笔的时间线：起点 = 实际埋设章 ?? 计划埋设章；
 * 已回收 / 部分回收 → 起点到实际回收章实线；未收束且有计划回收章 → 到计划回收章虚线，超期再接一段红线到当前章；
 * 未收束且没有计划回收章（长线）→ 虚线延伸到当前章 + 箭头；待埋 / 已废弃有计划回收章 → 虚线。
 */
export function timelineBars(items: Foreshadow[], currentChapter: number): TimelineBar[] {
  return items.map((f) => {
    const segments: TimelineSegment[] = []
    const markers: TimelineMarker[] = []
    const start = f.actual_plant_chapter ?? f.planned_plant_chapter
    const actualEnd = f.status === 'resolved' || f.status === 'partial' ? f.actual_resolve_chapter : null
    const overdue = overdueBy(f, currentChapter)
    let arrow = false
    if (start !== null) markers.push({ chapter: start, type: 'plant', actual: f.actual_plant_chapter !== null })
    if (actualEnd !== null) {
      markers.push({ chapter: actualEnd, type: 'resolve', actual: true })
      if (start !== null && actualEnd !== start) segments.push({ from: start, to: actualEnd, kind: 'solid' })
    }
    const planned = f.planned_resolve_chapter
    if (planned !== null && planned !== actualEnd && f.status !== 'resolved') {
      markers.push({ chapter: planned, type: 'resolve', actual: false })
    }
    const from = actualEnd ?? start
    if (isOpenForeshadow(f)) {
      if (planned !== null) {
        if (from !== null && planned > from) segments.push({ from, to: planned, kind: 'dashed' })
        if (overdue > 0) {
          const s = Math.max(planned, from ?? planned)
          if (currentChapter > s) segments.push({ from: s, to: currentChapter, kind: 'overdue' })
        }
      } else if (from !== null) {
        arrow = true
        if (currentChapter > from) segments.push({ from, to: currentChapter, kind: 'dashed' })
      }
    } else if ((f.status === 'pending' || f.status === 'abandoned') && start !== null && planned !== null && planned > start) {
      segments.push({ from: start, to: planned, kind: 'dashed' })
    }
    return { id: f.id, title: f.title, status: f.status, overdue, arrow, segments, markers }
  })
}

/** 横轴范围：覆盖所有线段 / 标记与当前章，至少 [1, 2] */
export function timelineRange(bars: TimelineBar[], currentChapter: number): { min: number; max: number } {
  const points: number[] = []
  for (const b of bars) {
    for (const s of b.segments) points.push(s.from, s.to)
    for (const m of b.markers) points.push(m.chapter)
  }
  if (currentChapter > 0) points.push(currentChapter)
  if (points.length === 0) return { min: 1, max: 2 }
  const min = Math.max(1, Math.min(...points))
  const max = Math.max(min + 1, ...points)
  return { min, max }
}

/** 章节号 → 横坐标：left + (ch - min) × unit */
export function chapterX(chapter: number, range: { min: number }, unit: number, left = 0): number {
  return left + (chapter - range.min) * unit
}

/** 刻度：章节数 ≤ 40 每章一个，否则取 5 / 10 / 20 / 50 … 的倍数（总数约 ≤ 40），首尾总在 */
export function timelineTicks(range: { min: number; max: number }): number[] {
  const span = range.max - range.min
  let step = 1
  if (span > 40) {
    const steps = [5, 10, 20, 50, 100, 200, 500, 1000]
    step = steps.find((s) => span / s <= 40) ?? Math.ceil(span / 40)
  }
  const ticks = new Set<number>([range.min, range.max])
  const first = Math.ceil(range.min / step) * step
  for (let c = first; c <= range.max; c += step) ticks.add(c)
  return [...ticks].sort((a, b) => a - b)
}
