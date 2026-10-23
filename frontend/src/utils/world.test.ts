import { expect, it } from 'vitest'
import { groupWorld } from './world'

it('世界观按固定分类顺序分组，未知分类归入其他', () => {
  const groups = groupWorld([
    { id: 1, category: 'other' },
    { id: 2, category: 'place' },
    { id: 3, category: 'weird' },
    { id: 4, category: 'rule' },
  ])
  expect(groups.map((g) => g.category)).toEqual(['place', 'rule', 'other'])
  expect(groups[2].items.map((i) => i.id)).toEqual([1, 3])
})
