// 一致性检查：预览 / 发起（consistency.check 任务，202）、概要、问题清单与状态；
// 修改影响：派生内容过期清单、评估影响（consistency.impact 任务，202）与确认状态。
// 类型镜像 backend/app/schemas/consistency.py；偏移一律是 Chapter.content 的码点半开区间。

import { request } from './client'
import type { Task } from './types'

export type FactCategory = 'character_history' | 'character_state' | 'item' | 'timeline' | 'foreshadow'
export type IssueSeverity = 'high' | 'medium' | 'low'
export type IssueStatus = 'open' | 'resolved' | 'ignored'
export type RunStatus = 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted'
export type CheckMode = 'all' | 'range'
/** 运行记录的范围：除全书／起止章节外，还有「重新核查某个问题」 */
export type RunMode = CheckMode | 'issue'
export type ItemState = 'pending' | 'extracted' | 'reused' | 'skipped_changed' | 'skipped_missing' | 'failed'

/** 检查范围：全书，或起止章节（按章号两端包含）；全书检查不能带起止章节。 */
export interface CheckScope {
  mode: CheckMode
  start_chapter_id?: number | null
  end_chapter_id?: number | null
}
export interface CheckRequest extends CheckScope { ai_config_id?: number | null }

export interface CheckPreview {
  /** 范围内章数 */
  total: number
  /** 需要重新抽取（将调用模型）的章数 */
  to_extract: number
  /** 台账可复用的章数 */
  reusable: number
  /** 空章数（不参与检查） */
  empty: number
  warnings: string[]
}

export interface RunSkipped {
  chapter_id: number
  state: ItemState
  error: string
  /** 章节已删除时为 null */
  chapter_no: number | null
  chapter_title: string | null
}

export interface RunStats {
  total: number
  extracted: number
  reused: number
  skipped: number
  failed: number
  created: number
  /** 已存在且本次再次检出的问题数（含 reappeared） */
  updated: number
  reappeared: number
  not_reproduced: number
  stale_outside: number
  batches: number
  failed_batches: number
  skipped_chapters: RunSkipped[]
  warnings: string[]
}

export interface RunScope {
  mode: RunMode
  start_chapter_id: number | null
  end_chapter_id: number | null
  issue_id: number | null
  chapter_ids: number[]
}

export interface ConsistencyRun {
  id: number
  task_id: number | null
  scope: RunScope
  status: RunStatus
  stats: RunStats
  started_at: string | null
  finished_at: string | null
  created_at: string
}

export interface LedgerCoverage { fresh: number; stale: number; missing: number }

export interface ConsistencySummary {
  last_run: ConsistencyRun | null
  counts_by_status: Record<IssueStatus, number>
  ledger_coverage: LedgerCoverage
  /** 本作品进行中（排队或运行）的检查任务 */
  running_task_id: number | null
  /** 只在任务属于当前用户时返回（订阅进度需要） */
  running_task_key: string | null
}

export interface IssueEvidence {
  /** null = 章节在导入的备份中已不存在 */
  chapter_id: number | null
  /** null = 章节已删除 */
  chapter_no: number | null
  chapter_title: string | null
  fact_id: string
  quote: string
  /** 无法逐字定位时 start / end 同为 null */
  start: number | null
  end: number | null
  text_hash: string
  /** 章节已删除，或当前正文指纹与 text_hash 不同 */
  stale: boolean
}

export interface ConsistencyIssue {
  id: number
  project_id: number
  category: FactCategory
  title: string
  description: string
  suggestion: string
  severity: IssueSeverity
  status: IssueStatus
  note: string
  status_changed_by: number | null
  status_changed_at: string | null
  revision: number
  evidence: IssueEvidence[]
  reappeared: boolean
  needs_recheck: boolean
  not_reproduced: boolean
  created_at: string
  updated_at: string
}

export interface IssuePage { items: ConsistencyIssue[]; total: number; page: number; page_size: number }

export interface IssueQuery {
  status?: IssueStatus
  category?: FactCategory
  chapter_id?: number
  needs_recheck?: boolean
  page?: number
  page_size?: number
}

