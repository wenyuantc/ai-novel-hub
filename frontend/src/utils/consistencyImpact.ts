// 「修改影响」页签的纯函数：页签与章节深链、列表行文案、派生内容跳转、评估按钮的禁用原因、
// 确认状态的本地勾选与冲突合并、只读显隐、快捷检查范围、任务结束提示。

import { ANALYSIS_PANEL_QUERY } from '../api/tasks'
import type {
  AssessBlock, ImpactChange, ImpactChangeKind, ImpactChapterRef, ImpactChapterSummary, ImpactDetail, ImpactItem,
  ImpactItemStatus, ImpactRunning, ImpactUpdate, LedgerState, StaleProgression,
} from '../api/consistency'
import type { TaskStatus } from '../api/types'
import type { ConsistencyAccess, TaskEndNotice } from './consistency'
import { entityDetailHref } from './entityLinks'
import { readPositiveId } from './mentions'

// ---------------- 页签与深链 ----------------

export type ConsistencyTab = 'issues' | 'impact'
export const TAB_PARAM = 'tab'
export const IMPACT_CHAPTER_PARAM = 'chapter'
/** 任务面板跳转带来的评估任务 id：页面据此找到对应章节后即从地址栏去掉 */
export const IMPACT_TASK_PARAM = 'task'

export function readConsistencyTab(params: URLSearchParams): ConsistencyTab {
  return params.get(TAB_PARAM) === 'impact' ? 'impact' : 'issues'
}

/** 所选章节只在「修改影响」页签下有效；伪整数、非正数一律视为未选。 */
export function readImpactChapter(params: URLSearchParams): number | null {
  return readConsistencyTab(params) === 'impact' ? readPositiveId(params.get(IMPACT_CHAPTER_PARAM)) : null
}

/** 任务面板跳转带来的评估任务 id；只在「修改影响」页签下有效。 */
export function readImpactTask(params: URLSearchParams): number | null {
  return readConsistencyTab(params) === 'impact' ? readPositiveId(params.get(IMPACT_TASK_PARAM)) : null
}

/**
 * 任务 id → 它评估的章节：进行中的评估取接口报告的章节，已结束的取概览里「最近一次评估的任务」。
 * undefined = 数据尚未读到，稍后再判断；null = 找不到（评估未落库即失败、已被新的评估覆盖或任务已清理），停在列表。
 */
export function impactTaskChapter(
  taskId: number,
  running: Pick<ImpactRunning, 'running_task_id' | 'running_chapter_id'> | undefined,
  chapters: readonly Pick<ImpactChapterSummary, 'chapter_id' | 'task_id'>[] | undefined,
): number | null | undefined {
  if (running?.running_task_id === taskId && running.running_chapter_id !== null) return running.running_chapter_id
  if (chapters === undefined) return undefined
  return chapters.find((row) => row.task_id === taskId)?.chapter_id ?? null
}

/** 一致性页「修改影响」页签的地址；给出章节时同时打开该章详情。 */
export function impactHref(projectId: number, chapterId?: number | null): string {
  const chapter = chapterId != null && Number.isSafeInteger(chapterId) && chapterId > 0 ? `&${IMPACT_CHAPTER_PARAM}=${chapterId}` : ''
  return `/projects/${projectId}/consistency?${TAB_PARAM}=impact${chapter}`
}

/** 切换页签 / 所选章节后的查询串：其余参数保留；回到问题页签时一并清掉所选章节。 */
export function withImpactLocation(params: URLSearchParams, tab: ConsistencyTab, chapterId: number | null = null): URLSearchParams {
  const next = new URLSearchParams(params)
  next.delete(IMPACT_CHAPTER_PARAM)
  next.delete(IMPACT_TASK_PARAM)
  if (tab === 'issues') next.delete(TAB_PARAM)
  else {
    next.set(TAB_PARAM, 'impact')
    if (chapterId !== null) next.set(IMPACT_CHAPTER_PARAM, String(chapterId))
  }
  return next
}

// ---------------- 列表行 ----------------

export const LEDGER_STATE: Record<LedgerState, { label: string; color: string }> = {
  fresh: { label: '事实台账最新', color: 'success' },
  stale: { label: '事实台账已过期', color: 'warning' },
  missing: { label: '尚未抽取事实', color: 'default' },
}

