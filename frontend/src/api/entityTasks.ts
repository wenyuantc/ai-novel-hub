// AI 单体生成 / 旧关系解析任务的结果收窄与保存载荷换算（纯函数）

import type {
  CareerAssignDraft,
  CareerKind,
  CareerSuggestion,
  CharacterDraft,
  CharacterGenerateResult,
  CharacterInput,
  GeneratedRelationSuggestion,
  LifeStatus,
  MemberSuggestion,
  OrganizationDraft,
  OrganizationGenerateResult,
  RelationCategory,
  RelationDraft,
  RelationshipCreate,
  Relationship,
  RelationStatus,
  RelationSuggestion,
  RelationsParseResult,
  RelationsGenerateResult,
  TaskKind,
} from './types'

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

const str = (v: unknown): string => (typeof v === 'string' ? v : '')
const num = (v: unknown, fallback = 0): number => (typeof v === 'number' && Number.isFinite(v) ? v : fallback)
const numOrNull = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const strList = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [])

const CATEGORIES: readonly string[] = ['family', 'social', 'hostile', 'work', 'other']
const STATUSES: readonly string[] = ['normal', 'broken', 'past', 'complicated']
const LIFE: readonly string[] = ['alive', 'dead', 'missing', 'other']

export function asCategory(v: string): RelationCategory {
  return (CATEGORIES.includes(v) ? v : 'other') as RelationCategory
}

export function asRelationStatus(v: string): RelationStatus {
  return (STATUSES.includes(v) ? v : 'normal') as RelationStatus
}

export function asLifeStatus(v: string): LifeStatus {
  return (LIFE.includes(v) ? v : 'alive') as LifeStatus
}

function readSuggestion(v: unknown): RelationSuggestion | null {
  if (!isRecord(v)) return null
  const t = isRecord(v.type) ? v.type : {}
  const targetId = numOrNull(v.target_id)
  return {
    target_name: str(v.target_name),
    target_id: targetId,
    resolved: v.resolved === true && targetId !== null,
    reason: str(v.reason),
    existing_relationship_id: numOrNull(v.existing_relationship_id),
    self_role: str(v.self_role),
    target_role: str(v.target_role),
    subject_is_a: v.subject_is_a !== false,
    type: { type_id: numOrNull(t.type_id), role_a: str(t.role_a), role_b: str(t.role_b), category: str(t.category) || 'other' },
    intimacy: Math.max(-100, Math.min(100, num(v.intimacy))),
    status: str(v.status) || 'normal',
    description: str(v.description),
  }
}

function readSuggestions(v: unknown): RelationSuggestion[] {
  return Array.isArray(v) ? v.map(readSuggestion).filter((x): x is RelationSuggestion => x !== null) : []
}

export function parseCharacterGenerateResult(result: unknown): CharacterGenerateResult | null {
  if (!isRecord(result) || !isRecord(result.draft) || typeof result.draft.name !== 'string') return null
  const d = result.draft
  const draft: CharacterDraft = {
    name: str(d.name),
    aliases: strList(d.aliases),
    role_type: str(d.role_type),
    age: str(d.age),
    gender: str(d.gender),
    appearance: str(d.appearance),
    personality: str(d.personality),
    background: str(d.background),
    profile: str(d.profile),
    current_state: str(d.current_state),
    life_status: str(d.life_status) || 'alive',
    always_in_context: d.always_in_context === true,
  }
  return {
    draft,
    relation_suggestions: readSuggestions(result.relation_suggestions),
    career_suggestions: readCareerSuggestions(result.career_suggestions),
    warnings: strList(result.warnings),
  }
}

const kindOrNull = (v: unknown): CareerKind | null => (v === 'main' || v === 'sub' ? v : null)

function readCareerSuggestions(v: unknown): CareerSuggestion[] {
  if (!Array.isArray(v)) return []
  return v.filter(isRecord).map((c) => {
    const careerId = numOrNull(c.career_id)
    const stageId = numOrNull(c.stage_id)
    return {
      career_name: str(c.career_name),
      career_id: careerId,
      career_kind: kindOrNull(c.career_kind),
      role: kindOrNull(c.role),
      stage_name: str(c.stage_name),
      stage_id: stageId,
      stage_position: numOrNull(c.stage_position),
      note: str(c.note),
      resolved: c.resolved === true && careerId !== null && stageId !== null,
      reason: str(c.reason),
    }
  })
}

