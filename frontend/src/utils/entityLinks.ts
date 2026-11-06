import type { ProgressionPositionInput, SettingEntryRef } from '../api/types'
import { readPositiveId } from './mentions'

export type EntityRef = SettingEntryRef
const ENTITY_PAGES: Record<EntityRef['kind'], string> = {
  character: 'characters', world: 'world', organization: 'organizations', career: 'careers',
}

/** 四类设定条目共用现有详情入口，不把关系时间线并入条目身份。 */
export function entityDetailHref(projectId: number, reference: EntityRef, position?: ProgressionPositionInput | null): string {
  const params = new URLSearchParams({ id: String(reference.id) })
  if (position) {
    const value = position.anchor_kind === 'chapter' ? position.chapter_id : position.chapter_no
    if (value === null || !Number.isInteger(value) || value < 1 || value > 2_147_483_647 || (position.anchor_kind === 'chapter' ? position.chapter_no !== null : position.chapter_id !== null)) throw new Error('有效值预览位置无效')
    params.set('view', 'effective')
    params.set(position.anchor_kind === 'chapter' ? 'chapter_id' : 'chapter_no', String(value))
    params.set('boundary', position.boundary)
  }
  return `/projects/${projectId}/${ENTITY_PAGES[reference.kind]}?${params}`
}

/** 历史深链只影响只读预览；混合锚、伪整数或非法边界均不作为章节位置。 */
export function readEntityFieldPosition(params: URLSearchParams): ProgressionPositionInput | null {
  if (params.get('view') !== 'effective') return null
  const boundary = params.get('boundary')
  if (boundary !== 'start' && boundary !== 'end') return null
  const chapter = params.get('chapter_id'), planned = params.get('chapter_no')
  if ((chapter === null) === (planned === null)) return null
  const value = readPositiveId(chapter ?? planned)
  if (value === null || value > 2_147_483_647) return null
  return chapter !== null ? { anchor_kind: 'chapter', chapter_id: value, chapter_no: null, boundary }
    : { anchor_kind: 'planned', chapter_id: null, chapter_no: value, boundary }
}
