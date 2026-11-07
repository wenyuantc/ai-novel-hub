import type { ProgressionPositionInput, SettingEntryRef } from '../api/types'
import { readPositiveId } from './mentions'
import { storyPositionQuery } from './storyPosition'

export type EntityRef = SettingEntryRef
const ENTITY_PAGES: Record<EntityRef['kind'], string> = {
  character: 'characters', world: 'world', organization: 'organizations', career: 'careers',
}

/** 四类设定条目共用现有详情入口，不把关系时间线并入条目身份。 */
export function entityDetailHref(projectId: number, reference: EntityRef, position?: ProgressionPositionInput | null): string {
  const params = new URLSearchParams({ id: String(reference.id) })
  if (position) {
    const query = storyPositionQuery(position)
    params.set('view', 'effective')
    for (const [key, value] of Object.entries(query)) params.set(key, String(value))
  }
  return `/projects/${projectId}/${ENTITY_PAGES[reference.kind]}?${params}`
}

/** 历史深链只影响只读预览；混合锚、伪整数或非法边界均不作为章节位置。 */
export function readEntityFieldPosition(params: URLSearchParams): ProgressionPositionInput | null {
  if (params.get('view') !== 'effective') return null
  const boundary = params.get('boundary')
  if (boundary !== 'start' && boundary !== 'end') return null
  const chapter = params.get('chapter_id'), planned = params.get('chapter_no'), scene = params.get('scene_id')
  if ([chapter, planned, scene].filter((value) => value !== null).length !== 1) return null
  const value = readPositiveId(chapter ?? planned ?? scene)
  if (value === null || value > 2_147_483_647) return null
  return chapter !== null ? { anchor_kind: 'chapter', chapter_id: value, chapter_no: null, boundary }
    : scene !== null ? { anchor_kind: 'scene', scene_id: value, chapter_id: null, chapter_no: null, boundary }
      : { anchor_kind: 'planned', chapter_id: null, chapter_no: value, boundary }
}
