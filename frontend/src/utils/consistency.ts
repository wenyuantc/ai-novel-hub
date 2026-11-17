// 一致性检查页的纯函数：标签、证据跳转、筛选参数、范围映射、状态草稿与冲突、只读显隐、请求代次。

import { ApiError } from '../api/client'
import type {
  CheckScope, ConsistencyIssue, ConsistencyRun, FactCategory, IssueEvidence, IssueQuery, IssueSeverity, IssueStatus,
  IssueUpdate, ItemState, RunScope, RunStats, RunStatus,
} from '../api/consistency'
import type { TaskStatus } from '../api/types'
import { MENTION_FROM_PARAM, mentionHref } from './mentions'

export const ISSUE_PAGE_SIZE = 20
/** 有检查在进行时轮询概要的间隔（无法订阅时是唯一的进度来源；订阅中作为流中断或无事件时的兜底） */
export const CHECK_POLL_MS = 3000

export const ISSUE_CATEGORY_LABELS: Record<FactCategory, string> = {
  character_history: '人物经历', character_state: '人物状态', item: '物品去向', timeline: '时间顺序', foreshadow: '伏笔前提',
}
export const ISSUE_SEVERITY: Record<IssueSeverity, { label: string; color: string }> = {
  high: { label: '严重', color: 'error' }, medium: { label: '中等', color: 'warning' }, low: { label: '轻微', color: 'default' },
}
export const ISSUE_STATUS: Record<IssueStatus, { label: string; color: string }> = {
  open: { label: '待处理', color: 'processing' }, resolved: { label: '已解决', color: 'success' }, ignored: { label: '已忽略', color: 'default' },
}
export const RUN_STATUS: Record<RunStatus, { label: string; color: string }> = {
  running: { label: '进行中', color: 'processing' }, succeeded: { label: '已完成', color: 'success' }, failed: { label: '失败', color: 'error' },
  cancelled: { label: '已取消', color: 'warning' }, interrupted: { label: '已中断', color: 'magenta' },
}
const SKIPPED_STATE_LABELS: Partial<Record<ItemState, string>> = {
  skipped_changed: '检查期间正文被修改，本次结果未保存', skipped_missing: '章节已删除', failed: '事实抽取失败',
}
export function skippedStateLabel(state: ItemState): string {
  return SKIPPED_STATE_LABELS[state] ?? '未处理'
}

// ---------------- 证据跳转 ----------------

export const EVIDENCE_DELETED = '章节已删除'
export const EVIDENCE_UNANCHORED = '未能逐字定位，将跳到该章开头'
export const EVIDENCE_STALE = '正文已修改，位置可能失效'

export type EvidenceLink =
  | { kind: 'deleted'; notes: string[] }
  /** anchored = 带原文锚点；chapter_start = 无偏移，只能到该章开头 */
  | { kind: 'anchored' | 'chapter_start'; readerHref: string; editorHref: string; notes: string[] }

type EvidenceTarget = Pick<IssueEvidence, 'chapter_id' | 'chapter_no' | 'chapter_title' | 'start' | 'end' | 'text_hash' | 'stale'>

/**
 * 证据的跳转目标：复用提及锚点的查询参数（mention_start / mention_end / mention_hash），由阅读器与编辑器既有的
 * 定位与失效提示处理。正文已变（stale）仍带原锚点跳转，由目标页按指纹提示失效而不是套用旧位置。
 */
export function evidenceLink(projectId: number, evidence: EvidenceTarget): EvidenceLink {
  const { chapter_id: chapterId, start, end } = evidence
  if (chapterId === null || evidence.chapter_no === null) return { kind: 'deleted', notes: [EVIDENCE_DELETED] }
  const reader = `/projects/${projectId}/read/${chapterId}`
  const editor = `/projects/${projectId}/chapters/${chapterId}`
  const anchored = start !== null && end !== null && Number.isSafeInteger(start) && Number.isSafeInteger(end)
    && start >= 0 && end > start && /^[a-f0-9]{64}$/.test(evidence.text_hash)
  if (!anchored) {
    return { kind: 'chapter_start', readerHref: reader, editorHref: editor, notes: [EVIDENCE_UNANCHORED, ...(evidence.stale ? [EVIDENCE_STALE] : [])] }
  }
  const readerHref = mentionHref(
    projectId,
    { type: 'chapter', id: chapterId, title: evidence.chapter_title ?? '', chapter_no: evidence.chapter_no, conversation_id: null, created_at: null },
    { start, end, content_hash: evidence.text_hash },
  )
  // 标明来源：目标页在位置失效时据此使用不指向「提及列表」的提示
  const query = `${readerHref.slice(reader.length)}&${MENTION_FROM_PARAM}=consistency`
  return { kind: 'anchored', readerHref: `${reader}${query}`, editorHref: `${editor}${query}`, notes: evidence.stale ? [EVIDENCE_STALE] : [] }
}

export function evidenceChapterLabel(evidence: Pick<IssueEvidence, 'chapter_no' | 'chapter_title'>): string {
  if (evidence.chapter_no === null) return EVIDENCE_DELETED
  return `第 ${evidence.chapter_no} 章${evidence.chapter_title ? ` · ${evidence.chapter_title}` : ''}`
}

