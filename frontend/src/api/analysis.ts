// 章节分析与回写：分析任务、状态、历史、回写条目操作、按建议重写，以及展示用的纯函数

import { lifeStatusLabel } from './characters'
import { api } from './client'
import { foreshadowStatusLabel } from './foreshadowLabels'
import { memberStatusLabel } from './organizations'
import { RELATION_CATEGORY, RELATION_STATUS } from './relationships'
import type {
  Analysis,
  AnalysisBrief,
  AnalysisChange,
  AnalysisScores,
  AnalysisStatus,
  AnalyzeDone,
  AnalyzePendingResult,
  AnalyzeRequest,
  ChangeKind,
  ChangeStatus,
  ChapterAnalysisState,
  ForeshadowOverrides,
  RelationCategory,
  RelationStatus,
  ReviseChapterRequest,
  ScoreKey,
  SuggestionPriority,
  Task,
} from './types'

export const analysisApi = {
  /** 202 新任务；同章已有排队中的分析时 200 返回那条任务 */
  analyze: (cid: number, body: AnalyzeRequest = {}) => api.post<Task>(`/chapters/${cid}/analyze`, body),
  analyzePending: (pid: number, body: AnalyzeRequest = {}) =>
    api.post<AnalyzePendingResult>(`/projects/${pid}/analysis/analyze-pending`, body),
  statuses: (pid: number) => api.get<ChapterAnalysisState[]>(`/projects/${pid}/analysis/statuses`),
  status: (cid: number) => api.get<ChapterAnalysisState>(`/chapters/${cid}/analysis-status`),
  /** 新在前 */
  list: (cid: number) => api.get<AnalysisBrief[]>(`/chapters/${cid}/analyses`),
  get: (aid: number) => api.get<Analysis>(`/analyses/${aid}`),
  revert: (id: number) => api.post<AnalysisChange>(`/analysis-changes/${id}/revert`),
  /** overrides 只对 foreshadow_new 生效（确认时修改字段），其他类型不传 */
  confirm: (id: number, overrides?: ForeshadowOverrides) =>
    api.post<AnalysisChange>(`/analysis-changes/${id}/confirm`, overrides ? { overrides } : undefined),
  reject: (id: number) => api.post<AnalysisChange>(`/analysis-changes/${id}/reject`),
}

/** 按建议整章重写：创建后台任务（202），章节立即进入 generating；与生成 / 润色互斥 */
export function reviseChapter(cid: number, body: ReviseChapterRequest): Promise<Task> {
  return api.post<Task>(`/chapters/${cid}/revise`, body)
}

// ---------------- 标签 ----------------

export const ANALYSIS_STATUS: Record<AnalysisStatus, { label: string; color: string }> = {
  none: { label: '未分析', color: 'default' },
  analyzing: { label: '分析中', color: 'processing' },
  analyzed: { label: '已分析', color: 'success' },
  stale: { label: '已过期', color: 'warning' },
}

export const SCORE_KEYS: ScoreKey[] = ['overall', 'pacing', 'engagement', 'coherence']

export const SCORE_LABELS: Record<ScoreKey, string> = {
  overall: '整体',
  pacing: '节奏',
  engagement: '吸引力',
  coherence: '连贯性',
}

/** 评分标准（与后端提示词分档一致），用于界面说明 */
export const SCORE_RUBRIC = '1–3 差 · 4–5 及格 · 6–7 良好 · 8–9 优秀 · 10 完美'

export const SUGGESTION_PRIORITY: Record<SuggestionPriority, { label: string; color: string }> = {
  high: { label: '高', color: 'red' },
  medium: { label: '中', color: 'orange' },
  low: { label: '低', color: 'default' },
}

export function priorityLabel(p: string): { label: string; color: string } {
  return p in SUGGESTION_PRIORITY ? SUGGESTION_PRIORITY[p as SuggestionPriority] : { label: p || '-', color: 'default' }
}

export const CHANGE_KINDS: ChangeKind[] = [
  'character_state',
  'life_status',
  'relationship_update',
  'relationship_create',
  'career_stage',
  'org_membership',
  'foreshadow_resolve',
  'foreshadow_plant_planned',
  'foreshadow_new',
]