export const IMPACT_ITEM_STATUS: Record<ImpactItemStatus, { label: string; color: string }> = {
  pending: { label: '待复核', color: 'processing' },
  reviewed: { label: '已复核', color: 'success' },
  dismissed: { label: '无需处理', color: 'default' },
}

export const IMPACT_CHANGE_KIND: Record<ImpactChangeKind, { label: string; color: string }> = {
  added: { label: '新增', color: 'green' },
  removed: { label: '消失', color: 'red' },
  changed: { label: '改变', color: 'gold' },
}

type Conclusion = Pick<ImpactChapterSummary, 'assessed' | 'no_change' | 'changes'>

/** 最近评估结论：未评估／未发现剧情事实变化／N 处事实变化。 */
export function impactConclusion(row: Conclusion): { label: string; color: string } {
  if (!row.assessed) return { label: '未评估', color: 'default' }
  if (row.no_change || row.changes <= 0) return { label: '未发现剧情事实变化', color: 'success' }
  return { label: `${row.changes} 处事实变化`, color: 'volcano' }
}

/** 第一级的过期计数标签（只列出大于 0 的类别）。 */
export function staleLabels(row: Pick<ImpactChapterSummary, 'stale_summary' | 'stale_memories' | 'stale_progressions'>): string[] {
  const labels: string[] = []
  if (row.stale_summary) labels.push('章摘要已过期')
  if (row.stale_memories > 0) labels.push(`剧情记忆 ${row.stale_memories} 条已过期`)
  if (row.stale_progressions > 0) labels.push(`设定变化 ${row.stale_progressions} 条待确认`)
  return labels
}

/** 评估结果上需要提醒的附加说明。未评估时没有。 */
export function impactRowNotes(row: Pick<ImpactChapterSummary, 'assessed' | 'outdated' | 'items' | 'pending_items' | 'changed_items' | 'uncovered'>): string[] {
  if (!row.assessed) return []
  const notes: string[] = []
  if (row.outdated) notes.push('评估后正文又有修改')
  if (row.items > 0) notes.push(row.pending_items > 0 ? `可能受影响 ${row.items} 章，待复核 ${row.pending_items} 项` : `可能受影响 ${row.items} 章，均已处理`)
  if (row.changed_items > 0) notes.push(`${row.changed_items} 个受影响章节在评估后已修改`)
  if (row.uncovered > 0) notes.push(`${row.uncovered} 章无法判断`)
  return notes
}

export function chapterLabel(chapter: { chapter_no: number | null; chapter_title?: string | null }): string {
  if (chapter.chapter_no === null) return '章节已删除'
  return `第 ${chapter.chapter_no} 章${chapter.chapter_title ? ` · ${chapter.chapter_title}` : ''}`
}

/** 事实变化的前后对照：新增没有「改动前」，消失没有「改动后」。 */
export function changeSides(change: Pick<ImpactChange, 'kind' | 'before' | 'after'>): { before: string | null; after: string | null } {
  return {
    before: change.kind === 'added' ? null : change.before,
    after: change.kind === 'removed' ? null : change.after,
  }
}

/** 命中的事实多于保存的证据条数时的说明；否则为空串。 */
export function evidenceOverflowNote(item: Pick<ImpactItem, 'fact_count' | 'evidence'>): string {
  return item.fact_count > item.evidence.length
    ? `该章共有 ${item.fact_count} 条相关事实，这里只展示前 ${item.evidence.length} 条`
    : ''
}

// ---------------- 第一级：派生内容的跳转 ----------------

/** 章摘要 → 该章编辑页的分析抽屉。 */
export function summaryHref(projectId: number, chapterId: number): string {
  return `/projects/${projectId}/chapters/${chapterId}?${ANALYSIS_PANEL_QUERY}`
}

/** 剧情记忆页没有按条目定位的入口，只能到记忆页。 */
export function memoriesHref(projectId: number): string {
  return `/projects/${projectId}/memories`
}

export const PROGRESSION_KIND_LABELS: Record<string, string> = {
  character: '角色', world: '世界观', relationship: '人物关系', career: '角色职业', membership: '组织成员',
  organization: '组织', career_entry: '职业',
}

type ProgressionTarget = Pick<StaleProgression, 'subject_kind' | 'timeline_id' | 'character_id' | 'world_setting_id' | 'career_id' | 'organization_id'>

/**
 * 设定变化 → 对应设定详情。四类条目带上「本章章末」的位置，现有详情页据此直接打开「字段与剧情变化」；
 * 人物关系按稳定时间线打开历史；角色职业、组织成员挂在角色上，只能到该角色。主体缺失时退到所在页面。
 */