/** 状态与备注的 CAS 写入：status / note 至少给一个且不能为 null（清空备注传空字符串）。 */
export interface IssueUpdate { revision: number; status?: IssueStatus; note?: string }

// ---------------- 修改影响 ----------------

export type ImpactChangeKind = 'added' | 'removed' | 'changed'
export type ImpactItemStatus = 'pending' | 'reviewed' | 'dismissed'
export type LedgerState = 'fresh' | 'stale' | 'missing'
/** 不能评估的原因：no_baseline = 没有改动前的台账；unchanged = 台账与当前正文一致且没有上一版；empty = 正文为空 */
export type AssessBlock = 'no_baseline' | 'unchanged' | 'empty'

/** 一处剧情事实变化；before / after 是改动前 / 后的事实陈述（新增时 before 为空，消失时 after 为空）。 */
export interface ImpactChange {
  kind: ImpactChangeKind
  category: FactCategory
  subject: string
  subject_key: string
  before: string
  after: string
}

export interface ImpactEvidence {
  fact_id: string
  quote: string
  /** 无法逐字定位时 start / end 同为 null */
  start: number | null
  end: number | null
  text_hash: string
}

/** 一个可能受影响的后续章节。 */
export interface ImpactItem {
  chapter_id: number
  /** null = 章节已删除 */
  chapter_no: number | null
  chapter_title: string | null
  subjects: string[]
  evidence: ImpactEvidence[]
  /** 该章台账中命中变化主体的事实总数（evidence 只保存前几条） */
  fact_count: number
  status: ImpactItemStatus
  /** 章节已删除，或当前正文指纹与证据记录时不同 */
  changed: boolean
}

export interface ImpactChapterRef { chapter_id: number; chapter_no: number | null; chapter_title: string | null }

/** 最近一次评估。assessed = false 表示只保存过第一级的确认、尚未评估。 */
export interface ImpactAssessment {
  assessed: boolean
  assessed_at: string | null
  /** 评估后本章正文（或 AI 可见区块）又有修改 */
  outdated: boolean
  /** 已评估且未发现剧情事实变化 */
  no_change: boolean
  changes: ImpactChange[]
  items: ImpactItem[]
  /** 评估时台账缺失或过期、无法判断的后续章节 */
  uncovered: ImpactChapterRef[]
  model: string
}

export interface StaleSummary { analysis_id: number; analyzed_at: string }
export interface StaleMemory { id: number; kind: string; content: string }
export type StaleProgressionKind = 'character' | 'world' | 'relationship' | 'career' | 'membership' | 'organization' | 'career_entry'
/** 由本章旧分析写入、来源正文已变的设定变化。 */
export interface StaleProgression {
  id: number
  timeline_id: number
  subject_kind: StaleProgressionKind | (string & {})
  character_id: number | null
  related_character_id: number | null
  world_setting_id: number | null
  career_id: number | null
  organization_id: number | null
  subject_name: string
  field_key: string | null
  custom_field_id: number | null
  anchor_label: string
  /** 作者已确认无需更新（不再计入过期数） */
  acknowledged: boolean
}

export interface ImpactRunning {
  /** 本作品进行中的影响评估任务（任一章） */
  running_task_id: number | null
  /** 只给发起者本人（订阅进度需要） */
  running_task_key: string | null
  running_chapter_id: number | null
}

export interface ImpactDetail extends ImpactRunning {
  chapter_id: number
  chapter_no: number
  chapter_title: string
  /** 是否有过章节分析；没有时摘要不算过期 */
  analyzed: boolean
  stale_summary: StaleSummary | null
  stale_memories: StaleMemory[]
  stale_progressions: StaleProgression[]
  /** 摘要 + 记忆 + 未确认的设定变化 */
  stale_count: number
  ledger_state: LedgerState
  can_assess: boolean
  assess_block: AssessBlock | null
  assessment: ImpactAssessment | null
  /** 确认状态的 CAS；0 = 尚无记录 */
  revision: number
}