export const CHANGE_KIND_LABELS: Record<ChangeKind, string> = {
  character_state: '角色状态',
  life_status: '存活状态',
  relationship_update: '关系变化',
  relationship_create: '新建关系',
  career_stage: '境界',
  org_membership: '组织成员',
  foreshadow_resolve: '伏笔回收',
  foreshadow_plant_planned: '伏笔埋设',
  foreshadow_new: '新伏笔',
}

export const CHANGE_STATUS: Record<ChangeStatus, { label: string; color: string }> = {
  applied: { label: '已写入', color: 'success' },
  pending: { label: '待确认', color: 'gold' },
  reverted: { label: '已撤销', color: 'default' },
  rejected: { label: '已拒绝', color: 'default' },
  skipped: { label: '已跳过', color: 'default' },
  failed: { label: '失败', color: 'error' },
}

export const REVISE_PRESERVE_PRESETS = ['保持整体结构', '保持人物性格', '保留关键对白'] as const

export const MAX_REVISE_SUGGESTIONS = 20
export const MAX_REVISE_PRESERVE = 10
export const MAX_REVISE_INSTRUCTION = 2000

// ---------------- 评分 ----------------

function scoreOf(scores: AnalysisScores | null | undefined, key: ScoreKey): number | null {
  const v = scores?.[key]
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/** 展示评分：一位小数，缺失为「-」 */
export function formatScore(scores: AnalysisScores | null | undefined, key: ScoreKey = 'overall'): string {
  const v = scoreOf(scores, key)
  return v === null ? '-' : v.toFixed(1)
}

/** 当前 − 对比，一位小数；任一方缺失为 null */
export function scoreDelta(
  current: AnalysisScores | null | undefined,
  previous: AnalysisScores | null | undefined,
): Record<ScoreKey, number | null> {
  const out = {} as Record<ScoreKey, number | null>
  for (const key of SCORE_KEYS) {
    const a = scoreOf(current, key)
    const b = scoreOf(previous, key)
    out[key] = a === null || b === null ? null : Math.round((a - b) * 10) / 10
  }
  return out
}

/** 差值文案：↑0.5 / ↓1.2 / 持平；null 为空串 */
export function formatDelta(delta: number | null): string {
  if (delta === null) return ''
  if (delta > 0) return `↑${delta.toFixed(1)}`
  if (delta < 0) return `↓${Math.abs(delta).toFixed(1)}`
  return '持平'
}

/** 差值颜色：升绿、降红、持平灰 */
export function deltaColor(delta: number | null): string | undefined {
  if (delta === null || delta === 0) return undefined
  return delta > 0 ? '#389e0d' : '#cf1322'
}

/** 对白占比 0–1 → 「35%」 */
export function formatRatio(ratio: number | undefined): string {
  return typeof ratio === 'number' && Number.isFinite(ratio) ? `${Math.round(ratio * 100)}%` : '-'
}

/** 评分走势的折线点：values 按时间正序，缺失值跳过（断开）；返回每段的 SVG points 字符串 */
export function trendSegments(
  values: Array<number | null>,
  width: number,
  height: number,
  pad = 6,
  min = 1,
  max = 10,
): string[] {
  const n = values.length
  const segments: string[] = []
  let current: string[] = []
  values.forEach((v, i) => {
    if (v === null) {
      if (current.length) segments.push(current.join(' '))
      current = []
      return
    }
    const x = n <= 1 ? width / 2 : pad + ((width - pad * 2) * i) / (n - 1)
    const clamped = Math.min(max, Math.max(min, v))
    const y = pad + ((height - pad * 2) * (max - clamped)) / (max - min)
    current.push(`${round1(x)},${round1(y)}`)
  })
  if (current.length) segments.push(current.join(' '))
  return segments
}

function round1(n: number): number {
  return Math.round(n * 10) / 10
}

// ---------------- 回写条目 ----------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function str(value: unknown): string {
  return typeof value === 'string' ? value : typeof value === 'number' ? String(value) : ''
}

