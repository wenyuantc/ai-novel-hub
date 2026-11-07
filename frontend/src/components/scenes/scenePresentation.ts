import type { PlanningRow, SceneBrief } from '../../api/types'

/** 场景卡片、规划覆盖与 AI 预览都指向同一个已保存来源。 */
export function scenePlanningRow(scene: SceneBrief): PlanningRow {
  return {
    key: `scene:${scene.id}`, source: { kind: 'scene', id: scene.id }, unit_id: scene.planning.unit_id,
    chapter_id: scene.chapter_id, chapter_no: scene.chapter_no, plan_id: null, outline_id: null,
    volume_no: null, volume_title: '', seq: null, title: scene.title, has_content: scene.word_count > 0,
    pov_character_id: scene.planning.effective.pov_character_id, pov_character_name: scene.planning.effective.pov_character_name,
    tags: scene.planning.effective.tags, references: scene.planning.effective.references,
    scene_id: scene.id, scene_sort: scene.sort, scene_planning: scene.planning, content_revision: scene.content_revision, source_level: 'scene', source_chapter_id: scene.chapter_id,
  }
}

/** 仅调整本章完整身份列表，不修改 Scene 本身或生成任何分隔符。 */
export function moveScene(ids: readonly number[], id: number, target: number): number[] {
  const index = ids.indexOf(id)
  if (index < 0 || target < 0 || target >= ids.length || index === target) return [...ids]
  const result = [...ids]
  result.splice(index, 1)
  result.splice(target, 0, id)
  return result
}
export function sceneLabel(scene: Pick<SceneBrief, 'title' | 'sort'>) { return `场景 ${scene.sort + 1} · ${scene.title || '未命名'}` }
export function sameSceneIds(a: readonly number[], b: readonly number[]) { return a.length === b.length && new Set(a).size === a.length && a.every((id) => b.includes(id)) }

/** 整章目标为总量，余数按已保存场景顺序分配，不给每个场景重复总量。 */
export function sceneTargetAllocation(total: number, sceneIds: readonly number[]) {
  if (!sceneIds.length || !Number.isSafeInteger(total) || total < sceneIds.length) return null
  const base = Math.floor(total / sceneIds.length)
  const extra = total % sceneIds.length
  return sceneIds.map((scene_id, index) => ({ scene_id, words: base + (index < extra ? 1 : 0) }))
}
