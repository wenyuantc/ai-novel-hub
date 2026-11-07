import { describe, expect, it } from 'vitest'
import { moveScene, sameSceneIds, sceneTargetAllocation } from './scenePresentation'
import { utf16ToCodePoint } from '../../utils/textOffset'
import { batchPayload, emptyBatchDraft, planningRowLink } from '../../utils/planning'
import type { PlanningRow } from '../../api/types'

const row: PlanningRow = { key: 'scene:8', unit_id: null, source: { kind: 'scene', id: 8 }, chapter_id: 4, plan_id: null, title: '雨夜', chapter_no: 2, outline_id: null, volume_no: null, volume_title: '', seq: null, has_content: false, pov_character_id: null, pov_character_name: null, tags: [], references: [], scene_id: 8, scene_sort: 1, content_revision: 7, scene_planning: null, source_level: 'scene', source_chapter_id: 4 }
describe('场景结构与覆盖载荷', () => {
  it('上下移和拖动共用完整同章顺序，不修改原列表', () => {
    const ids = [7, 8, 9]
    expect(moveScene(ids, 9, 0)).toEqual([9, 7, 8])
    expect(moveScene(ids, 7, -1)).toEqual(ids)
    expect(ids).toEqual([7, 8, 9])
    expect(sameSceneIds([8, 7, 9], ids)).toBe(true)
    expect(sameSceneIds([7, 7, 9], ids)).toBe(false)
    expect(sameSceneIds([7, 8], ids)).toBe(false)
  })
  it('整章目标只分配一次，余数给前序场景，每个场景保持正数', () => {
    expect(sceneTargetAllocation(1001, [7, 8, 9])).toEqual([{ scene_id: 7, words: 334 }, { scene_id: 8, words: 334 }, { scene_id: 9, words: 333 }])
    expect(sceneTargetAllocation(2, [7, 8, 9])).toBeNull()
  })
  it('emoji光标转换为码点，切片可精确重组含空白原文', () => {
    const text = '\n 甲😀乙\n\n尾 '
    const offset = utf16ToCodePoint(text, text.indexOf('乙'))
    expect(offset).toBe(4)
    const points = Array.from(text)
    expect(points.slice(0, offset).join('') + points.slice(offset).join('')).toBe(text)
  })
  it('每项恢复继承不夹带残留ID；明确清空与继承不同，并携带章内容CAS', () => {
    const draft = emptyBatchDraft()
    draft.pov = 'inherit'
    draft.tags = { op: 'clear', ids: [3] }
    draft.references = { character: { op: 'inherit', ids: [4] }, relationship: { op: 'clear', ids: [5] }, world: { op: 'replace', ids: [6] } }
    expect(batchPayload(9, [row.source], draft, [row])).toEqual({ expected_revision: 9, targets: [row.source], pov: { op: 'inherit' }, tags: { op: 'clear' }, references: [{ kind: 'character', op: 'inherit' }, { kind: 'world', op: 'replace', ids: [6] }, { kind: 'relationship', op: 'clear' }], expected_content_revisions: [{ chapter_id: 4, content_revision: 7 }] })
    expect(planningRowLink(1, row)).toBe('/projects/1/chapters/4?scene_id=8')
  })
  it('同章不同版本或缺少场景版本时拒绝构造写请求', () => {
    const draft = { ...emptyBatchDraft(), pov: 'clear' as const }
    expect(() => batchPayload(1, [row.source], draft)).toThrow('版本')
    const other = { ...row, key: 'scene:9', source: { kind: 'scene' as const, id: 9 }, content_revision: 8 }
    expect(() => batchPayload(1, [row.source, other.source], draft, [row, other])).toThrow('版本不一致')
  })
})
