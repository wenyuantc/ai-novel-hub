import { request } from './client'
import { runGeneration } from './sse'
import { parseSceneRewriteDone } from './scenes'
import type { SceneRewriteDone, SkillInputValues, StyleChoice, Task } from './types'

export interface RichInlineStyle { kind: 'bold' | 'italic'; start: number; end: number }
export interface RichBlockStyle { kind: 'heading' | 'bullet_list' | 'ordered_list' | 'blockquote'; start: number; end: number; level: number | null }
export interface RichFormatting { version: 1; boundaries: number[]; inline: RichInlineStyle[]; blocks: RichBlockStyle[] }
export interface RichSection { key: string; anchor: number; order: number; title: string; content: string; formatting: RichFormatting; include_in_ai: boolean }
export type MarkerColor = 'yellow' | 'green' | 'blue' | 'purple' | 'red'
export interface RichMarker {
  key: string; section_key: string | null; status: 'active' | 'stale'; start: number | null; end: number | null
  color: MarkerColor; excerpt: string; excerpt_truncated: boolean; last_start: number | null
  /** 作者未保存事务捕获的片段，服务端无法独立重放校验；旧数据可缺省。 */
  draft_excerpt?: string | null; draft_excerpt_truncated?: boolean; draft_last_start?: number | null
}
export interface RichDocument { content: string; formatting: RichFormatting; sections: RichSection[]; retired_section_keys: string[]; markers: RichMarker[] }
export interface RichDocumentOut { content_revision: number; planning_revision: number; document: RichDocument }
export interface RichDocumentSave {
  expected_content_revision: number; document: RichDocument
  add_section_keys: string[]; delete_section_keys: string[]; add_marker_keys: string[]; delete_marker_keys: string[]
  restore_section_keys?: string[]; restore_marker_keys?: string[]
  mapped_section_keys: string[]; mapped_marker_keys: string[]
}
export interface MarkerTimelineItem extends Omit<RichMarker, 'status'> {
  status: 'active' | 'stale'; chapter_id: number; chapter_no: number; chapter_title: string
  scene_id: number; scene_title: string; scene_active: boolean; section_title: string | null; content_revision: number
}
export interface MarkerTimelinePage { items: MarkerTimelineItem[]; total: number; offset: number; limit: number; planning_revision: number; snapshot: string }
export interface MarkerRelink {
  expected_source_revision: number; target_chapter_id: number; target_scene_id: number; expected_target_revision: number
  target_section_key?: string | null; start: number; end: number; selected_hash: string
}
export interface MarkerRelinkOut { source_chapter_id: number; source_revision: number; target_chapter_id: number; target_revision: number; target_scene_id: number; key: string }
export interface SectionRewriteRequest {
  expected_content_revision: number; expected_planning_revision: number; request_id: string; confirmed: true
  op: 'rewrite' | 'expand' | 'condense' | 'deslop'; start: number; end: number; selected_hash: string
  instruction?: string; ai_config_id?: number | null; style?: StyleChoice; skill_id?: number | null; skill_inputs?: SkillInputValues
}
export interface SectionRewriteDone extends SceneRewriteDone { section_key: string; request_id: string; log_id: number }
export interface SectionRewriteApply {
  expected_content_revision: number; expected_planning_revision: number; request_id: string; log_id: number
  start: number; end: number; original: string; replacement: string; confirm_format_loss?: boolean
}
export interface RichReviewScene {
  scene_id: number; original: RichDocument; candidate: string; proposed_formatting: RichFormatting
  proposed_markers: RichMarker[]; proposed_sections: { key: string; anchor: number | null; order: number }[]
  unresolved_sections: string[]; format_loss: boolean
}
export interface RichReviewPreview {
  reason: 'rich_document_review'; status: 'pending' | 'applied' | 'discarded' | 'stale'; task_status: string
  task_key: string; run_key: string; token: string; chapter_id: number; content_revision: number; scenes: RichReviewScene[]
}
export interface RichReviewPosition { scene_id: number; section_key: string; anchor: number }
export interface RichReviewOutcome { status: 'applied'; task_status: 'succeeded' | 'queued'; chapter_id: number; content_revision: number }

const sceneBase = (pid: number, cid: number, sid: number) => `/projects/${pid}/chapters/${cid}/scenes/${sid}`
const sectionBase = (pid: number, cid: number, sid: number, key: string) => `${sceneBase(pid, cid, sid)}/sections/${encodeURIComponent(key)}/rewrite`