export interface ImpactChapterSummary {
  chapter_id: number
  chapter_no: number
  chapter_title: string
  stale_summary: boolean
  stale_memories: number
  /** 未确认的过期设定变化数 */
  stale_progressions: number
  stale_count: number
  ledger_state: LedgerState
  assessed: boolean
  assessed_at: string | null
  /** 最近一次评估的任务 id（任务已被清理时为 null）；任务面板的跳转据此定位到章节 */
  task_id: number | null
  outdated: boolean
  no_change: boolean
  changes: number
  items: number
  pending_items: number
  /** 被列章节在评估后又有修改（或已删除）的影响项数 */
  changed_items: number
  uncovered: number
}

/** 有过期派生内容或有评估记录的章节，按章号排序。 */
export interface ImpactOverview extends ImpactRunning { chapters: ImpactChapterSummary[] }

/**
 * 确认状态的 CAS 写入：items / acknowledged 至少给一个且不能为 null。items 只改列出的影响项；
 * acknowledged 是合并语义：给出本章当前仍过期的设定变化中已确认的 id，服务端只替换这部分，此前确认过、现已不再
 * 过期的 id 原样保留。尚无记录时 revision 传 0，且只能保存 acknowledged。
 */
export interface ImpactUpdate {
  revision: number
  items?: { chapter_id: number; status: ImpactItemStatus }[]
  acknowledged?: number[]
}

const base = (pid: number) => `/projects/${pid}/consistency`

export const consistencyApi = {
  preview: (pid: number, scope: CheckScope, signal?: AbortSignal) =>
    request<CheckPreview>(`${base(pid)}/checks/preview`, { method: 'POST', body: scope, signal }),
  start: (pid: number, body: CheckRequest) => request<Task>(`${base(pid)}/checks`, { method: 'POST', body }),
  run: (pid: number, runId: number, signal?: AbortSignal, notificationId?: string) =>
    request<ConsistencyRun>(`${base(pid)}/runs/${runId}`, { signal, query: { notification_id: notificationId } }),
  summary: (pid: number, signal?: AbortSignal) => request<ConsistencySummary>(`${base(pid)}/summary`, { signal }),
  issues: (pid: number, query: IssueQuery = {}, signal?: AbortSignal) =>
    request<IssuePage>(`${base(pid)}/issues`, { query: { ...query }, signal }),
  issue: (pid: number, id: number, signal?: AbortSignal) => request<ConsistencyIssue>(`${base(pid)}/issues/${id}`, { signal }),
  update: (pid: number, id: number, body: IssueUpdate) =>
    request<ConsistencyIssue>(`${base(pid)}/issues/${id}`, { method: 'PATCH', body }),
  /** 范围 = 该问题现存证据章节（202） */
  recheck: (pid: number, id: number, aiConfigId?: number | null) =>
    request<Task>(`${base(pid)}/issues/${id}/recheck`, { method: 'POST', body: { ai_config_id: aiConfigId ?? null } }),
  /** 概览要读全书正文计算指纹：只在进入页签、任务结束与手动刷新时读取，不用于轮询 */
  impacts: (pid: number, signal?: AbortSignal) => request<ImpactOverview>(`${base(pid)}/impacts`, { signal }),
  /** 只报告进行中的评估任务（轻量）：评估进行中按它轮询 */
  impactRunning: (pid: number, signal?: AbortSignal) => request<ImpactRunning>(`${base(pid)}/impacts/running`, { signal }),
  impact: (pid: number, chapterId: number, signal?: AbortSignal) =>
    request<ImpactDetail>(`${base(pid)}/impacts/${chapterId}`, { signal }),
  /** 评估影响（202）；没有改动前的台账、正文为空或未改动时 400，与检查互斥 409 */
  assess: (pid: number, chapterId: number, aiConfigId?: number | null) =>
    request<Task>(`${base(pid)}/impacts/${chapterId}/assess`, { method: 'POST', body: { ai_config_id: aiConfigId ?? null } }),
  updateImpact: (pid: number, chapterId: number, body: ImpactUpdate) =>
    request<ImpactDetail>(`${base(pid)}/impacts/${chapterId}`, { method: 'PATCH', body }),
}
