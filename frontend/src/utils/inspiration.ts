// 灵感模式纯函数：步骤顺序与状态推导、stale 文案、规模建议（与后端 suggest_scale 一致，仅用于即时显示）、应用对比的默认勾选

import type {
  InspirationApplyField,
  InspirationApplyPreviewItem,
  InspirationCurrent,
  InspirationScale,
  InspirationSessionBrief,
  InspirationSessionStatus,
  InspirationState,
  InspirationStepKey,
  InspirationStepSource,
  InspirationStepState,
} from '../api/types'

/** AI 生成选项的步骤（顺序即 stale 传播顺序，与后端 STEPS 一致） */
export const INSPIRATION_STEPS: readonly InspirationStepKey[] = [
  'title',
  'synopsis',
  'theme',
  'tags',
  'selling_points',
  'audience',
]

/** 完整流程：想法 → 6 个 AI 步骤 → 规模 → 确认 */
export const INSPIRATION_FLOW: readonly InspirationCurrent[] = ['idea', ...INSPIRATION_STEPS, 'scale', 'confirm']

export const FLOW_LABELS: Record<InspirationCurrent, string> = {
  idea: '想法',
  title: '书名',
  synopsis: '简介',
  theme: '主题',
  tags: '类型标签',
  selling_points: '核心卖点',
  audience: '目标读者',
  scale: '规模',
  confirm: '确认',
}

export const APPLY_FIELD_LABELS: Record<InspirationApplyField, string> = {
  title: '书名',
  synopsis: '简介',
  theme: '主题',
  tags: '类型标签',
  selling_points: '核心卖点',
  audience: '目标读者',
  premise: '一句话设定',
  target_words: '目标总字数',
}

export const SESSION_STATUS: Record<InspirationSessionStatus, { label: string; color: string }> = {
  active: { label: '进行中', color: 'processing' },
  converted: { label: '已转为项目', color: 'success' },
}

export const STEP_SOURCE_LABELS: Record<InspirationStepSource, string> = {
  ai: 'AI 选项',
  edited: '编辑后选定',
  manual: '自己填写',
  project: '来自项目',
}

/** 每步生成选项的数量与长度要求（提示用，与后端 OPTION_COUNTS / 模板约束一致） */
export const STEP_HINTS: Record<InspirationStepKey, string> = {
  title: '每个书名不超过 30 字',
  synopsis: '每条简介 150–400 字',
  theme: '每个主题不超过 60 字',
  tags: '给出 6–10 个候选标签，可多选（最多 8 个），也可以自定义',
  selling_points: '每条卖点不超过 200 字',
  audience: '每条不超过 60 字',
}

/** 定下的值的长度上限（与后端 VALUE_LIMITS 一致） */
export const STEP_VALUE_LIMITS: Record<Exclude<InspirationStepKey, 'tags'>, number> = {
  title: 200,
  synopsis: 2000,
  theme: 200,
  selling_points: 500,
  audience: 100,
}

export const IDEA_MAX = 5000
export const NOTE_MAX = 200
export const MAX_NOTES = 20
export const FEEDBACK_MAX = 500

export function isStepKey(key: string): key is InspirationStepKey {
  return (INSPIRATION_STEPS as readonly string[]).includes(key)
}

export function isFlowKey(key: string): key is InspirationCurrent {
  return (INSPIRATION_FLOW as readonly string[]).includes(key)
}

/** 步骤条上显示的状态 */
export type StepDisplayStatus = 'decided' | 'skipped' | 'stale' | 'pending'

export const STEP_DISPLAY: Record<StepDisplayStatus, { label: string; color: string }> = {
  decided: { label: '已定', color: 'success' },
  skipped: { label: '已跳过', color: 'default' },
  stale: { label: '可能需要更新', color: 'warning' },
  pending: { label: '待定', color: 'processing' },
}

function emptyStep(): InspirationStepState {
  return { value: null, status: 'pending', stale: false, source: null, rounds: [] }
}

/** 取某个 AI 步骤的状态（缺失时视为待定） */
export function stepOf(state: InspirationState, step: InspirationStepKey): InspirationStepState {
  return state.steps[step] ?? emptyStep()
}