/** 问题涉及的章节（去重、按章号）；已删除的章节合并为一项「章节已删除」。 */
export function issueChaptersLabel(issue: Pick<ConsistencyIssue, 'evidence'>): string {
  const numbers = [...new Set(issue.evidence.flatMap((item) => (item.chapter_no === null ? [] : [item.chapter_no])))].sort((a, b) => a - b)
  const parts = numbers.map((no) => `第 ${no} 章`)
  if (issue.evidence.some((item) => item.chapter_no === null)) parts.push(EVIDENCE_DELETED)
  return parts.join('、')
}

// ---------------- 列表筛选 ----------------

export type RecheckFilter = 'yes' | 'no'
export interface IssueFilter { status?: IssueStatus; category?: FactCategory; chapterId?: number; recheck?: RecheckFilter }

/** 筛选 + 页码 → 查询参数；未选的条件不出现在请求里。 */
export function issueListQuery(filter: IssueFilter, page: number): IssueQuery {
  const query: IssueQuery = { page: Math.max(1, Math.trunc(page) || 1), page_size: ISSUE_PAGE_SIZE }
  if (filter.status) query.status = filter.status
  if (filter.category) query.category = filter.category
  if (filter.chapterId !== undefined && Number.isSafeInteger(filter.chapterId) && filter.chapterId > 0) query.chapter_id = filter.chapterId
  if (filter.recheck) query.needs_recheck = filter.recheck === 'yes'
  return query
}

export function hasIssueFilter(filter: IssueFilter): boolean {
  return !!(filter.status || filter.category || filter.chapterId !== undefined || filter.recheck)
}

// ---------------- 检查范围 ----------------

export interface ScopeChapter { id: number; chapter_no: number }
export interface ScopeValues { mode: CheckScope['mode']; startChapterId?: number | null; endChapterId?: number | null }

/** 范围选择 → 请求体。全书检查不带起止章节（后端拒绝）；按范围必须起止齐备、属于本作品且起始不晚于结束。 */
export function checkScopeBody(values: ScopeValues, chapters: readonly ScopeChapter[]): { body: CheckScope } | { error: string } {
  if (values.mode === 'all') return { body: { mode: 'all' } }
  const start = chapters.find((chapter) => chapter.id === values.startChapterId)
  const end = chapters.find((chapter) => chapter.id === values.endChapterId)
  if (!values.startChapterId || !values.endChapterId) return { error: '请选择起止章节' }
  if (!start || !end) return { error: '所选章节已不存在，请重新选择' }
  if (start.chapter_no > end.chapter_no) return { error: '起始章节不能晚于结束章节' }
  return { body: { mode: 'range', start_chapter_id: start.id, end_chapter_id: end.id } }
}

/** 最近一次检查的范围文案；起止章节已删除时退回章数。 */
export function runScopeLabel(scope: RunScope, chapters: readonly ScopeChapter[]): string {
  if (scope.mode === 'all') return '全书'
  if (scope.mode === 'issue') return scope.issue_id === null ? '重新核查单个问题' : `重新核查问题 #${scope.issue_id}`
  const start = chapters.find((chapter) => chapter.id === scope.start_chapter_id)
  const end = chapters.find((chapter) => chapter.id === scope.end_chapter_id)
  if (start && end) return start.id === end.id ? `第 ${start.chapter_no} 章` : `第 ${start.chapter_no}–${end.chapter_no} 章`
  return `所选范围（${scope.chapter_ids.length} 章）`
}

/** 结果概要的数值行（0 也显示，便于确认本次确实没有新增）。 */
export function runStatRows(stats: RunStats): { label: string; value: number }[] {
  return [
    { label: '参与检查的章节', value: stats.total },
    { label: '重新抽取', value: stats.extracted },
    { label: '复用台账', value: stats.reused },
    { label: '跳过', value: stats.skipped },
    { label: '抽取失败', value: stats.failed },
    { label: '新增问题', value: stats.created },
    { label: '再次检出', value: stats.updated },
    { label: '其中已解决后再次出现', value: stats.reappeared },
    { label: '本次未复现', value: stats.not_reproduced },
  ]
}

/** 概要中需要提醒的附加说明：范围外过期台账、比对批次失败。 */
export function runNotices(run: Pick<ConsistencyRun, 'stats'>): string[] {
  const { stats } = run
  const notices: string[] = []
  if (stats.stale_outside > 0) notices.push(`范围外有 ${stats.stale_outside} 章的事实台账已过期，未参与本次比对`)
  if (stats.failed_batches > 0) notices.push(`${stats.batches} 批比对中有 ${stats.failed_batches} 批失败，本次不标记「本次未复现」`)
  return [...notices, ...stats.warnings]
}

// ---------------- 状态与备注草稿 ----------------

export const NOTE_LIMIT = 2000

/** 表单草稿：base* 是草稿所基于的服务端值，用来区分「作者改过」与「只是旧值」。 */
export interface IssueDraft {
  issueId: number
  revision: number
  status: IssueStatus
  note: string
  baseStatus: IssueStatus
  baseNote: string
}
type DraftSource = Pick<ConsistencyIssue, 'id' | 'revision' | 'status' | 'note'>

