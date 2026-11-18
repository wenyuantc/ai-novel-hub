import { decodeSceneDraftRecord } from '../scenes/sceneDraftRecord'
import type { SceneDraftRecord } from '../scenes/sceneDraftRecord'

export interface EditorDraftScope {
  actor_id: number
  actor_created_at: string
  project_id: number
  project_created_at: string
}

export interface TitleDraftTarget {
  kind: 'chapter_title'
  chapter_id: number
  anchor_scene_id: number
}

export interface SceneDraftTarget {
  kind: 'scene_document'
  chapter_id: number
  anchor_scene_id: number
  scene_id: number
  scene_created_at: string
}

export type EditorDraftTarget = TitleDraftTarget | SceneDraftTarget

export interface DraftRecordRef {
  draft_id: string
  writer_id: string
  record_seq: number
}

export interface TitleDraftAttempt {
  id: string
  title: string
  expectedTitle: string
  revision: number
  editSeq: number
}

export interface TitleDraftPayload {
  base: { title: string; revision: number }
  value: string
  /** 写请求在途时冻结。重开时任何遗留请求都要先确认服务端结果。 */
  attempt?: TitleDraftAttempt
}

interface EditorDraftMetadata extends DraftRecordRef {
  schema_version: 1
  scope: EditorDraftScope
  edit_seq: number
  saved_at: number
  recovery_hold: boolean
  recovered_from: DraftRecordRef | null
}

export interface TitleDraftRecord extends EditorDraftMetadata {
  target: TitleDraftTarget
  payload: TitleDraftPayload
}

export interface SceneEditorDraftRecord extends EditorDraftMetadata {
  target: SceneDraftTarget
  payload: SceneDraftRecord
}

export type EditorDraftRecord = TitleDraftRecord | SceneEditorDraftRecord

export type EditorDraftEntry =
  | { status: 'ready'; record: EditorDraftRecord }
  | { status: 'unreadable'; ref: DraftRecordRef; target: EditorDraftTarget | null; saved_at: number | null }

export function isTitleDraft(record: EditorDraftRecord): record is TitleDraftRecord {
  return record.target.kind === 'chapter_title'
}

export function draftRecordRef(record: DraftRecordRef): DraftRecordRef {
  return { draft_id: record.draft_id, writer_id: record.writer_id, record_seq: record.record_seq }
}

export function sameEditorDraftScope(left: EditorDraftScope, right: EditorDraftScope): boolean {
  return left.actor_id === right.actor_id && left.actor_created_at === right.actor_created_at
    && left.project_id === right.project_id && left.project_created_at === right.project_created_at
}

export function sameEditorDraftTarget(left: EditorDraftTarget, right: EditorDraftTarget): boolean {
  if (left.chapter_id !== right.chapter_id || left.anchor_scene_id !== right.anchor_scene_id) return false
  if (left.kind === 'chapter_title') return right.kind === 'chapter_title'
  return right.kind === 'scene_document' && left.scene_id === right.scene_id && left.scene_created_at === right.scene_created_at
}

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function integer(value: unknown, minimum: number): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= minimum
}

function identityText(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 128
}

export function decodeDraftScope(value: unknown): EditorDraftScope | null {
  if (!object(value)) return null
  const { actor_id, actor_created_at, project_id, project_created_at } = value
  if (!integer(actor_id, 1) || !identityText(actor_created_at) || !integer(project_id, 1) || !identityText(project_created_at)) return null
  return { actor_id, actor_created_at, project_id, project_created_at }
}

function decodeRef(value: unknown): DraftRecordRef | null {
  if (!object(value)) return null
  const { draft_id, writer_id, record_seq } = value
  if (!identityText(draft_id) || !identityText(writer_id) || !integer(record_seq, 1)) return null
  return { draft_id, writer_id, record_seq }
}

function decodeTarget(value: unknown): EditorDraftTarget | null {
  if (!object(value)) return null
  const { kind, chapter_id, anchor_scene_id, scene_id, scene_created_at } = value
  if (!integer(chapter_id, 1) || !integer(anchor_scene_id, 1)) return null
  if (kind === 'chapter_title') return { kind, chapter_id, anchor_scene_id }
  if (kind === 'scene_document' && scene_id === anchor_scene_id && identityText(scene_created_at)) {
    return { kind, chapter_id, anchor_scene_id, scene_id: anchor_scene_id, scene_created_at }
  }
  return null
}

/** 外层身份字段在所有记录版本中固定，供核验范围、原始导出和精确删除。 */
export function decodeDraftEnvelope(value: unknown) {
  if (!object(value)) return null
  const ref = decodeRef(value)
  const scope = decodeDraftScope(value.scope)
  if (!ref || !scope) return null
  return {
    ref,
    scope,
    target: decodeTarget(value.target),
    saved_at: integer(value.saved_at, 0) ? value.saved_at : null,
    schema_version: value.schema_version,
  }
}

function decodeTitle(value: unknown, editSeq: number): TitleDraftPayload | null {
  if (!object(value) || !object(value.base)) return null
  const { title, revision } = value.base
  if (typeof title !== 'string' || !integer(revision, 0) || typeof value.value !== 'string') return null
  const base = { title, revision }
  if (value.attempt === undefined) return { base, value: value.value }
  if (!object(value.attempt)) return null
  const attempt = value.attempt
  if (!identityText(attempt.id) || typeof attempt.title !== 'string' || attempt.expectedTitle !== title
      || attempt.revision !== revision || !integer(attempt.editSeq, 0) || attempt.editSeq > editSeq) return null
  return {
    base,
    value: value.value,
    attempt: { id: attempt.id, title: attempt.title, expectedTitle: title, revision, editSeq: attempt.editSeq },
  }
}

export function decodeEditorDraftEntry(value: unknown): EditorDraftEntry | null {
  if (!object(value)) return null
  const envelope = decodeDraftEnvelope(value)
  if (!envelope) return null
  const { ref, scope, target, saved_at, schema_version } = envelope
  const unreadable: EditorDraftEntry = { status: 'unreadable', ref, target, saved_at }
  const { edit_seq, recovery_hold } = value
  const recovered_from = value.recovered_from === null ? null : decodeRef(value.recovered_from)
  if (schema_version !== 1 || !target || saved_at === null || !integer(edit_seq, 0)
      || typeof recovery_hold !== 'boolean' || value.recovered_from !== null && !recovered_from
      || recovered_from?.draft_id === ref.draft_id) return unreadable
  const metadata: EditorDraftMetadata = { ...ref, scope, schema_version, edit_seq, recovery_hold, recovered_from, saved_at }
  if (target.kind === 'chapter_title') {
    const payload = decodeTitle(value.payload, edit_seq)
    return payload ? { status: 'ready', record: { ...metadata, target, payload } } : unreadable
  }
  const payload = decodeSceneDraftRecord(value.payload)
  if (!payload || payload.base.id !== target.scene_id || payload.base.chapter_id !== target.chapter_id
      || payload.base.created_at !== target.scene_created_at || payload.editSeq !== edit_seq) return unreadable
  return { status: 'ready', record: { ...metadata, target, payload } }
}