/** 推导步骤条状态：想法有内容 = 已定；规模已保存 = 已定；确认步骤始终待定；AI 步骤 decided + stale = 可能需要更新 */
export function stepDisplayStatus(state: InspirationState, key: InspirationCurrent): StepDisplayStatus {
  if (key === 'idea') return state.idea.trim() ? 'decided' : 'pending'
  if (key === 'scale') return state.scale ? 'decided' : 'pending'
  if (key === 'confirm') return 'pending'
  const s = stepOf(state, key)
  if (s.status === 'decided') return s.stale ? 'stale' : 'decided'
  if (s.status === 'skipped') return 'skipped'
  return 'pending'
}

/** stale 提示文案：说明为什么可能需要更新、可以怎么处理 */
export function staleText(step: InspirationStepKey): string {
  return `前面的想法或步骤有改动，「${FLOW_LABELS[step]}」的已定内容可能需要更新。可以保持不变、换一批选项，或者自己修改。`
}

/** 有 stale 的步骤数量提示；没有时为空串 */
export function staleSummary(state: InspirationState): string {
  const stale = INSPIRATION_STEPS.filter((k) => stepDisplayStatus(state, k) === 'stale')
  if (stale.length === 0) return ''
  return `${stale.map((k) => `「${FLOW_LABELS[k]}」`).join('')}可能需要更新`
}

/** 已定或已跳过的 AI 步骤数（与后端 SessionBrief.done_steps 口径一致） */
export function handledCount(state: InspirationState): number {
  return INSPIRATION_STEPS.filter((k) => stepOf(state, k).status !== 'pending').length
}

/** 列表页进度：0–100 */
export function briefProgress(brief: Pick<InspirationSessionBrief, 'done_steps' | 'total_steps'>): number {
  if (brief.total_steps <= 0) return 0
  return Math.round((brief.done_steps / brief.total_steps) * 100)
}

/** 进入 AI 步骤时是否主动建议生成选项：只对还没定（待定）且没生成过的步骤；已定 / 跳过的步骤只显示值，「换一批」仍可手动点。 */
export function shouldSuggestGenerate(state: InspirationState, step: InspirationStepKey): boolean {
  const s = stepOf(state, step)
  return s.status === 'pending' && s.rounds.length === 0
}

/**
 * 决定 / 跳过某步后跳到哪里：之后第一个待定或可能需要更新的 AI 步骤；都处理过则进入规模（规模、确认总会停下）。
 * 当前是确认步骤时不再前进。
 */
export function nextFlowStep(state: InspirationState, from: InspirationCurrent): InspirationCurrent {
  const idx = INSPIRATION_FLOW.indexOf(from)
  for (const key of INSPIRATION_FLOW.slice(idx + 1)) {
    if (!isStepKey(key)) return key
    const st = stepDisplayStatus(state, key)
    if (st === 'pending' || st === 'stale') return key
  }
  return 'confirm'
}

/** 定下类型标签时的来源：全部来自 AI 候选 = ai；全部是自定义 = manual；混合 = edited */
export function tagsSource(tags: readonly string[], rounds: readonly { options: readonly string[] }[]): 'ai' | 'edited' | 'manual' {
  const offered = new Set(rounds.flatMap((r) => r.options))
  const fromAi = tags.filter((t) => offered.has(t)).length
  if (fromAi === tags.length) return 'ai'
  if (fromAi === 0) return 'manual'
  return 'edited'
}

/** 值的展示文本（标签用顿号连接） */
export function valueText(value: string | string[] | number | null | undefined): string {
  if (value === null || value === undefined) return ''
  if (Array.isArray(value)) return value.join('、')
  return String(value)
}

/** Python round()（银行家舍入）：.5 取偶，保证与后端 suggest_scale 一致 */
export function roundHalfEven(x: number): number {
  const floor = Math.floor(x)
  const diff = x - floor
  if (Math.abs(diff - 0.5) < 1e-9) return floor % 2 === 0 ? floor : floor + 1
  return Math.round(x)
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v))

