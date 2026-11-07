import { SETTING_ENTRY_TYPE_LABELS } from '../../api/settingFields'
import type { ProgressionPositionInput, SettingCategoryEntryQuery, SettingCategoryVersions, SettingEntryType, SettingFieldDefinitionList } from '../../api/types'
import { readEntityFieldPosition } from '../../utils/entityLinks'
import { readPositiveId } from '../../utils/mentions'
import { storyPositionQuery } from '../../utils/storyPosition'

export interface CategoryFilters { types: SettingEntryType[]; q: string; page: number; pageSize: number; position: ProgressionPositionInput | null; invalidPosition: boolean }
export interface CategoryBundle<T> { value: T; definitions: SettingFieldDefinitionList }
const entryType = (value: string): value is SettingEntryType => Object.hasOwn(SETTING_ENTRY_TYPE_LABELS, value)
/** 分类筛选在选模式后可以暂时未填章节；空锚仅用于URL中的输入草稿，不能发查询。 */
function readCategoryPosition(params: URLSearchParams): ProgressionPositionInput | null {
  const position = readEntityFieldPosition(params)
  if (position) return position
  if (params.get('view') !== 'effective') return null
  const boundary = params.get('boundary')
  if (boundary !== 'start' && boundary !== 'end') return null
  const chapter = params.get('chapter_id'), planned = params.get('chapter_no'), scene = params.get('scene_id')
  const anchor = chapter === '' && planned === null && scene === null ? 'chapter' : planned === '' && chapter === null && scene === null ? 'planned' : scene === '' && chapter === null && planned === null ? 'scene' : null
  return anchor ? { anchor_kind: anchor, chapter_id: null, chapter_no: null, ...(anchor === 'scene' ? { scene_id: null } : {}), boundary } : null
}
export const initialCategoryFilters = (): CategoryFilters => ({ types: [], q: '', page: 1, pageSize: 20, position: null, invalidPosition: false })
export function changeCategoryFilters(current: CategoryFilters, patch: Partial<CategoryFilters>): CategoryFilters {
  const changed = (patch.types !== undefined && JSON.stringify(patch.types) !== JSON.stringify(current.types)) || (patch.q !== undefined && patch.q !== current.q)
    || (patch.position !== undefined && JSON.stringify(patch.position) !== JSON.stringify(current.position)) || (patch.pageSize !== undefined && patch.pageSize !== current.pageSize)
  return { ...current, ...patch, ...(patch.position !== undefined ? { invalidPosition: false } : {}), ...(changed ? { page: 1 } : {}) }
}
export function categoryQueryRequest(filters: CategoryFilters): { ok: true; query: SettingCategoryEntryQuery; position: ProgressionPositionInput | null } | { ok: false; error: string } {
  const position = filters.position
  if (filters.invalidPosition) return { ok: false, error: '章节位置无效，请重新选择已有章、场景或计划章' }
  let positionQuery = {}
  try { if (position) positionQuery = storyPositionQuery(position) } catch { return { ok: false, error: position?.anchor_kind === 'scene' ? '请选择有效场景' : '请选择有效章节' } }
  if ([...filters.q].length > 200) return { ok: false, error: '名称搜索最多 200 字' }
  return { ok: true, position: position ? { ...position } : null, query: { ...(filters.types.length ? { types: [...filters.types] } : {}), ...(filters.q.trim() ? { q: filters.q.trim() } : {}), page: filters.page, page_size: filters.pageSize,
    ...positionQuery } }
}
export function readCategoryLocation(search: string): { categoryId: number | null; filters: CategoryFilters } {
  const params = new URLSearchParams(search)
  const position = readCategoryPosition(params)
  const history = ['view', 'chapter_id', 'chapter_no', 'scene_id', 'boundary'].some((key) => params.has(key))
  const pageSize = Number(params.get('page_size'))
  return { categoryId: readPositiveId(params.get('id')), filters: { types: [...new Set(params.getAll('type').filter(entryType))], q: params.get('q') ?? '',
    page: readPositiveId(params.get('page')) ?? 1, pageSize: [20, 50, 100].includes(pageSize) ? pageSize : 20,
    position, invalidPosition: history && !position } }
}
export function categoryLocation(categoryId: number | null, filters: CategoryFilters): string {
  const params = new URLSearchParams()
  if (categoryId !== null) params.set('id', String(categoryId))
  filters.types.forEach((type) => params.append('type', type))
  if (filters.q) params.set('q', filters.q)
  if (filters.page > 1) params.set('page', String(filters.page))
  if (filters.pageSize !== 20) params.set('page_size', String(filters.pageSize))
  if (filters.position || filters.invalidPosition) {
    params.set('view', 'effective')
    const position = filters.position
    if (position) {
      const value = position.anchor_kind === 'chapter' ? position.chapter_id : position.anchor_kind === 'scene' ? position.scene_id : position.chapter_no
      params.set(position.anchor_kind === 'chapter' ? 'chapter_id' : position.anchor_kind === 'scene' ? 'scene_id' : 'chapter_no', value == null ? '' : String(value))
      params.set('boundary', position.boundary)
    }
  }
  return params.toString() ? `?${params}` : ''
}
export function withCategoryMetadata<T extends Pick<SettingCategoryVersions, 'fields_revision'>>(value: T, definitions: SettingFieldDefinitionList): CategoryBundle<T> {
  if (value.fields_revision !== definitions.fields_revision) throw new Error('字段配置在读取期间发生变化，请重试以核对同一版本')
  return { value, definitions }
}
