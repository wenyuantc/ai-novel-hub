import { describe, expect, it } from 'vitest'
import type { Scene, SceneMovePreview, ScenePage } from '../../api/types'
import { locateMoveAfterDisconnect, matchesMovePreview, matchesRestructureScope, restructureRequest } from './sceneStructureState'

const scene: Scene = {
  id: 8, chapter_id: 2, chapter_no: 2, chapter_title: '河岸', title: '过桥', summary: '', sort: 1,
  word_count: 5, content_revision: 3, planning_revision: 6, created_at: '', updated_at: '',
  content: '走过桥。\r\n到达河岸。', separator_before: '\n', active: true,
  planning: {
    unit_id: null, binding_epoch: null, revision: 6,
    raw: {
      modes: { pov: 'inherit', tags: 'inherit', character: 'inherit', world: 'inherit', organization: 'inherit', career: 'inherit', relationship: 'inherit' },
      pov_character_id: null, tag_ids: [], references: [],
    },
    effective: {
      sources: { pov: 'chapter', tags: 'chapter', character: 'chapter', world: 'chapter', organization: 'chapter', career: 'chapter', relationship: 'chapter' },
      pov_character_id: null, pov_character_name: null, tags: [], references: [],
    },
  },
}
const earlier = { ...scene, id: 4, sort: 0 }
const later = { ...scene, id: 11, sort: 2 }
const page = (items: ScenePage['items']): ScenePage => ({
  items, offset: 0, limit: 100, total: items.length, content_revision: 3, planning_revision: 6,
})
const preview: SceneMovePreview = {
  scene_id: 8, source: { chapter_id: 2, chapter_no: 2, title: '河岸', content_revision: 3, scene_count: 3 },
  target: { chapter_id: 3, chapter_no: 3, title: '码头', content_revision: 5, scene_count: 2 },
  insert_before_scene_id: 13, source_position: 2, target_position: 1, planning_revision: 6,
  signature: 'a'.repeat(64), source_will_be_empty: false, planning_changes: [], timeline_events: [],
  source_seam: { before: '', after: '' }, target_seam: { before: '', after: '' }, moved_start: '', moved_end: '', blockers: [],
}

describe('作者确认时的场景范围', () => {
  it('换章、换插入点或保存已推进版本都必须重新预览', () => {
    const target = { target_chapter_id: 3, insert_before_scene_id: 13 }
    expect(matchesMovePreview(preview, scene, target)).toBe(true)
    expect(matchesMovePreview(preview, { ...scene, content_revision: 4 }, target)).toBe(false)
    expect(matchesMovePreview(preview, { ...scene, planning_revision: 7 }, target)).toBe(false)
    expect(matchesMovePreview(preview, { ...scene, chapter_id: 10 }, target)).toBe(false)
    expect(matchesMovePreview(preview, scene, { ...target, insert_before_scene_id: null })).toBe(false)
  })

  it('未知回执按重读的实际归属处理，矛盾或缺失时继续显示未知', () => {
    const relocated = { ...scene, chapter_id: 3 }
    expect(locateMoveAfterDisconnect(8, page([earlier]), page([relocated]))).toBe('target')
    expect(locateMoveAfterDisconnect(8, page([earlier, scene]), page([]))).toBe('source')
    expect(locateMoveAfterDisconnect(8, page([scene]), page([relocated]))).toBe('unknown')
    expect(locateMoveAfterDisconnect(8, page([earlier]), page([]))).toBe('unknown')
  })

  it('合并仅接受同章的直接相邻场景，按原文顺序发送', () => {
    const ordered = [earlier, scene, later]
    expect(restructureRequest(scene, ordered, null)).toEqual({
      operation: 'split', scene_ids: [8], expected_content_revision: 3,
    })
    expect(restructureRequest(scene, ordered, 4)?.scene_ids).toEqual([4, 8])
    expect(restructureRequest(scene, ordered, 11)?.scene_ids).toEqual([8, 11])
    expect(restructureRequest(earlier, ordered, 11)).toBeNull()
    expect(restructureRequest(scene, [scene, { ...later, chapter_id: 3 }], 11)).toBeNull()
  })

  it('后台建议可在同章重开，跨章移动、正文或规划版本变化后均无法采纳', () => {
    const result = { operation: 'merge' as const, scene_ids: [4, 8], content_revision: 3, planning_revision: 6 }
    expect(matchesRestructureScope(result, later, page([earlier, scene, later]))).toBe(true)
    expect(matchesRestructureScope(result, later, page([earlier, later]))).toBe(false)
    expect(matchesRestructureScope(result, later, page([earlier, later, scene]))).toBe(false)
    expect(matchesRestructureScope(result, { ...later, content_revision: 5 }, page([earlier, scene, later]))).toBe(false)
    expect(matchesRestructureScope(result, later, { ...page([earlier, scene]), planning_revision: 7 })).toBe(false)
  })
})
