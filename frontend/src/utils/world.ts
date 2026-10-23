import type { WorldCategory } from '../api/types'

export const WORLD_CATEGORIES: WorldCategory[] = ['place', 'faction', 'item', 'rule', 'history', 'other']

export const WORLD_CATEGORY_LABELS: Record<WorldCategory, string> = {
  place: '地点',
  faction: '势力',
  item: '物品',
  rule: '规则',
  history: '历史',
  other: '其他',
}

export function asWorldCategory(c: string): WorldCategory {
  return (WORLD_CATEGORIES as string[]).includes(c) ? (c as WorldCategory) : 'other'
}

/** 按分类分组（分类顺序固定，组内保持原顺序；未知分类归入「其他」；空组省略） */
export function groupWorld<T extends { category: string }>(items: T[]): { category: WorldCategory; items: T[] }[] {
  const map = new Map<WorldCategory, T[]>()
  for (const it of items) {
    const c = asWorldCategory(it.category)
    const list = map.get(c) ?? []
    list.push(it)
    map.set(c, list)
  }
  return WORLD_CATEGORIES.filter((c) => map.has(c)).map((c) => ({ category: c, items: map.get(c) as T[] }))
}