/** 所有正文、格式与作者资料共享一个章内容版本；写冲突不重放。 */
export const richDocumentApi = {
  get: (pid: number, cid: number, sid: number, signal?: AbortSignal) => request<RichDocumentOut>(`${sceneBase(pid, cid, sid)}/document`, { signal }),
  save: (pid: number, cid: number, sid: number, body: RichDocumentSave, signal?: AbortSignal) => request<RichDocumentOut>(`${sceneBase(pid, cid, sid)}/document`, { method: 'PUT', body, signal }),
  convert: (pid: number, cid: number, sid: number, key: string, revision: number) => request<RichDocumentOut>(`${sceneBase(pid, cid, sid)}/document/sections/${encodeURIComponent(key)}/convert`, { method: 'POST', body: { expected_content_revision: revision } }),
  timeline: (pid: number, query: { offset?: number; limit?: number; snapshot?: string } = {}, signal?: AbortSignal) => request<MarkerTimelinePage>(`/projects/${pid}/markers`, { query, signal }),
  relinkMarker: (pid: number, item: MarkerTimelineItem, body: MarkerRelink) => request<MarkerRelinkOut>(`/projects/${pid}/markers/${item.scene_id}/${encodeURIComponent(item.key)}/relink`, { method: 'POST', body }),
  deleteMarker: (pid: number, item: MarkerTimelineItem) => request<{ chapter_id: number; content_revision: number }>(`/projects/${pid}/markers/${item.scene_id}/${encodeURIComponent(item.key)}`, { method: 'DELETE', query: { expected_content_revision: item.content_revision } }),
  rewriteSelection: (pid: number, cid: number, sid: number, key: string, body: SectionRewriteRequest, expectedOriginal: string, onDelta: (text: string) => void, signal?: AbortSignal) =>
    runGeneration<SectionRewriteDone>(sectionBase(pid, cid, sid, key), body, { onDelta, signal, parseDone: (value) => parseSectionRewriteDone(value, { cid, sid, key, body, expectedOriginal }) }),
  applySelection: (pid: number, cid: number, sid: number, key: string, body: SectionRewriteApply) => request<RichDocumentOut>(`${sectionBase(pid, cid, sid, key)}/apply`, { method: 'POST', body }),
  review: async (task: Task, signal?: AbortSignal) => {
    const identity = task.rich_review
    if (!identity) throw new Error('任务缺少待核对提案身份')
    const value = await request<RichReviewPreview>(`/tasks/${task.id}/rich-proposals/${encodeURIComponent(identity.token)}`, {
      query: { task_key: identity.task_key, run_key: identity.run_key }, signal,
    })
    if (value.reason !== 'rich_document_review' || value.task_key !== identity.task_key || value.run_key !== identity.run_key
        || value.token !== identity.token || value.chapter_id !== identity.chapter_id || !Array.isArray(value.scenes)) {
      throw new Error('提案身份不一致，请刷新任务后重新核对')
    }
    return value
  },
  acceptReview: (task: Task, sections: RichReviewPosition[], confirmFormatLoss: boolean, signal?: AbortSignal) => {
    const identity = task.rich_review
    if (!identity) throw new Error('任务缺少待核对提案身份')
    return request<RichReviewOutcome>(`/tasks/${task.id}/rich-proposals/${encodeURIComponent(identity.token)}/accept`, {
      method: 'POST', body: { task_key: identity.task_key, run_key: identity.run_key, sections, confirm_format_loss: confirmFormatLoss }, signal,
    })
  },
}

/** 响应必须绑定同一区块、选区、操作确认和原章版本，不能把晚到结果用于新选择。 */
export function parseSectionRewriteDone(value: unknown, expected: { cid: number; sid: number; key: string; body: SectionRewriteRequest; expectedOriginal: string }): SectionRewriteDone {
  const base = parseSceneRewriteDone(value)
  const raw = value as Record<string, unknown>
  if (raw.section_key !== expected.key || raw.request_id !== expected.body.request_id || base.log_id === null
      || base.chapter_id !== expected.cid || base.scene_id !== expected.sid || base.start !== expected.body.start
      || base.end !== expected.body.end || base.original !== expected.expectedOriginal
      || base.content_revision !== expected.body.expected_content_revision
      || base.planning_revision !== expected.body.expected_planning_revision || base.before !== '') {
    throw new Error('区块改写结果与已确认的目标或版本不一致，请重新选择')
  }
  return { ...base, section_key: expected.key, request_id: expected.body.request_id, log_id: base.log_id }
}