export function progressionHref(projectId: number, chapterId: number, item: ProgressionTarget): string {
  const position = { anchor_kind: 'chapter', chapter_id: chapterId, chapter_no: null, boundary: 'end' } as const
  const base = `/projects/${projectId}`
  switch (item.subject_kind) {
    case 'relationship':
      return `${base}/relationships?timeline_id=${item.timeline_id}`
    case 'world':
      return item.world_setting_id === null ? `${base}/world` : entityDetailHref(projectId, { kind: 'world', id: item.world_setting_id }, position)
    case 'organization':
      return item.organization_id === null ? `${base}/organizations` : entityDetailHref(projectId, { kind: 'organization', id: item.organization_id }, position)
    case 'career_entry':
      return item.career_id === null ? `${base}/careers` : entityDetailHref(projectId, { kind: 'career', id: item.career_id }, position)
    case 'character':
      return item.character_id === null ? `${base}/characters` : entityDetailHref(projectId, { kind: 'character', id: item.character_id }, position)
    case 'career':
    case 'membership':
      return item.character_id === null ? `${base}/characters` : entityDetailHref(projectId, { kind: 'character', id: item.character_id })
    default:
      return `${base}/characters`
  }
}

// ---------------- 评估按钮 ----------------

export const ASSESS_BLOCK_TEXT: Record<AssessBlock, string> = {
  no_baseline: '本章还没有改动前的事实台账，请先运行一次一致性检查；之后再修改本章才能评估影响。',
  unchanged: '本章自上次抽取事实后没有改动，暂无可评估的变化。',
  empty: '章节正文为空，无法评估影响。',
}

export interface AssessGate {
  /** 空串 = 可以评估 */
  reason: string
  /** 需要先运行一致性检查（提供入口） */
  needsCheck: boolean
}

/**
 * 评估按钮的禁用原因。章节自身的原因优先（它决定了等检查结束后是否值得再来）；
 * 其次是互斥：检查与评估同一作品同时只能有一个在进行。
 */
export function assessGate(input: {
  detail: Pick<ImpactDetail, 'can_assess' | 'assess_block'>
  checkBusy: boolean
  impactBusy: boolean
  hasConfig: boolean | null
}): AssessGate {
  const block = input.detail.assess_block
  if (block) return { reason: ASSESS_BLOCK_TEXT[block], needsCheck: block === 'no_baseline' }
  if (!input.detail.can_assess) return { reason: '当前无法评估本章的影响，请刷新后重试。', needsCheck: false }
  if (input.checkBusy) return { reason: '已有进行中的一致性检查，结束后才能评估影响。', needsCheck: false }
  if (input.impactBusy) return { reason: '已有进行中的修改影响评估，请等待其完成。', needsCheck: false }
  if (input.hasConfig === false) return { reason: '尚未配置可用的文本模型。', needsCheck: false }
  return { reason: '', needsCheck: false }
}

// ---------------- 确认状态：本地勾选与冲突合并 ----------------

/**
 * 尚未保存的本地勾选，叠加在服务端详情之上：键是影响项的章节 id／设定变化 id，值是作者选的状态。
 * 与服务端值相同的条目等于没改。冲突或刷新后只替换详情，本地勾选原样保留，再次保存即以最新修订提交。
 */
export interface ImpactEdits {
  items: Readonly<Record<number, ImpactItemStatus>>
  acknowledged: Readonly<Record<number, boolean>>
}
export const NO_IMPACT_EDITS: ImpactEdits = { items: {}, acknowledged: {} }

type EditSource = Pick<ImpactDetail, 'revision' | 'stale_progressions'> & {
  assessment: { assessed: boolean; items: Pick<ImpactItem, 'chapter_id' | 'status'>[] } | null
}

function editableItems(detail: EditSource): Pick<ImpactItem, 'chapter_id' | 'status'>[] {
  return detail.assessment?.assessed ? detail.assessment.items : []
}

export function itemStatus(item: Pick<ImpactItem, 'chapter_id' | 'status'>, edits: ImpactEdits): ImpactItemStatus {
  return edits.items[item.chapter_id] ?? item.status
}

export function isAcknowledged(item: Pick<StaleProgression, 'id' | 'acknowledged'>, edits: ImpactEdits): boolean {
  return edits.acknowledged[item.id] ?? item.acknowledged
}

