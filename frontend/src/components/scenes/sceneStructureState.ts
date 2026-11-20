import type {
  Scene, SceneBrief, SceneMovePreview, SceneMoveSelection, ScenePage, SceneRestructureRequest, SceneRestructureResult,
} from '../../api/types'

const sameSceneIds = (first: readonly number[], second: readonly number[]) =>
  first.length === second.length && first.every((id, index) => id === second[index])

export function matchesMovePreview(
  preview: SceneMovePreview,
  scene: Scene,
  selection: SceneMoveSelection,
) {
  return preview.scene_id === scene.id && preview.source.chapter_id === scene.chapter_id
    && preview.source.content_revision === scene.content_revision && preview.planning_revision === scene.planning_revision
    && preview.target.chapter_id === selection.target_chapter_id
    && preview.insert_before_scene_id === selection.insert_before_scene_id
}

/** 连接中断后，只按新读取的活动归属呈现结果。 */
export function locateMoveAfterDisconnect(sceneId: number, source: ScenePage, target: ScenePage) {
  const inSource = source.items.some((item) => item.id === sceneId)
  const inTarget = target.items.some((item) => item.id === sceneId)
  if (inSource === inTarget) return 'unknown'
  return inTarget ? 'target' : 'source'
}

export function restructureRequest(
  scene: Scene,
  items: readonly SceneBrief[],
  neighborId: number | null,
): SceneRestructureRequest | null {
  const expected_content_revision = scene.content_revision
  if (neighborId === null) return { operation: 'split', scene_ids: [scene.id], expected_content_revision }
  const current = items.findIndex((item) => item.id === scene.id && item.chapter_id === scene.chapter_id)
  if (current < 0) return null
  const previous = items[current - 1]
  const next = items[current + 1]
  if (previous?.id === neighborId && previous.chapter_id === scene.chapter_id) {
    return { operation: 'merge', scene_ids: [previous.id, scene.id], expected_content_revision }
  }
  if (next?.id === neighborId && next.chapter_id === scene.chapter_id) {
    return { operation: 'merge', scene_ids: [scene.id, next.id], expected_content_revision }
  }
  return null
}

export function matchesRestructureScope(
  result: Pick<SceneRestructureResult, 'operation' | 'scene_ids' | 'content_revision' | 'planning_revision'>,
  scene: Scene,
  page: ScenePage | null,
) {
  if (result.content_revision !== scene.content_revision || result.planning_revision !== scene.planning_revision
    || !page || page.content_revision !== result.content_revision || page.planning_revision !== result.planning_revision) return false
  const first = page.items.findIndex((item) => item.id === result.scene_ids[0] && item.chapter_id === scene.chapter_id)
  if (first < 0) return false
  const targets = page.items.slice(first, first + (result.operation === 'merge' ? 2 : 1))
  return targets.length === (result.operation === 'merge' ? 2 : 1)
    && targets.every((item) => item.chapter_id === scene.chapter_id)
    && sameSceneIds(result.scene_ids, targets.map((item) => item.id))
}
