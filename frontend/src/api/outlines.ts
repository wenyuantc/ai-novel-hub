// 大纲进阶（outline-pro）：大纲概览 / 排序 / 追加、细纲同步与建章、快照、导入导出、续写与展开任务
// 只被大纲页（懒加载路由）、章节编辑器与任务结果组件引用，不得进入入口 chunk。

import { api, request } from './client'
import type {
  ExpandDraftCharacter,
  Outline,
  OutlineAppendItem,
  OutlineContinueDraft,
  OutlineContinueRequest,
  OutlineContinueResult,
  OutlineDetail,
  OutlineExpandDraft,
  OutlineExpandExisting,
  OutlineExpandRequest,
  OutlineExpandResult,
  OutlineImportPreview,
  OutlineImportResult,
  OutlineMode,
  OutlineMoveDirection,
  OutlineSnapshot,
  OutlineSnapshotDetail,
  Plan,
  PlanBuildResult,
  PlanInput,
  PlansSync,
  PlanUpdate,
  SnapshotReason,
  SnapshotRestoreResult,
  Task,
} from './types'

const base = (pid: number) => `/projects/${pid}`

export const outlineProApi = {
  tree: (pid: number) => api.get<OutlineDetail[]>(`${base(pid)}/outlines/tree`),
  move: (pid: number, oid: number, direction: OutlineMoveDirection) =>
    api.post<Outline[]>(`${base(pid)}/outlines/${oid}/move`, { direction }),
  append: (pid: number, items: OutlineAppendItem[]) => api.post<Outline[]>(`${base(pid)}/outlines/append`, { items }),
  plans: (pid: number, oid: number) => api.get<Plan[]>(`${base(pid)}/outlines/${oid}/plans`),
  updatePlan: (pid: number, oid: number, planId: number, body: PlanUpdate) =>
    api.patch<Plan>(`${base(pid)}/outlines/${oid}/plans/${planId}`, body),
  syncPlans: (pid: number, oid: number, body: PlansSync) =>
    request<Plan[]>(`${base(pid)}/outlines/${oid}/plans`, { method: 'PUT', body }),
  build: (pid: number, oid: number) => api.post<PlanBuildResult>(`${base(pid)}/outlines/${oid}/plans/build`),
  chapterPlan: (pid: number, cid: number) => api.get<Plan | null>(`${base(pid)}/chapters/${cid}/plan`),
  /** 202 Task：outline.continue */
  continue: (pid: number, body: OutlineContinueRequest) => api.post<Task>(`${base(pid)}/outlines/continue`, body),
  /** 202 Task：outline.expand */
  expand: (pid: number, oid: number, body: OutlineExpandRequest) =>
    api.post<Task>(`${base(pid)}/outlines/${oid}/expand`, body),
  export: (pid: number) => api.get<Record<string, unknown>>(`${base(pid)}/outlines/export`),
  importPreview: (pid: number, data: unknown) =>
    api.post<OutlineImportPreview>(`${base(pid)}/outlines/import/preview`, { data }),
  import: (pid: number, data: unknown, mode: OutlineMode) =>
    api.post<OutlineImportResult>(`${base(pid)}/outlines/import`, { data, mode }),
}

export const snapshotApi = {
  list: (pid: number) => api.get<OutlineSnapshot[]>(`${base(pid)}/outline-snapshots`),
  create: (pid: number, note: string) => api.post<OutlineSnapshot>(`${base(pid)}/outline-snapshots`, { note }),
  get: (pid: number, sid: number) => api.get<OutlineSnapshotDetail>(`${base(pid)}/outline-snapshots/${sid}`),
  restore: (pid: number, sid: number) =>
    api.post<SnapshotRestoreResult>(`${base(pid)}/outline-snapshots/${sid}/restore`),
  remove: (pid: number, sid: number) => api.del(`${base(pid)}/outline-snapshots/${sid}`),
}