export function setItemStatus(edits: ImpactEdits, chapterId: number, status: ImpactItemStatus): ImpactEdits {
  return { ...edits, items: { ...edits.items, [chapterId]: status } }
}

export function setAcknowledged(edits: ImpactEdits, progressionId: number, value: boolean): ImpactEdits {
  return { ...edits, acknowledged: { ...edits.acknowledged, [progressionId]: value } }
}

/**
 * 只提交改过的影响项；「已确认无需更新」有改动时提交当前仍过期的设定变化中已确认的 id（服务端只替换这部分，
 * 此前确认过、现已不再过期的设定变化不受影响）。已不在最新详情里的影响项与设定变化不提交。没有改动时为 null。
 */
export function impactUpdateBody(detail: EditSource, edits: ImpactEdits): ImpactUpdate | null {
  const body: ImpactUpdate = { revision: detail.revision }
  const items = editableItems(detail)
    .filter((item) => itemStatus(item, edits) !== item.status)
    .map((item) => ({ chapter_id: item.chapter_id, status: itemStatus(item, edits) }))
  if (items.length > 0) body.items = items
  if (detail.stale_progressions.some((item) => isAcknowledged(item, edits) !== item.acknowledged)) {
    body.acknowledged = detail.stale_progressions.filter((item) => isAcknowledged(item, edits)).map((item) => item.id).sort((a, b) => a - b)
  }
  return body.items === undefined && body.acknowledged === undefined ? null : body
}

export function isImpactDirty(detail: EditSource | null | undefined, edits: ImpactEdits): boolean {
  return !!detail && impactUpdateBody(detail, edits) !== null
}

/** 待复核的影响项数（按本地勾选后的状态）。 */
export function pendingCount(detail: EditSource, edits: ImpactEdits): number {
  return editableItems(detail).filter((item) => itemStatus(item, edits) === 'pending').length
}

// ---------------- 只读显隐 ----------------

/**
 * 界面能力（真实授权仍由后端执行）：查看 content.read；确认复核与「已确认无需更新」content.edit；评估影响与
 * 发起检查 ai.use。取消只对发起者本人（接口返回了 task_key 或本页正在订阅）且仍有 AI 能力时显示。
 */
export function impactPermissions(access: ConsistencyAccess, ownsRunningTask = false) {
  const canView = access.canRead
  return {
    canView,
    canConfirm: canView && access.canEdit,
    canAssess: canView && access.canUseAI,
    canStartCheck: canView && access.canUseAI,
    canCancelAssess: canView && access.canUseAI && ownsRunningTask,
  }
}

// ---------------- 快捷检查范围 ----------------

export interface CheckRange { startChapterId: number; endChapterId: number }

/** 「对这些章节运行一致性检查」的预填范围：现存章节里章号最小与最大的两章；全部已删除时为 null。 */
export function uncoveredCheckRange(chapters: readonly ImpactChapterRef[]): CheckRange | null {
  const live = chapters.flatMap((chapter) => (chapter.chapter_no === null ? [] : [{ id: chapter.chapter_id, no: chapter.chapter_no }]))
  if (live.length === 0) return null
  const sorted = [...live].sort((a, b) => a.no - b.no || a.id - b.id)
  return { startChapterId: sorted[0].id, endChapterId: sorted[sorted.length - 1].id }
}

// ---------------- 编辑页提示 ----------------

/** 章节编辑页页头的提示文案；没有过期派生内容（或尚未读到）时为 null，不显示。 */
export function editorImpactHint(detail: Pick<ImpactDetail, 'chapter_id' | 'stale_count'> | null | undefined, chapterId: number): string | null {
  if (!detail || detail.chapter_id !== chapterId || !Number.isFinite(detail.stale_count) || detail.stale_count <= 0) return null
  return `本章改动后有 ${detail.stale_count} 项派生内容可能过期`
}

// ---------------- 任务结束 ----------------

/** 评估任务结束后的提示；任务仍在排队、运行或待审核时返回 null（不能当作已结束）。 */
export function impactEndNotice(status: TaskStatus, detail = ''): TaskEndNotice | null {
  if (status === 'queued' || status === 'running' || status === 'paused') return null
  if (status === 'succeeded') return { level: 'success', text: '修改影响评估已完成' }
  if (status === 'cancelled') return { level: 'info', text: '评估已取消，正文与设定未受影响' }
  return { level: 'error', text: detail || '修改影响评估未完成' }
}