const EMPTY = '（空）'
const NONE = '（无）'

function textValue(value: unknown): string {
  if (value === null || value === undefined) return NONE
  const s = str(value)
  return s.trim() ? s : EMPTY
}

function relationStatusText(value: unknown): string {
  const s = str(value)
  return s in RELATION_STATUS ? RELATION_STATUS[s as RelationStatus].label : s || NONE
}

function membershipText(value: unknown): string {
  if (!isRecord(value)) return '非成员'
  const position = str(value.position).trim() || '无职位'
  const status = str(value.status)
  return status ? `${position}（${memberStatusLabel(status).label}）` : position
}

const ORG_ACTION_LABELS: Record<string, string> = { join: '加入', leave: '离开', update: '变更' }

const REL_FIELD_LABELS: Record<string, string> = { intimacy: '亲密度', status: '关系状态', description: '描述' }

function relationFieldValue(field: string, value: unknown): string {
  if (field === 'status') return relationStatusText(value)
  if (field === 'intimacy') return value === null || value === undefined ? NONE : str(value)
  return textValue(value)
}

/** 新建关系 / 未匹配关系的 after：{role_a, role_b, category, intimacy, status, description} 的任意子集 */
function relationSummary(value: unknown): string {
  if (!isRecord(value)) return textValue(value)
  const parts: string[] = []
  const roleA = str(value.role_a).trim()
  const roleB = str(value.role_b).trim()
  if (roleA || roleB) parts.push(roleA && roleB && roleA !== roleB ? `${roleA} / ${roleB}` : roleA || roleB)
  const category = str(value.category)
  if (category in RELATION_CATEGORY) parts.push(RELATION_CATEGORY[category as RelationCategory].label)
  if (value.intimacy !== undefined && value.intimacy !== null) parts.push(`亲密度 ${str(value.intimacy)}`)
  if (value.status !== undefined && value.status !== null) parts.push(relationStatusText(value.status))
  const desc = str(value.description).trim()
  if (desc) parts.push(desc)
  return parts.length ? parts.join('，') : NONE
}

/** 伏笔状态快照 {status, actual_resolve_chapter | actual_plant_chapter} → 「已埋 · 第 3 章」 */
function foreshadowStateText(value: unknown, chapterKey: 'actual_resolve_chapter' | 'actual_plant_chapter'): string {
  if (!isRecord(value)) return NONE
  const status = str(value.status)
  const label = status ? foreshadowStatusLabel(status).label : ''
  const ch = value[chapterKey]
  const chText = typeof ch === 'number' ? `第 ${ch} 章` : ''
  return [label, chText].filter(Boolean).join(' · ') || NONE
}

/** 新伏笔的 after：{title, planned_resolve_chapter, importance, ...} → 「计划第 8 章回收 · 重要度 3」 */
function newForeshadowText(value: unknown): string {
  if (!isRecord(value)) return NONE
  const parts: string[] = []
  const content = str(value.content).trim()
  if (content) parts.push(content.length > 60 ? `${content.slice(0, 60)}…` : content)
  const planned = value.planned_resolve_chapter
  parts.push(typeof planned === 'number' ? `计划第 ${planned} 章回收` : '长线（未定回收章）')
  if (typeof value.importance === 'number') parts.push(`重要度 ${value.importance}`)
  return parts.join(' · ')
}

/** 伏笔条目的对象名：「#5 玉佩来历」；未匹配条目回退到原始关键词 / 文本 */
function foreshadowSubject(t: Record<string, unknown>): string {
  const id = typeof t.foreshadow_id === 'number' ? `#${t.foreshadow_id}` : ''
  const title = str(t.title) || str(t.keyword) || (str(t.text).length > 20 ? `${str(t.text).slice(0, 20)}…` : str(t.text))
  return [id, title].filter(Boolean).join(' ') || '?'
}

export interface ChangeView {
  /** 对象：角色名 / 「A ↔ B」/「角色 · 职业」/「角色 · 组织」 */
  subject: string
  /** 字段说明，如「当前状态」「亲密度」 */
  field: string
  before: string
  after: string
}