export const SNAPSHOT_REASON_LABELS: Record<SnapshotReason, { label: string; color: string }> = {
  wizard_replace: { label: '向导覆盖前', color: 'orange' },
  import_replace: { label: '导入替换前', color: 'purple' },
  reexpand: { label: '重新展开前', color: 'blue' },
  manual: { label: '手动保存', color: 'green' },
  restore: { label: '恢复前', color: 'magenta' },
}

export const MIN_EST_WORDS = 300
export const MAX_EST_WORDS = 20000
export const DEFAULT_EST_WORDS = 3000

// ---------------- 大纲页展示 ----------------

/** 展开状态文案：未展开（1 纲 1 章，是否已建章）/ N 章细纲 · M 章已建 */
export function expandStatus(o: Pick<OutlineDetail, 'plan_count' | 'built_count' | 'chapter_id'>): {
  label: string
  color: string
} {
  if (o.plan_count === 0) return o.chapter_id !== null ? { label: '未展开 · 已建章', color: 'green' } : { label: '未展开', color: 'default' }
  if (o.built_count === 0) return { label: `${o.plan_count} 章细纲 · 未建章`, color: 'blue' }
  if (o.built_count >= o.plan_count) return { label: `${o.plan_count} 章细纲 · 全部已建`, color: 'green' }
  return { label: `${o.plan_count} 章细纲 · ${o.built_count} 章已建`, color: 'gold' }
}

/** 大纲占用的章节号：已建章的细纲取其章节号范围，否则取大纲 chapter_no */
export function chapterRange(o: Pick<OutlineDetail, 'chapter_no' | 'plans'>): string {
  const nos = o.plans.map((p) => p.chapter_no).filter((n): n is number => n !== null)
  if (nos.length === 0) return String(o.chapter_no)
  const lo = Math.min(...nos)
  const hi = Math.max(...nos)
  return lo === hi ? String(lo) : `${lo}–${hi}`
}

/** 可以「建章」：有未建章的细纲 */
export function canBuild(o: Pick<OutlineDetail, 'plan_count' | 'built_count'>): boolean {
  return o.plan_count > o.built_count
}

/** 可以走「1 纲 1 章」批量建章：未展开且未关联章节 */
export function canCreateSingleChapter(o: Pick<OutlineDetail, 'plan_count' | 'chapter_id'>): boolean {
  return o.plan_count === 0 && o.chapter_id === null
}

/** 细纲 → 整体同步中的一条（带 id，全字段，避免 PUT 把未提交的字段重置为默认值） */
export function planToInput(p: Plan): PlanInput {
  return {
    id: p.id,
    title: p.title,
    summary: p.summary,
    key_events: [...p.key_events],
    character_ids: [...p.character_ids],
    emotion: p.emotion,
    goal: p.goal,
    est_words: p.est_words,
  }
}

/** 删除单条细纲：其余细纲原样（带 id）提交；有正文的细纲不能删（后端也会 409） */
export function removePlanSync(plans: Plan[], removeId: number): PlansSync {
  return { plans: plans.filter((p) => p.id !== removeId).map(planToInput) }
}

// ---------------- 续写预览 ----------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((x): x is string => typeof x === 'string') : []
}