export function draftFromIssue(issue: DraftSource): IssueDraft {
  return { issueId: issue.id, revision: issue.revision, status: issue.status, note: issue.note, baseStatus: issue.status, baseNote: issue.note }
}

export function isDraftDirty(draft: IssueDraft): boolean {
  return draft.status !== draft.baseStatus || draft.note !== draft.baseNote
}

/**
 * 冲突后重新读取到服务端最新值：作者改过的字段保留本地输入，没改过的字段跟随服务端；修订号换成最新的，
 * 再次保存即以最新修订提交。不是同一个问题时直接取服务端值。
 */
export function rebaseDraft(draft: IssueDraft, fresh: DraftSource): IssueDraft {
  if (draft.issueId !== fresh.id) return draftFromIssue(fresh)
  return {
    issueId: fresh.id,
    revision: fresh.revision,
    status: draft.status !== draft.baseStatus ? draft.status : fresh.status,
    note: draft.note !== draft.baseNote ? draft.note : fresh.note,
    baseStatus: fresh.status,
    baseNote: fresh.note,
  }
}

/** 只提交改过的字段；没有改动时为 null（后端要求 status / note 至少给一个）。 */
export function issueUpdateBody(draft: IssueDraft): IssueUpdate | null {
  const body: IssueUpdate = { revision: draft.revision }
  if (draft.status !== draft.baseStatus) body.status = draft.status
  if (draft.note !== draft.baseNote) body.note = draft.note
  return body.status === undefined && body.note === undefined ? null : body
}

export function isStaleRevision(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409 && err.body?.code === 'stale_revision'
}

/** 同一作品已有进行中的检查（发起 / 重新核查被拒）：应刷新概要接上进行中的任务。 */
export function isCheckConflict(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409
}

// ---------------- 只读显隐 ----------------

export interface ConsistencyAccess { canRead: boolean; canEdit: boolean; canUseAI: boolean }

/**
 * 界面能力（真实授权仍由后端执行）：查看 content.read；改状态与备注 content.edit；发起检查与重新核查 ai.use。
 * 取消只对发起者本人（概要返回了 task_key）且仍有 AI 能力时显示。
 */
export function consistencyPermissions(access: ConsistencyAccess, ownsRunningTask = false) {
  const canView = access.canRead
  return {
    canView,
    canUpdateIssue: canView && access.canEdit,
    canStartCheck: canView && access.canUseAI,
    canRecheck: canView && access.canUseAI,
    canCancelCheck: canView && access.canUseAI && ownsRunningTask,
  }
}

// ---------------- 进度订阅的兜底 ----------------

/**
 * 实时订阅期间仍轮询概要：订阅流可能被中断、报错或长时间没有事件（代理不传递上游中断时连接既不结束也无数据）。
 * 概要已不把所订阅的任务列为进行中时返回 'verify'：调用方读取任务真实状态，确认结束后断开订阅并刷新。
 * 概要尚未加载、或仍列着该任务时返回 'none'（刚发起时概要可能还是旧数据，所以不能直接当作已结束）。
 */
export function streamWatchAction(input: {
  streaming: boolean; followedTaskId: number | null; summaryLoaded: boolean; runningTaskId: number | null
}): 'none' | 'verify' {
  if (!input.streaming || input.followedTaskId === null || !input.summaryLoaded) return 'none'
  return input.runningTaskId === input.followedTaskId ? 'none' : 'verify'
}

export interface TaskEndNotice { level: 'success' | 'info' | 'error'; text: string }
/** 检查任务结束后的提示；任务仍在排队、运行或待审核时返回 null（不能当作已结束）。 */
export function checkEndNotice(status: TaskStatus, detail = ''): TaskEndNotice | null {
  if (status === 'queued' || status === 'running' || status === 'paused') return null
  if (status === 'succeeded') return { level: 'success', text: '一致性检查已完成' }
  if (status === 'cancelled') return { level: 'info', text: '检查已取消，已完成章节的事实台账已保留' }
  return { level: 'error', text: detail || '一致性检查未完成' }
}

// ---------------- 请求代次 ----------------

/** 每次 begin 使之前的代次失效：切换作品、身份、筛选或再次发起后，迟到的响应不得回填。 */
export function createRequestGate() {
  let generation = 0
  return {
    begin(): () => boolean {
      const mine = ++generation
      return () => mine === generation
    },
    invalidate(): void { generation += 1 },
  }
}

/** 只在本代次仍有效时交付结果；失效返回 stale，调用方不得使用也不得提示错误。 */
export async function settleCurrent<T>(isCurrent: () => boolean, work: Promise<T>): Promise<{ status: 'ok'; value: T } | { status: 'error'; error: unknown } | { status: 'stale' }> {
  try {
    const value = await work
    return isCurrent() ? { status: 'ok', value } : { status: 'stale' }
  } catch (error) {
    return isCurrent() ? { status: 'error', error } : { status: 'stale' }
  }
}
