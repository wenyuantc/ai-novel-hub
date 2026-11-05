import { describe, expect, it } from 'vitest'
import type { Character } from '../api/types'
import { ALL_CHARACTERS, characterAgeLabel, characterPage, characterRoleFilters, filterCharacters } from './characterList'

const character = (id: number, values: Partial<Character> = {}): Character => ({
  id, project_id: 1, name: `角色${id}`, role_type: '配角', aliases: [], age: '', gender: '',
  profile: '', personality: '', appearance: '', background: '', relations_text: '', current_state: '',
  life_status: 'alive', always_in_context: false, sort: id, ...values,
})

describe('角色列表筛选', () => {
  it('统计真实定位数量，兼容旧定位和未设定值', () => {
    const items = [character(1, { role_type: ' 主角 ' }), character(2, { role_type: '主角' }),
      character(3, { role_type: '旧定位' }), character(4, { role_type: ' ' })]
    expect(characterRoleFilters(items)).toEqual([
      { value: ALL_CHARACTERS, label: '全部', count: 4 },
      { value: '主角', label: '主角', count: 2 },
      { value: '旧定位', label: '旧定位', count: 1 },
      { value: '未设定', label: '未设定', count: 1 },
    ])
    expect(filterCharacters(items, '未设定', '')).toEqual([items[3]])
  })

  it('按姓名、别名和设定搜索，忽略查询首尾空白及大小写', () => {
    const items = [character(1, { name: '夜承渊', aliases: ['十一郎', 'Emperor'] }),
      character(2, { background: '曾在北境驻军' }), character(3, { current_state: '追查边报来源' })]
    expect(filterCharacters(items, ALL_CHARACTERS, ' 承渊 ')).toEqual([items[0]])
    expect(filterCharacters(items, ALL_CHARACTERS, ' EMPEROR ')).toEqual([items[0]])
    expect(filterCharacters(items, ALL_CHARACTERS, '北境')).toEqual([items[1]])
    expect(filterCharacters(items, ALL_CHARACTERS, '边报')).toEqual([items[2]])
  })

  it('搜索与定位同时生效，保留原顺序且不修改原列表', () => {
    const items = [character(2, { role_type: '主角', profile: '北境新君' }),
      character(1, { role_type: '配角', profile: '北境将军' }), character(3, { role_type: '主角' })]
    expect(filterCharacters(items, '主角', '北境')).toEqual([items[0]])
    expect(filterCharacters(items, ALL_CHARACTERS, ' ')).toEqual(items)
    expect(items.map((item) => item.id)).toEqual([2, 1, 3])
    expect(filterCharacters(items, ALL_CHARACTERS, '不存在')).toEqual([])
  })
})

describe('角色分页', () => {
  const items = Array.from({ length: 17 }, (_, index) => character(index + 1))

  it('返回当前页的数据与真实显示范围', () => {
    expect(characterPage(items, 2, 8)).toEqual({ page: 2, items: items.slice(8, 16), start: 9, end: 16 })
    expect(characterPage(items, 3, 8)).toEqual({ page: 3, items: [items[16]], start: 17, end: 17 })
  })

  it('删除或筛选缩小结果后回退到有效页，空列表不出现虚假范围', () => {
    expect(characterPage(items.slice(0, 9), 3, 8)).toEqual({ page: 2, items: [items[8]], start: 9, end: 9 })
    expect(characterPage([], 3, 8)).toEqual({ page: 1, items: [], start: 0, end: 0 })
  })
})

describe('角色年龄展示', () => {
  it('数字补充单位，保留自由文本和已有单位', () => {
    expect(characterAgeLabel('22')).toBe('22 岁')
    expect(characterAgeLabel('约二十岁')).toBe('约二十岁')
    expect(characterAgeLabel('22岁')).toBe('22岁')
    expect(characterAgeLabel(' ')).toBe('')
  })
})
