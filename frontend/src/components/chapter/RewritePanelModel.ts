import { ApiError } from '../../api/client'
import type { RewriteApply, RewriteRequest, SceneRewriteDone } from '../../api/types'

export interface RewritePreviewTarget {
  chapterId: number
  sceneId?: number
  request: RewriteRequest
  original: string
  before?: string
  revisions?: { content_revision: number; planning_revision: number }
  identity: string
}

/** 不把形状正确但来自其他目标、选区或版本的流结果交给采纳按钮。 */
export function rewritePreviewProblem(result: SceneRewriteDone, target: RewritePreviewTarget): string | null {
  if (result.chapter_id !== target.chapterId || (target.sceneId !== undefined && result.scene_id !== target.sceneId)
      || result.start !== target.request.start || result.end !== target.request.end || result.original !== target.original
      || (target.before !== undefined && result.before !== target.before) || result.log_id === null
      || (target.revisions && (result.content_revision !== target.revisions.content_revision
        || result.planning_revision !== target.revisions.planning_revision))) {
    return '改写结果与已选择的目标、文字或版本不一致，请重新选择。'
  }
  return null
}

export function rewriteApplyBody(result: SceneRewriteDone, confirmFormatLoss: boolean): RewriteApply {
  return {
    expected_content_revision: result.content_revision, expected_planning_revision: result.planning_revision,
    start: result.start, end: result.end, original: result.original, before: result.before,
    replacement: result.text, log_id: result.log_id,
    ...(confirmFormatLoss ? { confirm_format_loss: true } : {}),
  }
}

export type RewriteApplyIssue = 'format' | 'unmapped' | 'conflict' | 'uncertain'

/** 格式确认只能处理明确的格式损失，不能当成任意 409 的绕过开关。 */
export function classifyRewriteApplyIssue(error: unknown, confirmed: boolean): RewriteApplyIssue {
  if (error instanceof ApiError && error.status === 409) {
    if (error.body?.code === 'rich_document_confirmation_required' && !confirmed) return 'format'
    if (error.body?.code === 'rich_document_unmapped') return 'unmapped'
    return 'conflict'
  }
  return 'uncertain'
}
