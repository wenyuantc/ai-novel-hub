import type { ProgressionPositionInput, SettingEffective } from '../../api/types'
import { chapterOptions, toPositionInput, type ChapterReference } from '../progressions/position'

export interface CareerPreviewRequest {
  key: string
  timelineId: number
  revision: number
  position: ProgressionPositionInput
}

export interface CareerPreviewResult {
  key: string
  data: SettingEffective | null
  error: string | null
}

export function initialCareerPreviewPosition(chapters: readonly ChapterReference[]): ProgressionPositionInput {
  const first = chapterOptions(chapters)[0]?.value ?? null
  return { anchor_kind: first === null ? 'planned' : 'chapter', chapter_id: first, chapter_no: null, boundary: 'start' }
}

/** 锚点类型、位置及身份都进入请求键；同号计划章不转为已建章。 */
export function careerPreviewRequest(
  identity: string,
  projectId: number,
  timelineId: number,
  revision: number,
  position: ProgressionPositionInput,
  chapters: readonly ChapterReference[],
): CareerPreviewRequest | null {
  const chapterId = position.anchor_kind === 'chapter' && chapters.some((item) => item.id === position.chapter_id)
    ? position.chapter_id : null
  const selected = toPositionInput({ ...position, chapter_id: chapterId })
  if (!selected) return null
  return {
    key: JSON.stringify([identity, projectId, timelineId, revision, selected.anchor_kind, selected.scene_id ?? selected.chapter_id ?? selected.chapter_no, selected.boundary]),
    timelineId,
    revision,
    position: selected,
  }
}

export function careerPreviewResponse(request: CareerPreviewRequest, data: SettingEffective): CareerPreviewResult {
  const samePosition = data.position.boundary === request.position.boundary && (
    request.position.anchor_kind === 'scene' ? data.position.scene_id === request.position.scene_id : request.position.anchor_kind === 'chapter'
      ? data.position.chapter_id === request.position.chapter_id
      : data.position.chapter_id === null && data.position.chapter_no === request.position.chapter_no
  )
  if (data.timeline_id !== request.timelineId || !samePosition) {
    return { key: request.key, data: null, error: '返回的职业或章节位置不一致，请刷新后重试。' }
  }
  return data.timeline_revision === request.revision
    ? { key: request.key, data, error: null }
    : { key: request.key, data: null, error: '职业时间线已更新，请刷新后查看。' }
}

export function currentCareerPreview(request: CareerPreviewRequest | null, result: CareerPreviewResult | null) {
  return request && result?.key === request.key ? result : null
}