/** 预览中可编辑的职业建议（可改阶段与备注） */
export interface EditableCareerSuggestion extends CareerSuggestion {
  key: string
  checked: boolean
}

export function toEditableCareers(list: CareerSuggestion[]): EditableCareerSuggestion[] {
  return list.map((c, i) => ({ ...c, key: `${i}:${c.career_name}`, checked: c.resolved }))
}

/** 勾选的职业建议超出主 1 副 3 时的提示（null = 未超出）；后端仍会校验 */
export function careerSuggestionLimitError(list: EditableCareerSuggestion[]): string | null {
  const picked = list.filter((c) => c.checked && c.resolved)
  if (picked.filter((c) => c.role === 'main').length > 1) return '该角色已有主职业'
  if (picked.filter((c) => c.role === 'sub').length > 3) return '副职业最多 3 个'
  return null
}

/** 勾选的职业建议 → with-relations 的 careers */
export function careerSuggestionsToDrafts(list: EditableCareerSuggestion[]): CareerAssignDraft[] {
  return list
    .filter((c) => c.checked && c.resolved && c.career_id !== null)
    .map((c) => ({ career_id: c.career_id as number, stage_id: c.stage_id, note: c.note.trim().slice(0, 200) }))
}

export function parseOrganizationGenerateResult(result: unknown): OrganizationGenerateResult | null {
  if (!isRecord(result) || !isRecord(result.draft) || typeof result.draft.name !== 'string') return null
  const d = result.draft
  const draft: OrganizationDraft = {
    name: str(d.name),
    org_type: str(d.org_type),
    purpose: str(d.purpose),
    location: str(d.location),
    power_level: numOrNull(d.power_level),
    description: str(d.description),
    parent_name: typeof d.parent_name === 'string' && d.parent_name ? d.parent_name : null,
    parent_id: numOrNull(d.parent_id),
  }
  const members: MemberSuggestion[] = Array.isArray(result.member_suggestions)
    ? result.member_suggestions.filter(isRecord).map((m) => {
        const cid = numOrNull(m.character_id)
        return {
          character_name: str(m.character_name),
          character_id: cid,
          position: str(m.position),
          resolved: m.resolved === true && cid !== null,
          reason: str(m.reason),
        }
      })
    : []
  return { draft, member_suggestions: members, warnings: strList(result.warnings) }
}

export function parseRelationsParseResult(result: unknown): RelationsParseResult | null {
  if (!isRecord(result) || typeof result.character_id !== 'number') return null
  return {
    character_id: result.character_id,
    character_name: str(result.character_name),
    suggestions: readSuggestions(result.suggestions),
    warnings: strList(result.warnings),
  }
}

const positiveId = (v: unknown): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v > 0
const pairKey = (a: number, b: number): string => `${Math.min(a, b)}:${Math.max(a, b)}`

