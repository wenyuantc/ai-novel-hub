import { describe, expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { batchPayload, emptyBatchDraft, planningReferenceKey, planningRowLabel, planningSourceKey, readPlanningConflict, settingCellDescription } from './planning'
import type { PlanningRow } from '../api/types'

const row: PlanningRow = { key: 'plan:4', unit_id: null, source: { kind: 'plan', id: 4 }, chapter_id: null, plan_id: 4, title: '雨夜来客', chapter_no: null, outline_id: 2, volume_no: 1, volume_title: '启程', seq: 3, scene_id: null, scene_sort: null, content_revision: null, scene_planning: null, source_level: 'chapter', source_chapter_id: null, has_content: false, pov_character_id: null, pov_character_name: null, tags: [], references: [] }
describe('规划交互契约', () => {
  it('批量替换一种设定不清除其他类型，关系传时间线而非载体id', () => {
    const draft = emptyBatchDraft()
    draft.references.character = { op: 'replace', ids: [3, 7] }
    draft.references.relationship = { op: 'add', ids: [19] }
    const payload = batchPayload(8, [row.source], draft)
    expect(payload).toEqual({ expected_revision: 8, targets: [row.source], references: [{ kind: 'character', op: 'replace', ids: [3, 7] }, { kind: 'relationship', op: 'add', timeline_ids: [19] }] })
    expect(payload).not.toHaveProperty('tags')
    expect(payload).not.toHaveProperty('pov')
  })
  it('清空不携带旧选择，未设置视角和无变更不发送请求', () => {
    const draft = emptyBatchDraft()
    expect(() => batchPayload(0, [row.source], draft)).toThrow('选择至少一项')
    draft.pov = 'set'
    expect(() => batchPayload(0, [row.source], draft)).toThrow('视角角色')
    draft.pov = 'clear'; draft.tags = { op: 'clear', ids: [6] }
    expect(batchPayload(1, [row.source], draft)).toEqual({ expected_revision: 1, targets: [row.source], pov: { op: 'clear' }, tags: { op: 'clear' } })
  })
  it('批量限制目标数量并拒绝重复，避免一个目标多次受影响', () => {
    const draft = { ...emptyBatchDraft(), pov: 'clear' as const }
    expect(() => batchPayload(1, [], draft)).toThrow('选择')
    expect(() => batchPayload(1, [row.source, row.source], draft)).toThrow('重复')
    expect(() => batchPayload(1, Array.from({ length: 101 }, (_, id) => ({ kind: 'chapter', id: id + 1 })), draft)).toThrow('100')
  })
  it('不同类型相同数字不会混淆，未建章不伪造正式章号', () => {
    expect(planningReferenceKey({ kind: 'relationship', timeline_id: 7 })).not.toBe(planningReferenceKey({ kind: 'character', id: 7 }))
    expect(planningSourceKey(row.source)).toBe('plan:4')
    expect(planningRowLabel(row)).toContain('细纲 3')
    expect(planningRowLabel(row)).not.toContain('第 4 章')
  })
  it('自动追踪不支持、未写、歧义与手动标记区分', () => {
    const base = { kind: 'setting' as const, row_key: 'x', column_key: 'y', manual: false, pov: false, tracking_supported: false, has_content: false, mention_count: 0, ambiguous_count: 0 }
    expect(settingCellDescription(base)).toContain('无自动追踪')
    expect(settingCellDescription({ ...base, tracking_supported: true })).toContain('未写')
    expect(settingCellDescription({ ...base, tracking_supported: true, has_content: true, ambiguous_count: 2, manual: true })).toContain('歧义 2')
    expect(settingCellDescription({ ...base, manual: true })).toContain('手动关联')
  })
  it('只接受完整可解的合并冲突，不强转错误响应', () => {
    expect(readPlanningConflict(new ApiError(409, '过期', { code: 'planning_stale_revision' }))).toBeNull()
    expect(readPlanningConflict(new ApiError(409, '冲突', { code: 'planning_merge_conflict', current_revision: 2, unit_ids: [1, 'bad'] }))).toBeNull()
    const err = new ApiError(409, '冲突', { code: 'planning_merge_conflict', current_revision: 2, unit_ids: [1, 2], pov_character_ids: [3, null], node_conflicts: [], survivor_unit_id: 1, units: [{ unit_id: 1, title: '初遇', pov_character_id: 3, pov_character_name: '林岚' }] })
    expect(readPlanningConflict(err)).toMatchObject({ revision: 2, unitIds: [1, 2], survivorUnitId: 1, povNames: { 3: '林岚' } })
  })
})
