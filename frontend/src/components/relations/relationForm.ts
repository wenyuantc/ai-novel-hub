import { RELATION_STATUS } from '../../api/relationships'
import type {
  ProgressionPositionInput,
  ProgressionSubject,
  RelationshipUpdate,
  RelationStatus,
} from '../../api/types'
import { toPositionInput } from '../progressions/position'

const MAX_ID = 2_147_483_647

export interface RelationValues {
  a_id: number
  b_id: number
  type_id: number
  intimacy: number
  status: RelationStatus
  description: string
}

export type RelationSnapshot = { exists: false } | ({ exists: true } & RelationValues)

export type RelationTemporalUpdate = RelationshipUpdate & {
  edit_mode: 'baseline' | 'progression'
  expected_revision: number
  chapter_id?: number | null
  boundary?: 'start' | 'end'
}

interface EditContext {
  mode: 'baseline' | 'progression'
  revision: number
  position: ProgressionPositionInput | null
  note: string
}

type UpdateResult = { ok: true; body: RelationTemporalUpdate | null } | { ok: false; error: string }

function positiveId(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0 && value <= MAX_ID
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function validStatus(value: unknown): value is RelationStatus {
  return typeof value === 'string' && Object.hasOwn(RELATION_STATUS, value)
}

function samePair(a: number, b: number, otherA: number, otherB: number): boolean {
  return a !== b && ((a === otherA && b === otherB) || (a === otherB && b === otherA))
}

/** 时间线身份固定为无方向人物对；关系类型中的方向另行表达。 */
export function relationSubject(a: number, b: number): Extract<ProgressionSubject, { subject_kind: 'relationship' }> {
  if (!positiveId(a) || !positiveId(b) || a === b) throw new Error('关系角色不合法')
  return { subject_kind: 'relationship', character_id: Math.min(a, b), related_character_id: Math.max(a, b) }
}

/** 服务端时间线快照只读投影；缺字段时明确报错，不用最新关系回填。 */
export function readRelationSnapshot(fields: Record<string, unknown>, pair: readonly [number, number]): RelationSnapshot {
  if (fields.exists === false) return { exists: false }
  if (fields.exists !== true || !record(fields.identity)) throw new Error('关系设定不完整，请刷新后重试')
  const { a_id, b_id, type_id } = fields.identity
  if (!positiveId(a_id) || !positiveId(b_id) || !positiveId(type_id) || !samePair(a_id, b_id, pair[0], pair[1])) {
    throw new Error('关系身份与当前角色不一致，请刷新后重试')
  }
  if (typeof fields.intimacy !== 'number' || !Number.isInteger(fields.intimacy) || fields.intimacy < -100 || fields.intimacy > 100
    || !validStatus(fields.status) || typeof fields.description !== 'string') {
    throw new Error('关系设定内容不完整，请刷新后重试')
  }
  return { exists: true, a_id, b_id, type_id, intimacy: fields.intimacy, status: fields.status, description: fields.description }
}

/** 对选定的基础／章节快照计算真实变更；不向早章提交未经作者修改的未来字段。 */
export function buildRelationUpdate(before: RelationSnapshot, values: RelationValues, context: EditContext): UpdateResult {
  if (!before.exists) return { ok: false, error: '此时关系尚未建立，请先记录关系建立变化' }
  if (!positiveId(context.revision)) return { ok: false, error: '设定版本无效，请刷新后重试' }
  if (!positiveId(values.a_id) || !positiveId(values.b_id) || !positiveId(values.type_id)
    || !samePair(values.a_id, values.b_id, before.a_id, before.b_id)) {
    return { ok: false, error: '关系角色不能变为其他人物' }
  }
  if (!Number.isInteger(values.intimacy) || values.intimacy < -100 || values.intimacy > 100
    || !validStatus(values.status) || typeof values.description !== 'string') {
    return { ok: false, error: '关系内容不合法，请检查后重试' }
  }
  const note = context.note.trim()
  if (Array.from(note).length > 200) return { ok: false, error: '变更备注不能超过 200 字' }
  const body: RelationTemporalUpdate = { edit_mode: context.mode, expected_revision: context.revision }
  if (context.mode === 'progression') {
    const pos = context.position && toPositionInput(context.position)
    if (!pos) return { ok: false, error: '请选择章节或输入有效的计划章节号' }
    body.anchor_kind = pos.anchor_kind
    if (pos.anchor_kind === 'scene') body.scene_id = pos.scene_id
    body.chapter_id = pos.chapter_id
    body.chapter_no = pos.chapter_no
    body.boundary = pos.boundary
  }
  let changed = false
  if (values.a_id !== before.a_id || values.b_id !== before.b_id || values.type_id !== before.type_id) {
    body.a_id = values.a_id
    body.type_id = values.type_id
    changed = true
  }
  if (values.intimacy !== before.intimacy) { body.intimacy = values.intimacy; changed = true }
  if (values.status !== before.status) { body.status = values.status; changed = true }
  if (values.description !== before.description) { body.description = values.description; changed = true }
  if (!changed) return { ok: true, body: null }
  if (note) body.note = note
  return { ok: true, body }
}

export type RelationEventField = 'identity' | 'intimacy' | 'status' | 'description' | 'presence'
export interface RelationEventDraft {
  field: RelationEventField
  presence: boolean
  values: RelationValues
  position: ProgressionPositionInput
  operation: 'replace' | 'append'
  sortOrder: number
  note: string
}

/** 构造关系事件；身份和建立状态均作为不可拆分的结构保存。 */
export function buildRelationEvent(draft: RelationEventDraft, pair: readonly [number, number], revision: number):
  | { ok: true; value: import('../../api/types').SettingProgressionInput }
  | { ok: false; error: string } {
  const position = toPositionInput(draft.position)
  if (!position) return { ok: false, error: '请选择有效的生效章节' }
  if (!positiveId(revision)) return { ok: false, error: '设定版本无效，请刷新后重试' }
  if (!Number.isSafeInteger(draft.sortOrder) || draft.sortOrder < 0 || draft.sortOrder > 2_147_483_647) {
    return { ok: false, error: '排序必须为非负整数' }
  }
  if (Array.from(draft.note.trim()).length > 200) return { ok: false, error: '变更备注不能超过 200 字' }
  if (draft.operation === 'append' && draft.field !== 'description') return { ok: false, error: '只有描述可以追加' }
  const v = draft.values
  const identity = { a_id: v.a_id, b_id: v.b_id, type_id: v.type_id }
  if ((draft.field === 'identity' || (draft.field === 'presence' && draft.presence))
    && (!positiveId(v.a_id) || !positiveId(v.b_id) || !positiveId(v.type_id) || !samePair(v.a_id, v.b_id, pair[0], pair[1]))) {
    return { ok: false, error: '请选择属于这两位角色的关系类型和方向' }
  }
  if ((draft.field === 'intimacy' || (draft.field === 'presence' && draft.presence))
    && (!Number.isInteger(v.intimacy) || v.intimacy < -100 || v.intimacy > 100)) return { ok: false, error: '亲密度必须在 -100 至 100 之间' }
  if ((draft.field === 'status' || (draft.field === 'presence' && draft.presence)) && !validStatus(v.status)) return { ok: false, error: '请选择关系状态' }
  if ((draft.field === 'description' || (draft.field === 'presence' && draft.presence)) && typeof v.description !== 'string') return { ok: false, error: '关系描述不合法' }
  if (draft.field === 'description' && draft.operation === 'append' && !v.description.trim()) return { ok: false, error: '请输入要追加的描述' }
  const state = { identity, intimacy: v.intimacy, status: v.status, description: v.description }
  let value: import('../../api/types').ProgressionJsonValue
  if (draft.field === 'presence') value = draft.presence ? { exists: true, state } : { exists: false }
  else if (draft.field === 'identity') value = identity
  else value = v[draft.field]
  return { ok: true, value: { ...position, expected_revision: revision, field_key: draft.field,
    custom_field_id: null, operation: draft.operation, value, sort_order: draft.sortOrder, note: draft.note.trim() } }
}

/** 关系弹窗关闭、切换主体或切换身份后，旧响应不得回写。 */
export function createRelationRequestScope() {
  let version = 0
  return {
    capture: () => version,
    invalidate: () => { version += 1 },
    isCurrent: (ticket: number, allowed: boolean) => allowed && ticket === version,
  }
}
