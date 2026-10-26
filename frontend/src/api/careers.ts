// 职业等级体系：职业 / 阶段 CRUD、持有人、角色挂接与历史、AI 生成职业任务，以及相关纯函数

import { api } from './client'
import type {
  Assignment,
  AssignmentInput,
  AssignmentUpdate,
  Career,
  CareerDraft,
  CareerGenerateRequest,
  CareerGenerateResult,
  CareerHistory,
  CareerHolder,
  CareerInput,
  CareerKind,
  CareerStage,
  CareerUpdate,
  StageInput,
  Task,
} from './types'

const base = (pid: number) => `/projects/${pid}/careers`
const charBase = (pid: number, cid: number) => `/projects/${pid}/characters/${cid}`

export const careerApi = {
  list: (pid: number) => api.get<Career[]>(base(pid)),
  get: (pid: number, id: number) => api.get<Career>(`${base(pid)}/${id}`),
  create: (pid: number, body: CareerInput) => api.post<Career>(base(pid), body),
  /** AI 预览确认后一次保存（单事务，任一同名 → 409 全部回滚） */
  batch: (pid: number, careers: CareerInput[]) => api.post<Career[]>(`${base(pid)}/batch`, { careers }),
  update: (pid: number, id: number, body: CareerUpdate) => api.patch<Career>(`${base(pid)}/${id}`, body),
  remove: (pid: number, id: number) => api.del(`${base(pid)}/${id}`),
  holders: (pid: number, id: number) => api.get<CareerHolder[]>(`${base(pid)}/${id}/holders`),
  /** AI 生成职业：202 任务（career.generate），只产出草稿 */
  generate: (pid: number, body: CareerGenerateRequest) => api.post<Task>(`${base(pid)}/generate`, body),
}

export const assignmentApi = {
  /** 项目内全部角色的挂接（角色卡片汇总，一次请求；按角色、主职业在前） */
  listProject: (pid: number) => api.get<Assignment[]>(`/projects/${pid}/character-careers`),
  list: (pid: number, cid: number) => api.get<Assignment[]>(`${charBase(pid, cid)}/careers`),
  create: (pid: number, cid: number, body: AssignmentInput) => api.post<Assignment>(`${charBase(pid, cid)}/careers`, body),
  update: (pid: number, cid: number, aid: number, body: AssignmentUpdate) =>
    api.patch<Assignment>(`${charBase(pid, cid)}/careers/${aid}`, body),
  remove: (pid: number, cid: number, aid: number) => api.del(`${charBase(pid, cid)}/careers/${aid}`),
  /** 倒序 */
  history: (pid: number, cid: number) => api.get<CareerHistory[]>(`${charBase(pid, cid)}/career-history`),
}

/** 与后端 MAX_MAIN_CAREERS / MAX_SUB_CAREERS 一致 */
export const MAX_MAIN_CAREERS = 1
export const MAX_SUB_CAREERS = 3
export const MAX_STAGE_NAME = 50

export const CAREER_KIND_LABELS: Record<CareerKind, string> = { main: '主修', sub: '副修' }
/** 挂接身份（role）的展示：主职业 / 副职业 */
export const CAREER_ROLE_LABELS: Record<CareerKind, string> = { main: '主职业', sub: '副职业' }

export function asCareerKind(v: unknown): CareerKind {
  return v === 'sub' ? 'sub' : 'main'
}

export function careerKindLabel(kind: string): string {
  return kind === 'main' || kind === 'sub' ? CAREER_KIND_LABELS[kind] : kind || '-'
}

export const CAREER_SOURCE_LABELS: Record<string, string> = {
  manual: '手动',
  wizard: '向导',
  ai: 'AI 生成',
  import: '卡片导入',
  analysis: '章节分析',
}

export const CAREER_HISTORY_FIELD_LABELS: Record<string, string> = {
  role: '类别',
  stage: '阶段',
  note: '状态备注',
  removed: '卸下',
}

