import type { RichDocument, RichDocumentOut, RichDocumentSave, RichFormatting, RichMarker, RichSection } from '../../api/richDocument'
import type { Scene } from '../../api/types'
import { stripSettingWhitespace } from '../settingTags/tagDraft'
import { validSectionDocument } from './richSectionValidity'

export interface SceneDraft { title: string; content: string; summary: string }

export interface RichEditEvidence {
  mappedMarkerKeys: string[]
  explicitMarkerIntent: boolean
  restoredMarkerKeys?: string[]
  mappedSectionKeys?: string[]
  explicitSectionIntent?: boolean
  restoredSectionKeys?: string[]
}

export type SceneEditEvidence = Required<RichEditEvidence>
export type SceneDraftBase = Pick<Scene,
  'id' | 'chapter_id' | 'created_at' | 'title' | 'summary' | 'content' | 'content_revision' | 'planning_revision'>

export interface SceneSaveAttempt {
  id: number
  editSeq: number
  baseRevision: number
  kind: 'document' | 'metadata' | 'plain'
  draft: SceneDraft
  payload: RichDocumentSave | null
  evidence: SceneEditEvidence
}

/** 正文保存进行中时 evidence 是提交后继续编辑的证据，已发送证据留在 attempt。 */
export interface SceneDraftRecord {
  base: SceneDraftBase
  document: RichDocumentOut | null
  draft: SceneDraft
  documentDraft: RichDocument | null
  evidence: SceneEditEvidence
  retiredMarkerKeys: string[]
  attempt: SceneSaveAttempt | null
  editSeq: number
  bodyEditSeq: number
}

// 场景标题和章节标题均由 Python str.strip() 清理。
export const trimSceneTitle = stripSettingWhitespace

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)
const revision = (value: unknown): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
const positive = (value: unknown): value is number => revision(value) && value > 0
const key = (value: unknown): value is string =>
  typeof value === 'string' && /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(value)
const keys = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every(key) && new Set(value).size === value.length
const offset = (value: unknown): value is number => revision(value) && value <= 2_147_483_647
const nullableOffset = (value: unknown): value is number | null => value === null || offset(value)
const draft = (value: unknown): value is SceneDraft =>
  record(value) && typeof value.title === 'string' && typeof value.content === 'string' && typeof value.summary === 'string'

function sceneBase(value: unknown): value is SceneDraftBase {
  return record(value) && draft(value) && positive(value.id) && positive(value.chapter_id)
    && typeof value.created_at === 'string' && value.created_at.length > 0
    && revision(value.content_revision) && revision(value.planning_revision)
}

function formatting(value: unknown): value is RichFormatting {
  return record(value) && value.version === 1 && Array.isArray(value.boundaries) && value.boundaries.every(offset)
    && Array.isArray(value.inline) && value.inline.every((item: unknown) => record(item)
      && (item.kind === 'bold' || item.kind === 'italic') && offset(item.start) && offset(item.end))
    && Array.isArray(value.blocks) && value.blocks.every((item: unknown) => record(item) && offset(item.start) && offset(item.end)
      && (item.kind === 'heading' ? positive(item.level) && item.level <= 6
        : (item.kind === 'bullet_list' || item.kind === 'ordered_list' || item.kind === 'blockquote') && item.level === null))
}

function section(value: unknown): value is RichSection {
  return record(value) && key(value.key) && offset(value.anchor) && offset(value.order)
    && typeof value.title === 'string' && typeof value.content === 'string' && typeof value.include_in_ai === 'boolean'
    && formatting(value.formatting)
}

function marker(value: unknown): value is RichMarker {
  if (!record(value) || !key(value.key) || (value.section_key !== null && !key(value.section_key))
    || (value.status !== 'active' && value.status !== 'stale') || !nullableOffset(value.start)
    || !nullableOffset(value.end) || !nullableOffset(value.last_start) || typeof value.excerpt !== 'string'
    || typeof value.excerpt_truncated !== 'boolean'
    || typeof value.color !== 'string' || !['yellow', 'green', 'blue', 'purple', 'red'].includes(value.color)) return false

  if (value.draft_excerpt_truncated !== undefined && typeof value.draft_excerpt_truncated !== 'boolean') return false
  if (value.draft_excerpt == null) return !value.draft_excerpt_truncated && value.draft_last_start == null
  return typeof value.draft_excerpt === 'string' && value.draft_excerpt.length > 0 && value.status === 'stale'
    && offset(value.draft_last_start)
}