function str(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function num(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

/** 收窄 outline.continue 的 done.result；结构不对返回 null */
export function parseContinueResult(result: unknown): OutlineContinueResult | null {
  if (!isRecord(result) || !Array.isArray(result.drafts)) return null
  const drafts: OutlineContinueDraft[] = result.drafts.filter(isRecord).map((d) => ({
    volume_no: num(d.volume_no, 1),
    volume_title: str(d.volume_title),
    chapter_no: num(d.chapter_no, 1),
    title: str(d.title),
    summary: str(d.summary),
  }))
  return { drafts, warnings: strings(result.warnings) }
}

export interface EditableContinueDraft {
  key: string
  checked: boolean
  volume_no: number
  volume_title: string
  title: string
  summary: string
}

export function toEditableContinueDrafts(drafts: OutlineContinueDraft[]): EditableContinueDraft[] {
  return drafts.map((d, i) => ({
    key: `c${i}`,
    checked: true,
    volume_no: d.volume_no,
    volume_title: d.volume_title,
    title: d.title,
    summary: d.summary,
  }))
}

/** 采纳后的章节号预览：已勾选的草稿从 startNo 起依次编号（后端从当前末尾顺延，与此一致） */
export function continuePreviewNos(drafts: EditableContinueDraft[], startNo: number): Map<string, number> {
  const out = new Map<string, number>()
  let n = startNo
  for (const d of drafts) {
    if (!d.checked) continue
    out.set(d.key, n)
    n += 1
  }
  return out
}

export function continueDraftError(d: Pick<EditableContinueDraft, 'title' | 'volume_title'>): string | null {
  const title = d.title.trim()
  if (!title) return '标题不能为空'
  if (title.length > 200) return '标题不超过 200 字'
  if (d.volume_title.trim().length > 200) return '卷名不超过 200 字'
  return null
}

/** 已勾选草稿 → POST /outlines/append 的 items（章节号由后端顺延） */
export function continueAppendItems(drafts: EditableContinueDraft[]): OutlineAppendItem[] {
  return drafts
    .filter((d) => d.checked)
    .map((d) => ({
      volume_no: Math.max(1, Math.trunc(d.volume_no)),
      volume_title: d.volume_title.trim(),
      title: d.title.trim(),
      summary: d.summary.trim(),
    }))
}

// ---------------- 展开预览 ----------------

function parseCharacters(value: unknown): ExpandDraftCharacter[] {
  if (!Array.isArray(value)) return []
  return value.filter(isRecord).map((c) => {
    const id = typeof c.character_id === 'number' ? c.character_id : null
    return { name: str(c.name), character_id: id, resolved: c.resolved === true && id !== null }
  })
}

/** 收窄 outline.expand 的 done.result；结构不对返回 null */
export function parseExpandResult(result: unknown): OutlineExpandResult | null {
  if (!isRecord(result) || typeof result.outline_id !== 'number' || !Array.isArray(result.drafts)) return null
  const drafts: OutlineExpandDraft[] = result.drafts.filter(isRecord).map((d) => ({
    title: str(d.title),
    summary: str(d.summary),
    key_events: strings(d.key_events),
    characters: parseCharacters(d.characters),
    emotion: str(d.emotion),
    goal: str(d.goal),
    est_words: num(d.est_words, DEFAULT_EST_WORDS),
  }))
  const existing: OutlineExpandExisting[] = (Array.isArray(result.existing) ? result.existing : [])
    .filter(isRecord)
    .filter((e) => typeof e.id === 'number')
    .map((e) => ({
      id: e.id as number,
      seq: num(e.seq, 0),
      title: str(e.title),
      summary: str(e.summary),
      chapter_id: typeof e.chapter_id === 'number' ? e.chapter_id : null,
      chapter_no: typeof e.chapter_no === 'number' ? e.chapter_no : null,
      has_content: e.has_content === true,
    }))
  return { outline_id: result.outline_id, drafts, existing, warnings: strings(result.warnings) }
}

export interface EditablePlanDraft {
  key: string
  checked: boolean
  title: string
  summary: string
  key_events: string[]
  /** 只取 resolved 的角色 */
  character_ids: number[]
  /** 不在角色表中的名字（仅提示，不提交） */
  unresolved: string[]
  emotion: string
  goal: string
  est_words: number
}

function clampWords(n: number): number {
  return Math.min(MAX_EST_WORDS, Math.max(MIN_EST_WORDS, Math.round(n)))
}

export function toEditablePlanDrafts(drafts: OutlineExpandDraft[]): EditablePlanDraft[] {
  return drafts.map((d, i) => {
    const ids: number[] = []
    for (const c of d.characters) if (c.resolved && c.character_id !== null && !ids.includes(c.character_id)) ids.push(c.character_id)
    return {
      key: `p${i}`,
      checked: true,
      title: d.title,
      summary: d.summary,
      key_events: [...d.key_events],
      character_ids: ids,
      unresolved: d.characters.filter((c) => !c.resolved && c.name).map((c) => c.name),
      emotion: d.emotion,
      goal: d.goal,
      est_words: clampWords(d.est_words),
    }
  })
}

export function planDraftError(d: Pick<EditablePlanDraft, 'title' | 'emotion' | 'est_words'>): string | null {
  const title = d.title.trim()
  if (!title) return '标题不能为空'
  if (title.length > 200) return '标题不超过 200 字'
  if (d.emotion.trim().length > 100) return '情感基调不超过 100 字'
  if (!Number.isFinite(d.est_words) || d.est_words < MIN_EST_WORDS || d.est_words > MAX_EST_WORDS)
    return `预估字数应在 ${MIN_EST_WORDS}–${MAX_EST_WORDS} 之间`
  return null
}

/** 重新展开时必须保留的细纲：已建章且章节有正文 */
export function keptPlans(plans: Plan[]): Plan[] {
  return plans.filter((p) => p.has_content).sort((a, b) => a.seq - b.seq)
}

/** 将被替换的细纲：没有正文（已建的空章节会随之删除） */
export function replacedPlans(plans: Plan[]): Plan[] {
  return plans.filter((p) => !p.has_content).sort((a, b) => a.seq - b.seq)
}

/**
 * 展开预览采纳 → PUT /outlines/{oid}/plans：保留的细纲（带 id，全字段）在前，其后为已勾选的草稿（无 id = 新建）；
 * 没有正文的旧细纲不提交 = 删除。
 */
export function buildExpandSync(kept: Plan[], drafts: EditablePlanDraft[]): PlansSync {
  return {
    plans: [
      ...kept.map(planToInput),
      ...drafts
        .filter((d) => d.checked)
        .map((d) => ({
          title: d.title.trim(),
          summary: d.summary.trim(),
          key_events: d.key_events.map((e) => e.trim()).filter(Boolean),
          character_ids: [...d.character_ids],
          emotion: d.emotion.trim(),
          goal: d.goal.trim(),
          est_words: clampWords(d.est_words),
        })),
    ],
  }
}

// ---------------- 导入导出 ----------------

/** 与后端 MAX_OUTLINE_FILE_BYTES 一致：紧凑序列化后的 UTF-8 字节数 */
export const MAX_OUTLINE_BYTES = 2 * 1024 * 1024
/** 读取前按文件大小粗筛（容纳导出文件的缩进） */
export const MAX_OUTLINE_FILE_BYTES = 4 * MAX_OUTLINE_BYTES

const OUTLINE_TOO_LARGE = '大纲文件过大，上限为 2MB'

/** 解析大纲文件文本：JSON 语法与大小在本地先查，结构由后端校验 */
export function parseOutlineFileText(text: string): { data: unknown; error: string | null } {
  if (text.length > MAX_OUTLINE_FILE_BYTES) return { data: null, error: OUTLINE_TOO_LARGE }
  let data: unknown
  try {
    data = JSON.parse(text) as unknown
  } catch {
    return { data: null, error: '文件不是有效的 JSON' }
  }
  if (new TextEncoder().encode(JSON.stringify(data)).length > MAX_OUTLINE_BYTES) return { data: null, error: OUTLINE_TOO_LARGE }
  return { data, error: null }
}

/** 导出文件名：{项目名}-大纲-{YYYYMMDD}.json */
export function outlineFilename(projectTitle: string, date = new Date()): string {
  const safe = projectTitle.replace(/[\\/:*?"<>|]/g, '_').trim() || 'ainovel'
  const d = `${date.getFullYear()}${String(date.getMonth() + 1).padStart(2, '0')}${String(date.getDate()).padStart(2, '0')}`
  return `${safe}-大纲-${d}.json`
}