/** 历史中的单个值：role 显示为主职业 / 副职业，空值显示（空） */
export function formatCareerChange(field: string, value: unknown): string {
  if (value === null || value === undefined || value === '') return '（空）'
  if (field === 'role' && (value === 'main' || value === 'sub')) return CAREER_ROLE_LABELS[value]
  return typeof value === 'string' || typeof value === 'number' ? String(value) : JSON.stringify(value)
}

/** 「筑基（2/9）」 */
export function stageLabel(name: string, position: number, total: number): string {
  return `${name}（${position}/${total}）`
}

/** 「剑修·筑基（2/9，瓶颈期）」——与后端上下文中的角色卡写法一致 */
export function formatAssignment(a: Pick<Assignment, 'career_name' | 'stage_name' | 'stage_position' | 'stage_total' | 'note'>): string {
  const note = a.note.trim()
  return `${a.career_name}·${a.stage_name}（${a.stage_position}/${a.stage_total}${note ? `，${note}` : ''}）`
}

/** 「练气 → 筑基 → 金丹」；超过 max 阶时截断并以「…」结尾 */
export function ladderText(stages: Pick<CareerStage, 'name'>[], max = 12): string {
  const names = stages.map((s) => s.name)
  return names.length > max ? `${names.slice(0, max).join(' → ')} → …` : names.join(' → ')
}

// ---------------- 阶段编辑（职业抽屉 / AI 草稿共用） ----------------

/** 编辑中的一行阶段：id 为空表示新建；key 仅用于渲染 */
export interface StageRow {
  key: string
  id: number | null
  name: string
  description: string
}

let stageSeq = 0
export function newStageRow(name = '', description = ''): StageRow {
  stageSeq += 1
  return { key: `new-${stageSeq}`, id: null, name, description }
}

export function stagesToRows(stages: { id?: number; name: string; description: string }[]): StageRow[] {
  return stages.map((s) =>
    s.id === undefined
      ? newStageRow(s.name, s.description)
      : { key: `id-${s.id}`, id: s.id, name: s.name, description: s.description },
  )
}

/** 把 index 处的元素移动 delta 位（越界时原样返回） */
export function moveItem<T>(list: T[], index: number, delta: number): T[] {
  const to = index + delta
  if (index < 0 || index >= list.length || to < 0 || to >= list.length) return list
  const next = [...list]
  const [item] = next.splice(index, 1)
  next.splice(to, 0, item)
  return next
}

/** 与后端一致的阶段校验：至少 1 阶、名称非空、≤50 字、同一职业内不重复（trim 后比较） */
export function validateStages(rows: Pick<StageRow, 'name'>[]): string | null {
  if (rows.length === 0) return '职业至少需要 1 个阶段'
  const seen = new Set<string>()
  for (const r of rows) {
    const name = r.name.trim()
    if (!name) return '阶段名称不能为空'
    if (name.length > MAX_STAGE_NAME) return `阶段名称不能超过 ${MAX_STAGE_NAME} 字`
    if (seen.has(name)) return '同一职业内阶段名称不能重复'
    seen.add(name)
  }
  return null
}

/** 提交载荷：顺序即 position；已有阶段带 id，新阶段不带 */
export function rowsToStages(rows: StageRow[]): StageInput[] {
  return rows.map((r) =>
    r.id === null
      ? { name: r.name.trim(), description: r.description }
      : { id: r.id, name: r.name.trim(), description: r.description },
  )
}

// ---------------- 挂接上限（前端预检，后端仍会校验） ----------------

/** 角色挂接某职业的阻止原因（null = 可以挂）；文案与后端 409 一致 */
export function assignBlockReason(existing: Pick<Assignment, 'career_id' | 'role'>[], career: Pick<Career, 'id' | 'kind'>): string | null {
  if (existing.some((a) => a.career_id === career.id)) return '该角色已挂该职业'
  if (career.kind === 'main' && existing.filter((a) => a.role === 'main').length >= MAX_MAIN_CAREERS) return '该角色已有主职业'
  if (career.kind === 'sub' && existing.filter((a) => a.role === 'sub').length >= MAX_SUB_CAREERS) return `副职业最多 ${MAX_SUB_CAREERS} 个`
  return null
}