/** 回写条目的「前 → 后」展示；兼容未匹配条目（target 只有原始名字、before 为空） */
export function describeChange(change: Pick<AnalysisChange, 'kind' | 'target' | 'before' | 'after'>): ChangeView {
  const t = change.target ?? {}
  const name = str(t.name) || '?'
  switch (change.kind) {
    case 'character_state':
      return { subject: name, field: '当前状态', before: textValue(change.before), after: textValue(change.after) }
    case 'life_status':
      return {
        subject: name,
        field: '存活状态',
        before: change.before === null || change.before === undefined ? NONE : lifeStatusLabel(str(change.before)).label,
        after: lifeStatusLabel(str(change.after)).label,
      }
    case 'relationship_update':
    case 'relationship_create': {
      const subject = `${str(t.a_name) || '?'} ↔ ${str(t.b_name) || '?'}`
      const field = str(t.field)
      if (change.kind === 'relationship_update' && field) {
        return {
          subject,
          field: REL_FIELD_LABELS[field] ?? field,
          before: relationFieldValue(field, change.before),
          after: relationFieldValue(field, change.after),
        }
      }
      return {
        subject,
        field: change.kind === 'relationship_create' ? '新建关系' : '关系',
        before: change.before === null || change.before === undefined ? NONE : relationSummary(change.before),
        after: relationSummary(change.after),
      }
    }
    case 'career_stage': {
      const career = str(t.career_name) || '?'
      const stage = (v: unknown) => (isRecord(v) ? str(v.stage) || NONE : NONE)
      return { subject: `${name} · ${career}`, field: '境界', before: stage(change.before), after: stage(change.after) }
    }
    case 'org_membership': {
      const org = str(t.organization_name) || '?'
      const after = change.after
      // 未匹配条目的 after 是模型原始动作 {action, position, status}
      if (isRecord(after) && typeof after.action === 'string') {
        const action = ORG_ACTION_LABELS[after.action] ?? after.action
        return {
          subject: `${name} · ${org}`,
          field: '组织成员',
          before: NONE,
          after: `${action}${str(after.position) ? `：${str(after.position)}` : ''}`,
        }
      }
      return {
        subject: `${name} · ${org}`,
        field: '组织成员',
        before: membershipText(change.before),
        after: membershipText(after),
      }
    }
    case 'foreshadow_resolve':
      return {
        subject: foreshadowSubject(t),
        field: '回收',
        before: foreshadowStateText(change.before, 'actual_resolve_chapter'),
        after: foreshadowStateText(change.after, 'actual_resolve_chapter'),
      }
    case 'foreshadow_plant_planned':
      return {
        subject: foreshadowSubject(t),
        field: '埋设',
        before: foreshadowStateText(change.before, 'actual_plant_chapter'),
        after: foreshadowStateText(change.after, 'actual_plant_chapter'),
      }
    case 'foreshadow_new':
      return { subject: foreshadowSubject(t), field: '新伏笔', before: NONE, after: newForeshadowText(change.after) }
    default:
      return { subject: name, field: '', before: textValue(change.before), after: textValue(change.after) }
  }
}

export interface ChangeGroup {
  kind: ChangeKind
  label: string
  items: AnalysisChange[]
}

/** 按类型分组（固定顺序），只返回非空分组；组内保持原顺序 */
export function groupChanges(changes: AnalysisChange[]): ChangeGroup[] {
  return CHANGE_KINDS.map((kind) => ({
    kind,
    label: CHANGE_KIND_LABELS[kind],
    items: changes.filter((c) => c.kind === kind),
  })).filter((g) => g.items.length > 0)
}

export function countPending(changes: Pick<AnalysisChange, 'status'>[] | null | undefined): number {
  return (changes ?? []).filter((c) => c.status === 'pending').length
}

/** 用服务端返回的条目替换列表中的同 id 条目 */
export function replaceChange(changes: AnalysisChange[], updated: AnalysisChange): AnalysisChange[] {
  return changes.map((c) => (c.id === updated.id ? updated : c))
}

// ---------------- 按建议重写 ----------------

