import { describe, expect, it } from 'vitest'
import type { SettingEffective } from '../../api/types'
import { careerPreviewRequest, careerPreviewResponse, currentCareerPreview, initialCareerPreviewPosition } from './careerPreview'

const chapters = [{ id: 12, chapter_no: 20, title: '晋升' }]
const planned = { anchor_kind: 'planned' as const, chapter_id: null, chapter_no: 20, boundary: 'end' as const }
const linked = { anchor_kind: 'chapter' as const, chapter_id: 12, chapter_no: null, boundary: 'end' as const }

function result(): SettingEffective {
  return {
    timeline_id: 8, position: { chapter_id: null, chapter_no: 20, boundary: 'end' },
    fields: { exists: true, stage_id: 4 }, custom_fields: {}, applied_event_ids: [], orphaned_event_ids: [],
    baseline_status: 'confirmed', timeline_revision: 3,
  }
}

describe('职业按章节预览', () => {
  it('没有已建章节时仍可输入计划章，初始未填写时不发请求', () => {
    const position = initialCareerPreviewPosition([])
    expect(position).toEqual({ anchor_kind: 'planned', chapter_id: null, chapter_no: null, boundary: 'start' })
    expect(careerPreviewRequest('作者', 1, 8, 3, position, [])).toBeNull()
    expect(careerPreviewRequest('作者', 1, 8, 3, planned, [])?.position).toEqual(planned)
  })

  it('同号计划章节仍按数字请求，与已建章节的请求键不同', () => {
    const first = careerPreviewRequest('作者', 1, 8, 3, planned, chapters)!
    const second = careerPreviewRequest('作者', 1, 8, 3, linked, chapters)!
    expect(first.position).toEqual(planned)
    expect(second.position).toEqual(linked)
    expect(first.key).not.toBe(second.key)
    expect(careerPreviewRequest('作者', 1, 8, 3, linked, [])).toBeNull()
  })

  it('版本已变化或服务端返回其他主体、位置时不显示旧结果', () => {
    const request = careerPreviewRequest('作者', 1, 8, 3, planned, chapters)!
    expect(careerPreviewResponse(request, { ...result(), timeline_revision: 4 })).toMatchObject({ data: null, error: '职业时间线已更新，请刷新后查看。' })
    expect(careerPreviewResponse(request, { ...result(), timeline_id: 9 })).toMatchObject({ data: null, error: expect.any(String) })
    expect(careerPreviewResponse(request, { ...result(), position: { chapter_id: 12, chapter_no: 20, boundary: 'end' } })).toMatchObject({ data: null, error: expect.any(String) })
    expect(careerPreviewResponse(request, result()).data).toEqual(result())
  })

  it('切换主体、位置、身份或版本时，即使旧请求晚到也不展示旧结果', () => {
    const old = careerPreviewRequest('作者', 1, 8, 3, planned, chapters)!
    const stale = careerPreviewResponse(old, result())
    for (const next of [
      careerPreviewRequest('作者', 1, 9, 3, planned, chapters),
      careerPreviewRequest('另一账号', 1, 8, 3, planned, chapters),
      careerPreviewRequest('作者', 1, 8, 4, planned, chapters),
      careerPreviewRequest('作者', 1, 8, 3, linked, chapters),
      careerPreviewRequest('作者', 1, 8, 3, { ...planned, boundary: 'start' }, chapters),
    ]) {
      expect(next).not.toBeNull()
      expect(currentCareerPreview(next, stale)).toBeNull()
    }
    expect(currentCareerPreview(null, stale)).toBeNull()
    expect(currentCareerPreview(old, stale)).toEqual(stale)
  })
})
