import { ROLE_TYPES } from '../api/characters'
import type { Character } from '../api/types'

export const ALL_CHARACTERS = '__all__'

const roleLabel = (character: Character) => character.role_type.trim() || '未设定'

/** 定位计数使用完整列表，旧自由文本定位仍保留独立入口。 */
export function characterRoleFilters(items: readonly Character[]) {
  const counts = new Map<string, number>()
  for (const item of items) {
    const role = roleLabel(item)
    counts.set(role, (counts.get(role) ?? 0) + 1)
  }
  const roles = [...new Set([...ROLE_TYPES, ...counts.keys()])].filter((role) => counts.has(role))
  return [
    { value: ALL_CHARACTERS, label: '全部', count: items.length },
    ...roles.map((role) => ({ value: role, label: role, count: counts.get(role)! })),
  ]
}

/** 姓名、别名和文字设定搜索可与定位叠加，不改变服务端返回的排序。 */
export function filterCharacters(items: readonly Character[], role: string, query: string): Character[] {
  const keyword = query.trim().toLocaleLowerCase()
  return items.filter((item) => {
    if (role !== ALL_CHARACTERS && roleLabel(item) !== role) return false
    return !keyword || [item.name, ...item.aliases, item.profile, item.appearance, item.personality,
      item.background, item.current_state, item.relations_text].some((text) => text.toLocaleLowerCase().includes(keyword))
  })
}

/** 数据刷新后限制页码，避免删除末页最后一个角色后出现空白页。 */
export function characterPage(items: readonly Character[], requestedPage: number, pageSize: number) {
  const page = Math.max(1, Math.min(requestedPage, Math.ceil(items.length / pageSize)))
  const offset = (page - 1) * pageSize
  return {
    page,
    items: items.slice(offset, offset + pageSize),
    start: items.length > 0 ? offset + 1 : 0,
    end: Math.min(offset + pageSize, items.length),
  }
}

/** 年龄允许自由文本，只有纯数字才补充「岁」。 */
export function characterAgeLabel(age: string): string {
  const value = age.trim()
  return /^\d+$/.test(value) ? `${value} 岁` : value
}