export interface ReviseFormValues {
  analysisId: number
  suggestionIds: number[]
  instruction: string
  preserve: string[]
  /** null = 原文字数 */
  targetWords: number | null
  aiConfigId: number | null
}

export type ReviseBuild = { ok: true; body: ReviseChapterRequest } | { ok: false; error: string }

/** 构造并校验重写请求（与后端 ReviseChapterRequest 的约束一致）；去重、去空白 */
export function buildReviseRequest(v: ReviseFormValues): ReviseBuild {
  const suggestionIds = [...new Set(v.suggestionIds)]
  const instruction = v.instruction.trim()
  const preserve = [...new Set(v.preserve.map((p) => p.trim()).filter(Boolean))]
  if (suggestionIds.length === 0 && !instruction) return { ok: false, error: '请至少选择一条建议或填写修改要求' }
  if (suggestionIds.length > MAX_REVISE_SUGGESTIONS)
    return { ok: false, error: `最多选择 ${MAX_REVISE_SUGGESTIONS} 条建议` }
  if (instruction.length > MAX_REVISE_INSTRUCTION)
    return { ok: false, error: `修改要求不超过 ${MAX_REVISE_INSTRUCTION} 字` }
  if (preserve.length > MAX_REVISE_PRESERVE) return { ok: false, error: `保留要素最多 ${MAX_REVISE_PRESERVE} 项` }
  if (v.targetWords !== null && (!Number.isInteger(v.targetWords) || v.targetWords < 300 || v.targetWords > 20000))
    return { ok: false, error: '目标字数应在 300–20000 之间' }
  const body: ReviseChapterRequest = {
    analysis_id: v.analysisId,
    suggestion_ids: suggestionIds,
    instruction,
    preserve,
    ai_config_id: v.aiConfigId,
  }
  if (v.targetWords !== null) body.target_words = v.targetWords
  return { ok: true, body }
}

/** 按建议重写成功后的评分对比：base = 重写引用的分析；since = 重写成功时已知的最新分析 id（新分析须晚于它） */
export interface ReviseCompare {
  baseAnalysisId: number
  sinceAnalysisId: number | null
}

/**
 * 由成功的 chapter.revise 任务得出评分对比基准。base 取 TaskOut.analysis_id（任务面板重试、其他标签页发起的重写同样可用）；
 * since 取「重写成功时已知的最新分析」与 base 中较大者——自动重新分析在任务成功后才入队，因此它一定晚于此时已知的分析。
 * 非 revise 任务或缺少 analysis_id 返回 null。
 */
export function reviseCompareFor(task: Pick<Task, 'kind' | 'analysis_id'>, latestKnownId: number | null): ReviseCompare | null {
  if (task.kind !== 'chapter.revise' || typeof task.analysis_id !== 'number') return null
  const base = task.analysis_id
  return { baseAnalysisId: base, sinceAnalysisId: latestKnownId === null ? base : Math.max(base, latestKnownId) }
}

/** 校验 chapter.analyze 的 done.result；结构不符返回 null */
export function parseAnalyzeDone(result: unknown): AnalyzeDone | null {
  if (!isRecord(result) || typeof result.analysis_id !== 'number' || typeof result.chapter_id !== 'number') return null
  return {
    analysis_id: result.analysis_id,
    chapter_id: result.chapter_id,
    chapter_no: typeof result.chapter_no === 'number' ? result.chapter_no : 0,
    summary: str(result.summary),
    scores: isRecord(result.scores) ? (result.scores as AnalysisScores) : {},
    change_counts: isRecord(result.change_counts) ? (result.change_counts as AnalyzeDone['change_counts']) : {},
    warnings: Array.isArray(result.warnings) ? result.warnings.filter((w): w is string => typeof w === 'string') : [],
  }
}

/** 「已写入 3 · 待确认 1」；没有条目为空串 */
export function changeCountsLabel(counts: Partial<Record<ChangeStatus, number>>): string {
  return (Object.keys(CHANGE_STATUS) as ChangeStatus[])
    .filter((s) => (counts[s] ?? 0) > 0)
    .map((s) => `${CHANGE_STATUS[s].label} ${counts[s]}`)
    .join(' · ')
}