function richDocument(value: unknown): value is RichDocument {
  if (!record(value) || typeof value.content !== 'string' || !formatting(value.formatting)
    || !Array.isArray(value.sections) || !value.sections.every(section) || !keys(value.retired_section_keys)
    || !Array.isArray(value.markers) || !value.markers.every(marker)) return false
  return validSectionDocument({
    content: value.content, formatting: value.formatting, sections: value.sections,
    retired_section_keys: value.retired_section_keys, markers: value.markers,
  })
}

function documentOut(value: unknown): value is RichDocumentOut {
  return record(value) && revision(value.content_revision) && revision(value.planning_revision) && richDocument(value.document)
}

function editEvidence(value: unknown, target: RichDocument | null): value is SceneEditEvidence {
  if (!record(value) || typeof value.explicitMarkerIntent !== 'boolean' || typeof value.explicitSectionIntent !== 'boolean'
    || !keys(value.mappedMarkerKeys) || !keys(value.mappedSectionKeys)
    || !keys(value.restoredMarkerKeys) || !keys(value.restoredSectionKeys)) return false
  if (value.restoredMarkerKeys.length && !value.explicitMarkerIntent
    || value.restoredSectionKeys.length && !value.explicitSectionIntent) return false
  const markers = new Set(target?.markers.map((item) => item.key))
  const sections = new Set(target?.sections.map((item) => item.key))
  return [...value.mappedMarkerKeys, ...value.restoredMarkerKeys].every((item) => markers.has(item))
    && [...value.mappedSectionKeys, ...value.restoredSectionKeys].every((item) => sections.has(item))
}

function documentSave(value: unknown): value is RichDocumentSave {
  return record(value) && revision(value.expected_content_revision) && richDocument(value.document)
    && keys(value.add_marker_keys) && keys(value.delete_marker_keys) && keys(value.restore_marker_keys)
    && keys(value.add_section_keys) && keys(value.delete_section_keys) && keys(value.restore_section_keys)
    && keys(value.mapped_marker_keys) && keys(value.mapped_section_keys)
}

function saveAttempt(value: unknown, base: SceneDraftBase, document: RichDocumentOut | null): value is SceneSaveAttempt {
  if (!record(value) || !positive(value.id) || !revision(value.editSeq) || !draft(value.draft)
    || value.baseRevision !== base.content_revision) return false
  if (value.kind === 'document') {
    return !!document && documentSave(value.payload) && value.payload.expected_content_revision === base.content_revision
      && value.draft.content === value.payload.document.content && value.draft.title === base.title
      && value.draft.summary === base.summary && editEvidence(value.evidence, value.payload.document)
  }
  if (value.kind !== 'metadata' && value.kind !== 'plain' || value.payload !== null) return false
  return editEvidence(value.evidence, null) && (value.kind === 'metadata'
    ? value.draft.content === base.content && (value.draft.title !== base.title || value.draft.summary !== base.summary)
    : document === null && value.draft.content !== base.content)
}

/** 严格读取 IndexedDB 结构；超出服务器容量的有效本地输入仍可恢复。 */
export function decodeSceneDraftRecord(value: unknown): SceneDraftRecord | null {
  if (!record(value) || !sceneBase(value.base) || !draft(value.draft)
    || !revision(value.editSeq) || !revision(value.bodyEditSeq) || value.bodyEditSeq > value.editSeq
    || !keys(value.retiredMarkerKeys)) return null
  let document: RichDocumentOut | null = null
  let documentDraft: RichDocument | null = null
  if (value.document !== null || value.documentDraft !== null) {
    if (!documentOut(value.document) || !richDocument(value.documentDraft)
      || value.document.content_revision !== value.base.content_revision || value.document.planning_revision !== value.base.planning_revision
      || value.document.document.content !== value.base.content || value.documentDraft.content !== value.draft.content) return null
    const live = new Set(value.document.document.markers.map((item) => item.key))
    if (value.retiredMarkerKeys.some((item) => live.has(item))) return null
    document = value.document
    documentDraft = value.documentDraft
  } else if (value.retiredMarkerKeys.length) return null
  if (!editEvidence(value.evidence, documentDraft)) return null
  let attempt: SceneSaveAttempt | null = null
  if (value.attempt !== null) {
    if (!saveAttempt(value.attempt, value.base, document) || value.attempt.editSeq > value.editSeq) return null
    attempt = value.attempt
  }
  return {
    base: value.base, document, draft: value.draft, documentDraft, evidence: value.evidence,
    retiredMarkerKeys: value.retiredMarkerKeys, attempt, editSeq: value.editSeq, bodyEditSeq: value.bodyEditSeq,
  }
}