// ---------------- 角色卡片上的主职业 ----------------

export interface CharacterCareerBadge {
  main: string | null
  subs: string[]
}

/** 项目内全部挂接（`GET /character-careers`）→ 角色 id → {主职业「剑修·筑基（2/9）」, 副职业名单} */
export function careerBadges(assignments: Assignment[]): Map<number, CharacterCareerBadge> {
  const map = new Map<number, CharacterCareerBadge>()
  for (const a of assignments) {
    const badge = map.get(a.character_id) ?? { main: null, subs: [] }
    const text = formatAssignment(a)
    if (a.role === 'main') badge.main = text
    else badge.subs.push(text)
    map.set(a.character_id, badge)
  }
  return map
}

// ---------------- AI 生成职业（career.generate）结果 ----------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

const str = (v: unknown): string => (typeof v === 'string' ? v : '')
const strList = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [])

export function parseCareerGenerateResult(result: unknown): CareerGenerateResult | null {
  if (!isRecord(result) || !Array.isArray(result.drafts)) return null
  const drafts: CareerDraft[] = result.drafts.filter(isRecord).map((d) => ({
    name: str(d.name),
    kind: asCareerKind(d.kind),
    description: str(d.description),
    requirements: str(d.requirements),
    abilities: str(d.abilities),
    rules: str(d.rules),
    stages: Array.isArray(d.stages)
      ? d.stages.filter(isRecord).map((s) => ({ name: str(s.name), description: str(s.description) }))
      : [],
    conflict: d.conflict === true,
  }))
  return { drafts, warnings: strList(result.warnings) }
}

/** 预览中可编辑的职业草稿 */
export interface EditableCareerDraft {
  key: string
  checked: boolean
  name: string
  kind: CareerKind
  description: string
  requirements: string
  abilities: string
  rules: string
  stages: StageRow[]
  /** 后端标记的同名冲突与当时的名字：改名后才解除 */
  original_conflict: boolean
  original_name: string
}

export function toEditableDrafts(drafts: CareerDraft[]): EditableCareerDraft[] {
  return drafts.map((d, i) => ({
    key: `${i}:${d.name}`,
    checked: !d.conflict,
    name: d.name,
    kind: d.kind,
    description: d.description,
    requirements: d.requirements,
    abilities: d.abilities,
    rules: d.rules,
    stages: stagesToRows(d.stages),
    original_conflict: d.conflict,
    original_name: d.name,
  }))
}

export const DRAFT_NAME_CONFLICT = '与已有职业同名，请改名'

/**
 * 草稿不能保存的原因（null = 可保存）：名字为空 / 与已有职业同名（后端标记且未改名，或与已加载的职业名单相同）/
 * 与本批其他勾选草稿同名 / 阶段不合法。
 */
export function draftBlockReason(d: EditableCareerDraft, all: EditableCareerDraft[], existingNames: ReadonlySet<string>): string | null {
  const name = d.name.trim()
  if (!name) return '请填写职业名称'
  if (name.length > 100) return '职业名称不能超过 100 字'
  if ((d.original_conflict && name === d.original_name.trim()) || existingNames.has(name)) return DRAFT_NAME_CONFLICT
  if (all.some((o) => o.key !== d.key && o.checked && o.name.trim() === name)) return '与本次其他职业同名'
  return validateStages(d.stages)
}

/** 勾选且可保存的草稿 → careers/batch 载荷（不含 conflict） */
export function draftsToBatch(list: EditableCareerDraft[], existingNames: ReadonlySet<string>): CareerInput[] {
  return list
    .filter((d) => d.checked && draftBlockReason(d, list, existingNames) === null)
    .map((d) => ({
      name: d.name.trim(),
      kind: d.kind,
      description: d.description,
      requirements: d.requirements,
      abilities: d.abilities,
      rules: d.rules,
      stages: rowsToStages(d.stages),
    }))
}