/** 生成结果的身份和字段契约在 API 边界收窄，组件只消费已标明有效性的建议。 */
export function parseRelationsGenerateResult(result: unknown): RelationsGenerateResult | null {
  if (!isRecord(result) || !Array.isArray(result.character_ids) || !Array.isArray(result.suggestions)) return null
  const ids = result.character_ids
  if (ids.length < 2 || ids.length > 20 || !ids.every(positiveId) || new Set(ids).size !== ids.length) return null
  const selected = new Set(ids)
  const seen = new Set<string>()
  const suggestions: GeneratedRelationSuggestion[] = []
  for (const raw of result.suggestions) {
    const s = readSuggestion(raw)
    if (!s || !isRecord(raw)) continue
    const subjectId = positiveId(raw.subject_id) ? raw.subject_id : null
    const targetId = positiveId(raw.target_id) ? raw.target_id : null
    const pair = subjectId !== null && targetId !== null ? pairKey(subjectId, targetId) : null
    let reason = s.reason
    if (subjectId === null || targetId === null || !selected.has(subjectId) || !selected.has(targetId)) {
      reason = '关系双方必须是本次所选角色 ID'
    } else if (subjectId === targetId) reason = '不能与自己建立关系'
    else if (s.existing_relationship_id !== null) reason = '两人之间已有关系'
    else if (pair !== null && seen.has(pair)) reason = '同一角色对重复建议（含反向）'
    else if (!isRecord(raw.type) || typeof raw.type.category !== 'string' || !CATEGORIES.includes(raw.type.category)
      || typeof raw.status !== 'string' || !STATUSES.includes(raw.status) || typeof raw.subject_is_a !== 'boolean'
      || !validSuggestionFields(s) || !Number.isInteger(raw.intimacy) || raw.intimacy !== s.intimacy) {
      reason = '关系字段无效，无法保存'
    }
    if (pair !== null) seen.add(pair)
    suggestions.push({
      ...s,
      subject_id: subjectId,
      subject_name: str(raw.subject_name),
      target_id: targetId,
      resolved: s.resolved && !reason,
      reason: reason || (s.resolved ? '' : '无法保存该建议'),
    })
  }
  return { character_ids: ids, suggestions, warnings: strList(result.warnings) }
}

type ExistingPair = Pick<Relationship, 'id' | 'a_id' | 'b_id'>

/** 可补充的无序角色对数量，已有关系的两个方向都算已占用。 */
export function missingRelationPairCount(ids: readonly number[], relationships: readonly ExistingPair[]): number {
  const selected = new Set(ids)
  const existing = new Set(relationships.filter((r) => selected.has(r.a_id) && selected.has(r.b_id)).map((r) => pairKey(r.a_id, r.b_id)))
  return Math.max(0, selected.size * (selected.size - 1) / 2 - existing.size)
}

/** 角色草稿 → 创建请求体（可编辑预览后提交） */
export function draftToCharacterInput(d: CharacterDraft): CharacterInput {
  return {
    name: d.name.trim(),
    aliases: d.aliases,
    role_type: d.role_type.trim(),
    age: d.age,
    gender: d.gender,
    appearance: d.appearance,
    personality: d.personality,
    background: d.background,
    profile: d.profile,
    current_state: d.current_state,
    life_status: asLifeStatus(d.life_status),
    always_in_context: d.always_in_context,
  }
}

/**
 * 预览中可编辑的关系建议：称呼被改动（≠ 原建议）时丢弃 type_id，按「主体=role_a」重新提交 role_a/role_b，
 * 由后端匹配（含反向）或新建自定义类型。
 */
export interface EditableSuggestion extends RelationSuggestion {
  key: string
  checked: boolean
  /** 当前编辑后的称呼（初始 = self_role / target_role） */
  edit_self_role: string
  edit_target_role: string
}

export function toEditable<T extends RelationSuggestion>(list: T[]): (T & EditableSuggestion)[] {
  return list.map((s, i) => ({
    ...s,
    key: `${i}:${s.target_name}`,
    checked: s.resolved,
    edit_self_role: s.self_role,
    edit_target_role: s.target_role,
  }))
}

function rolesEdited(s: EditableSuggestion): boolean {
  return s.edit_self_role.trim() !== s.self_role || s.edit_target_role.trim() !== s.target_role
}

/** 关系建议 → 类型引用 + 主体是否为 a 一方 */
export function suggestionTypeRef(s: EditableSuggestion): {
  type_id: number | null
  role_a: string
  role_b: string
  category: RelationCategory
  subject_is_a: boolean
} {
  const category = asCategory(s.type.category)
  if (rolesEdited(s) || s.type.type_id === null) {
    return {
      type_id: null,
      role_a: s.edit_self_role.trim(),
      role_b: s.edit_target_role.trim(),
      category,
      subject_is_a: true,
    }
  }
  return { type_id: s.type.type_id, role_a: s.type.role_a, role_b: s.type.role_b, category, subject_is_a: s.subject_is_a }
}

