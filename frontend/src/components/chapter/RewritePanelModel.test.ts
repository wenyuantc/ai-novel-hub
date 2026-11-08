import { describe, expect, it } from 'vitest'
import { ApiError } from '../../api/client'
import type { SceneRewriteDone } from '../../api/types'
import { classifyRewriteApplyIssue, rewriteApplyBody, rewritePreviewProblem, type RewritePreviewTarget } from './RewritePanelModel'

const target: RewritePreviewTarget = { chapterId: 7, sceneId: 9, identity: 'actor',
  request: { op: 'rewrite', start: 1, end: 3 }, original: '甲🙂', before: '前',
  revisions: { content_revision: 3, planning_revision: 4 } }
const result: SceneRewriteDone = { chapter_id: 7, scene_id: 9, start: 1, end: 3, original: '甲🙂', before: '前',
  text: '新', log_id: 11, content_revision: 3, planning_revision: 4 }

describe('正文选区改写预览和格式确认', () => {
  it('验证目标、选区原文、前文、版本和日志身份', () => {
    expect(rewritePreviewProblem(result, target)).toBeNull()
    for (const other of [{ chapter_id: 8 }, { scene_id: 8 }, { start: 0 }, { end: 4 }, { original: '乙🙂' },
      { before: '旧' }, { log_id: null }, { content_revision: 2 }, { planning_revision: 3 }]) {
      expect(rewritePreviewProblem({ ...result, ...other }, target)).toBeTruthy()
    }
  })

  it('采纳时固定预览版本，仅明确确认后发送严格 true', () => {
    const ordinary = rewriteApplyBody(result, false)
    expect(ordinary).toEqual({ start: 1, end: 3, original: '甲🙂', before: '前', replacement: '新', log_id: 11,
      expected_content_revision: 3, expected_planning_revision: 4 })
    expect(rewriteApplyBody(result, true)).toEqual({ ...ordinary, confirm_format_loss: true })
  })

  it('格式损失、不能映射、版本冲突与网络不确定分别处理', () => {
    const error = (code: string) => new ApiError(409, '冲突', { code })
    expect(classifyRewriteApplyIssue(error('rich_document_confirmation_required'), false)).toBe('format')
    expect(classifyRewriteApplyIssue(error('rich_document_confirmation_required'), true)).toBe('conflict')
    expect(classifyRewriteApplyIssue(error('rich_document_unmapped'), true)).toBe('unmapped')
    expect(classifyRewriteApplyIssue(error('scene_context_changed'), false)).toBe('conflict')
    expect(classifyRewriteApplyIssue(new ApiError(0, '网络中断'), true)).toBe('uncertain')
  })
})