/** 规模建议：每章约 3000 字（至少 10 章）；每卷约 30 章，卷数 1–20、每卷章数 5–100；角色数 = 5 + 卷数 × 2（≤ 30） */
export function suggestScale(targetWords: number): InspirationScale {
  const chapters = Math.max(10, roundHalfEven(targetWords / 3000))
  const volumes = clamp(Math.ceil(chapters / 30), 1, 20)
  const perVolume = clamp(Math.ceil(chapters / volumes), 5, 100)
  return {
    target_words: targetWords,
    volume_count: volumes,
    chapters_per_volume: perVolume,
    character_count: Math.min(30, 5 + volumes * 2),
  }
}

/** 规模表单的值（规模一步与确认一步共用） */
export interface ScaleFormValues {
  target_words: number | null
  volume_count: number
  chapters_per_volume: number
  character_count: number
}

/** 规模表单初始值：会话已保存的规模；没有时按 30 万字的建议 */
export function initialScaleValues(scale: InspirationScale | null): ScaleFormValues {
  return scale ? { ...scale } : { ...suggestScale(300000) }
}

/** 表单值 → 请求体的规模 */
export function toScale(v: ScaleFormValues): InspirationScale {
  return {
    target_words: v.target_words ?? null,
    volume_count: v.volume_count,
    chapters_per_volume: v.chapters_per_volume,
    character_count: v.character_count,
  }
}

/** 规模摘要：「4 卷 × 25 章，共 100 章；13 个角色」 */
export function scaleText(scale: Pick<InspirationScale, 'volume_count' | 'chapters_per_volume' | 'character_count'>): string {
  return `${scale.volume_count} 卷 × ${scale.chapters_per_volume} 章，共 ${scale.volume_count * scale.chapters_per_volume} 章；${scale.character_count} 个角色`
}

function isEmptyValue(v: unknown): boolean {
  if (v === null || v === undefined) return true
  if (typeof v === 'string') return v.trim() === ''
  if (Array.isArray(v)) return v.length === 0
  return false
}

function sameValue(a: unknown, b: unknown): boolean {
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => x === b[i])
  return a === b
}

/** 应用对比中某个字段的值是否与项目当前值相同（相同的字段勾不勾都不改变项目） */
export function applyUnchanged(item: Pick<InspirationApplyPreviewItem, 'current' | 'proposed'>): boolean {
  return sameValue(item.current, item.proposed)
}

/** 默认勾选（与后端 default_checked 同口径）：项目该字段为空且建议值不同 */
export function defaultApplyChecked(item: Pick<InspirationApplyPreviewItem, 'current' | 'proposed'>): boolean {
  return isEmptyValue(item.current) && !sameValue(item.current, item.proposed)
}

/** 打开对比时的默认勾选字段：以后端 default_checked 为准，同时兜底「当前为空」的口径 */
export function defaultApplyFields(items: readonly InspirationApplyPreviewItem[]): InspirationApplyField[] {
  return items.filter((it) => it.default_checked && defaultApplyChecked(it)).map((it) => it.field)
}

/** 对比表里的值展示：空值显示「（空）」，目标字数加单位 */
export function applyValueText(field: InspirationApplyField, value: InspirationApplyPreviewItem['current']): string {
  if (isEmptyValue(value)) return '（空）'
  if (field === 'target_words' && typeof value === 'number') return `${value.toLocaleString('zh-CN')} 字`
  return valueText(value)
}

/** 立项草稿汇总：每个 AI 步骤的显示状态与值 */
export function draftSummary(state: InspirationState): { key: InspirationStepKey; label: string; status: StepDisplayStatus; text: string }[] {
  return INSPIRATION_STEPS.map((key) => {
    const s = stepOf(state, key)
    return {
      key,
      label: FLOW_LABELS[key],
      status: stepDisplayStatus(state, key),
      text: s.status === 'decided' ? valueText(s.value) : '',
    }
  })
}

/** 已定书名（转为项目需要）；没有时为空串 */
export function decidedTitle(state: InspirationState): string {
  const s = stepOf(state, 'title')
  return s.status === 'decided' && typeof s.value === 'string' ? s.value : ''
}
