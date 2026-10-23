// AI 单体生成 / 旧关系解析任务的结果收窄与保存载荷换算（纯函数）

import type {
  CharacterDraft,
  CharacterGenerateResult,
  CharacterInput,
  LifeStatus,
  MemberSuggestion,
  OrganizationDraft,
  OrganizationGenerateResult,
  RelationCategory,
  RelationDraft,
  RelationshipCreate,
  RelationStatus,
  RelationSuggestion,
  RelationsParseResult,
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
  return { draft, relation_suggestions: readSuggestions(result.relation_suggestions), warnings: strList(result.warnings) }
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

export function toEditable(list: RelationSuggestion[]): EditableSuggestion[] {
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
  return s.resolved && s.checked && s.target_id !== null && !!s.edit_self_role.trim() && !!s.edit_target_role.trim()
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
export function suggestionsToBatch(subjectId: number, list: EditableSuggestion[]): RelationshipCreate[] {
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
      source: 'ai_parse',
    }
  })
}

/** 有结果查看组件的任务类型（任务面板据此显示「查看结果」） */
export const ENTITY_TASK_KINDS: readonly TaskKind[] = ['character.generate', 'organization.generate', 'relations.parse']
