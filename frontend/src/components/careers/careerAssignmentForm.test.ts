import { describe, expect, it } from 'vitest'
import { ApiError } from '../../api/client'
import { buildCareerBaselineChanges, buildCareerCreate, buildCareerEventValue, buildCareerRemoval, buildCareerUpdate, isCareerHistoryTimelineRequired, isStaleCareerRevision, type CareerAssignmentDraft } from './careerAssignmentForm'

const atChapter = { anchor_kind: 'chapter', chapter_id: 10, chapter_no: null, boundary: 'start' } as const
const baseline = { exists: true, stage_id: 1, note: '初学' }
const draft: CareerAssignmentDraft = {
  edit_mode: 'progression', stage_id: 3, note: '未来的状态', change_stage: true, change_note: false,
  position: atChapter, history_note: '',
}

describe('职业变更请求', () => {
  it('只发送作者选择的变化，不把当前备注带到较早的章节', () => {
    expect(buildCareerUpdate(draft, baseline, 6)).toEqual({
      ok: true,
      value: { edit_mode: 'progression', stage_id: 3, anchor_kind: 'chapter', chapter_id: 10, boundary: 'start', expected_revision: 6 },
    })
  })

  it('基础修正以真实基线比较，即使输入与最新状态相同也不能遗漏', () => {
    expect(buildCareerUpdate({ ...draft, edit_mode: 'baseline', note: '初学' }, baseline, 6)).toEqual({
      ok: true, value: { edit_mode: 'baseline', stage_id: 3, expected_revision: 6 },
    })
  })

  it('校准初始状态只提交修改字段，首次建立基线才提交完整状态', () => {
    expect(buildCareerBaselineChanges(baseline, true, 1, '新备注', 'main')).toEqual({ note: '新备注' })
    expect(buildCareerBaselineChanges(baseline, false, 1, '', 'main')).toEqual({ exists: false })
    expect(buildCareerBaselineChanges({ exists: false, stage_id: null, note: '' }, true, 3, '', 'main')).toEqual({ exists: true, role: 'main', stage_id: 3, note: '' })
  })

  it('允许清空备注，拒绝无章锚、无变更和缺少版本的请求', () => {
    expect(buildCareerUpdate({ ...draft, change_stage: false, change_note: true, note: '' }, baseline, 6)).toEqual({
      ok: true,
      value: { edit_mode: 'progression', note: '', anchor_kind: 'chapter', chapter_id: 10, boundary: 'start', expected_revision: 6 },
    })
    expect(buildCareerUpdate({ ...draft, position: { ...atChapter, chapter_id: null } }, baseline, 6).ok).toBe(false)
    expect(buildCareerUpdate({ ...draft, change_stage: false }, baseline, 6).ok).toBe(false)
    expect(buildCareerUpdate(draft, baseline, null).ok).toBe(false)
  })

  it('计划章与章末准确传入，且误建删除不会附带章节', () => {
    const planned = { anchor_kind: 'planned', chapter_id: null, chapter_no: 18, boundary: 'end' } as const
    expect(buildCareerRemoval('progression', planned, 7)).toEqual({
      ok: true, value: { edit_mode: 'progression', anchor_kind: 'planned', chapter_no: 18, boundary: 'end', expected_revision: 7 },
    })
    expect(buildCareerRemoval('delete', planned, 7)).toEqual({
      ok: true, value: { edit_mode: 'delete', expected_revision: 7 },
    })
  })

  it('首次挂接要求时间线尚不存在；重挂使用现有时间线版本', () => {
    expect(buildCareerCreate(2, draft, null)).toEqual({
      ok: true, value: { career_id: 2, stage_id: 3, note: '未来的状态', edit_mode: 'progression', anchor_kind: 'chapter', chapter_id: 10, boundary: 'start', expected_revision: 0 },
    })
    expect(buildCareerCreate(2, draft, 9)).toMatchObject({ ok: true, value: { expected_revision: 9 } })
  })

  it('只有版本冲突需要阻止旧版本重试，区间冲突可以继续修改草稿', () => {
    expect(isStaleCareerRevision(new ApiError(409, 'stale', { code: 'stale_revision' }))).toBe(true)
    expect(isStaleCareerRevision(new ApiError(409, '无效区间', { code: 'progression_lifecycle_conflict' }))).toBe(false)
  })

  it('只对历史挂接特殊冲突提供时间线维护入口', () => {
    expect(isCareerHistoryTimelineRequired(new ApiError(409, '已卸下', { code: 'career_history_requires_timeline' }))).toBe(true)
    expect(isCareerHistoryTimelineRequired(new ApiError(409, '超出主职业上限'))).toBe(false)
    expect(isCareerHistoryTimelineRequired(new ApiError(400, '错误', { code: 'career_history_requires_timeline' }))).toBe(false)
  })
})

describe('职业历史事件', () => {
  it('挂接事件包含完整快照，卸下不包含残留状态', () => {
    expect(buildCareerEventValue('presence', true, 3, '瓶颈', 'main')).toEqual({
      exists: true, state: { role: 'main', stage_id: 3, note: '瓶颈' },
    })
    expect(buildCareerEventValue('presence', false, 3, '瓶颈', 'main')).toEqual({ exists: false })
    expect(buildCareerEventValue('stage_id', true, null, '', 'main')).toBeNull()
    expect(buildCareerEventValue('note', true, null, '', 'main')).toBe('')
  })
})