/** 是否可以保存：已解析、已勾选、称呼非空 */
export function isSavable(s: EditableSuggestion): boolean {
  return s.resolved && s.checked && s.target_id !== null && validSuggestionFields({
    ...s, self_role: s.edit_self_role.trim(), target_role: s.edit_target_role.trim(),
    type: { ...s.type, category: asCategory(s.type.category) }, status: asRelationStatus(s.status),
  })
}

function validSuggestionFields(s: RelationSuggestion): boolean {
  return !!s.self_role.trim() && !!s.target_role.trim() && s.self_role.trim().length <= 20 && s.target_role.trim().length <= 20
    && Number.isInteger(s.intimacy) && s.intimacy >= -100 && s.intimacy <= 100
    && CATEGORIES.includes(s.type.category) && STATUSES.includes(s.status)
}

/** 新角色一起保存的关系（with-relations） */
export function suggestionsToDrafts(list: EditableSuggestion[]): RelationDraft[] {
  return list.filter(isSavable).map((s) => {
    const t = suggestionTypeRef(s)
    return {
      target_id: s.target_id as number,
      subject_is_a: t.subject_is_a,
      type_id: t.type_id,
      role_a: t.role_a,
      role_b: t.role_b,
      category: t.category,
      intimacy: s.intimacy,
      status: asRelationStatus(s.status),
      description: s.description,
    }
  })
}

/** 已有角色的关系建议 → relationships/batch 条目：a_id = subject_is_a ? 主体 : 目标 */
export function suggestionsToBatch(
  subjectId: number,
  list: EditableSuggestion[],
  source: RelationshipCreate['source'] = 'ai_parse',
): RelationshipCreate[] {
  return list.filter(isSavable).map((s) => {
    const t = suggestionTypeRef(s)
    const target = s.target_id as number
    return {
      a_id: t.subject_is_a ? subjectId : target,
      b_id: t.subject_is_a ? target : subjectId,
      type_id: t.type_id,
      role_a: t.role_a,
      role_b: t.role_b,
      category: t.category,
      intimacy: s.intimacy,
      status: asRelationStatus(s.status),
      description: s.description,
      source,
    }
  })
}

export type EditableGeneratedSuggestion = EditableSuggestion & Pick<GeneratedRelationSuggestion, 'subject_id' | 'subject_name'>

/** 预览重开时排除已经确认保存的角色对；保留未保存的编辑和勾选。 */
export function filterExistingGeneratedSuggestions(
  list: EditableGeneratedSuggestion[],
  relationships: readonly ExistingPair[],
): EditableGeneratedSuggestion[] {
  const existing = new Map(relationships.map((r) => [pairKey(r.a_id, r.b_id), r.id]))
  return list.map((s) => {
    const id = s.subject_id !== null && s.target_id !== null ? existing.get(pairKey(s.subject_id, s.target_id)) : undefined
    return id === undefined ? s : { ...s, resolved: false, checked: false, existing_relationship_id: id, reason: '两人之间已有关系' }
  })
}

/** 只提交所选、勾选且字段有效的缺失对，再按无序对去重。 */
export function generatedSuggestionsToBatch(list: EditableGeneratedSuggestion[], ids: readonly number[]): RelationshipCreate[] {
  const selected = new Set(ids)
  const seen = new Set<string>()
  return list.flatMap((s) => {
    if (s.subject_id === null || s.target_id === null || s.subject_id === s.target_id
      || !selected.has(s.subject_id) || !selected.has(s.target_id) || !validSuggestionFields(s) || !isSavable(s)) return []
    const pair = pairKey(s.subject_id, s.target_id)
    if (seen.has(pair)) return []
    seen.add(pair)
    return suggestionsToBatch(s.subject_id, [s], 'ai_generate')
  })
}

/** 有结果查看组件的任务类型（任务面板据此显示「查看结果」） */
export const ENTITY_TASK_KINDS: readonly TaskKind[] = [
  'character.generate',
  'organization.generate',
  'relations.parse',
  'relations.generate',
  'career.generate',
]
