import type { CareerRemovalIntent } from '../../api/careers'
import { ApiError } from '../../api/client'
import type { AssignmentInput, AssignmentUpdate, CareerKind, ProgressionJsonValue, ProgressionPositionInput } from '../../api/types'
import { toPositionInput } from '../progressions/position'

export interface CareerAssignmentDraft {
  edit_mode: 'baseline' | 'progression'
  stage_id: number | null
  note: string
  change_stage: boolean
  change_note: boolean
  position: ProgressionPositionInput
  history_note: string
}

export interface CareerBaseline {
  exists: boolean
  stage_id: number | null
  note: string
}

/** 基础表单基于打开时的实际快照，避免发送未编辑字段。 */
export function buildCareerBaselineChanges(
  original: CareerBaseline,
  exists: boolean,
  stageId: number | null,
  note: string,
  role: CareerKind,
): Record<string, ProgressionJsonValue> {
  if (!exists) return original.exists ? { exists: false } : {}
  if (!original.exists) return { exists: true, role, stage_id: stageId, note: note.trim() }
  const changes: Record<string, ProgressionJsonValue> = {}
  if (stageId !== original.stage_id) changes.stage_id = stageId
  if (note.trim() !== original.note) changes.note = note.trim()
  return changes
}

type Result<T> = { ok: true; value: T } | { ok: false; error: string }

function positionQuery(position: ProgressionPositionInput): Pick<CareerRemovalIntent, 'anchor_kind' | 'chapter_id' | 'chapter_no' | 'scene_id' | 'boundary'> | null {
  const valid = toPositionInput(position)
  if (!valid) return null
  if (valid.anchor_kind === 'scene') return { anchor_kind: 'scene', scene_id: valid.scene_id!, boundary: valid.boundary }
  return valid.anchor_kind === 'chapter'
    ? { anchor_kind: 'chapter', chapter_id: valid.chapter_id!, boundary: valid.boundary }
    : { anchor_kind: 'planned', chapter_no: valid.chapter_no!, boundary: valid.boundary }
}

export function buildCareerUpdate(
  draft: CareerAssignmentDraft,
  baseline: CareerBaseline,
  revision: number | null,
): Result<AssignmentUpdate> {
  if (revision === null) return { ok: false, error: '时间线尚未加载，请刷新后重试' }
  if (draft.note.length > 200 || draft.history_note.length > 200) return { ok: false, error: '备注不能超过 200 字' }
  const result: AssignmentUpdate = { edit_mode: draft.edit_mode, expected_revision: revision }
  if (draft.edit_mode === 'progression') {
    const position = positionQuery(draft.position)
    if (!position) return { ok: false, error: '请选择有效的生效章节' }
    Object.assign(result, position)
    if (draft.change_stage) {
      if (draft.stage_id === null) return { ok: false, error: '请选择阶段' }
      result.stage_id = draft.stage_id
    }
    if (draft.change_note) result.note = draft.note.trim()
  } else {
    if (!baseline.exists || draft.stage_id !== baseline.stage_id) {
      if (draft.stage_id === null) return { ok: false, error: '请选择初始阶段' }
      result.stage_id = draft.stage_id
    }
    if (!baseline.exists || draft.note.trim() !== baseline.note) result.note = draft.note.trim()
  }
  if (result.stage_id === undefined && result.note === undefined) return { ok: false, error: '请选择要记录的变化' }
  if (draft.history_note.trim()) result.history_note = draft.history_note.trim()
  return { ok: true, value: result }
}

export function buildCareerCreate(careerId: number, draft: CareerAssignmentDraft, revision: number | null): Result<AssignmentInput> {
  if (draft.stage_id === null) return { ok: false, error: '请选择阶段' }
  if (draft.note.length > 200 || draft.history_note.length > 200) return { ok: false, error: '备注不能超过 200 字' }
  const position = draft.edit_mode === 'progression' ? positionQuery(draft.position) : {}
  if (!position) return { ok: false, error: '请选择有效的生效章节' }
  const result: AssignmentInput = {
    career_id: careerId,
    stage_id: draft.stage_id,
    note: draft.note.trim(),
    edit_mode: draft.edit_mode,
    ...position,
    expected_revision: revision ?? 0,
  }
  if (draft.history_note.trim()) result.history_note = draft.history_note.trim()
  return { ok: true, value: result }
}

export function buildCareerRemoval(
  mode: CareerRemovalIntent['edit_mode'],
  position: ProgressionPositionInput,
  revision: number | null,
): Result<CareerRemovalIntent> {
  if (revision === null) return { ok: false, error: '时间线尚未加载，请刷新后重试' }
  const intent: CareerRemovalIntent = { edit_mode: mode, expected_revision: revision }
  if (mode === 'progression') {
    const query = positionQuery(position)
    if (!query) return { ok: false, error: '请选择有效的生效章节' }
    Object.assign(intent, query)
  }
  return { ok: true, value: intent }
}

export function isStaleCareerRevision(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409 && error.body?.code === 'stale_revision'
}

export function isCareerHistoryTimelineRequired(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409 && error.body?.code === 'career_history_requires_timeline'
}

export function buildCareerEventValue(
  field: 'presence' | 'stage_id' | 'note',
  exists: boolean,
  stageId: number | null,
  note: string,
  role: CareerKind,
): ProgressionJsonValue {
  if (field === 'note') return note.trim()
  if (field === 'presence' && !exists) return { exists: false }
  if (stageId === null) return null
  return field === 'stage_id' ? stageId : { exists: true, state: { role, stage_id: stageId, note: note.trim() } }
}
