import { describe, expect, it } from 'vitest'
import { entrySelectOptions, entrySelectValues } from './entrySelectState'

describe('分页条目选择状态', () => {
  it('选中条目不在当前页时仍保留原身份和已知名称', () => {
    const value = [7, 8]
    const options = entrySelectOptions(
      [{ value: 9, label: '本页角色' }], value,
      [{ value: 7, label: '旧页角色' }, { value: 8, label: '另一页角色' }, { value: 10, label: '未选角色' }],
    )
    expect(options).toEqual([
      { value: 9, label: '本页角色' }, { value: 7, label: '旧页角色' }, { value: 8, label: '另一页角色' },
    ])
    expect(value).toEqual([7, 8])
  })

  it('搜索为空或读取失败而无候选时，不把已选值误当作删除', () => {
    expect(entrySelectOptions([], [8], [{ value: 8, label: '已选组织' }])).toEqual([{ value: 8, label: '已选组织' }])
    expect(entrySelectOptions([], [8])).toEqual([{ value: 8, label: '已选条目 #8' }])
  })

  it('当前页的新名称和停用状态优先于旧选中摘要，且不加入未选摘要', () => {
    expect(entrySelectOptions(
      [{ value: 8, label: '新名称', disabled: true }], [8],
      [{ value: 8, label: '旧名称' }, { value: 9, label: '其他条目' }],
    )).toEqual([{ value: 8, label: '新名称', disabled: true }])
  })

  it('复合身份不按数字猜测或跨类型合并', () => {
    expect(entrySelectOptions(
      [{ value: 'world:7', label: '世界观 · 青云宗' }], ['character:7'],
      [{ value: 'character:7', label: '角色 · 青云宗' }],
    )).toEqual([
      { value: 'world:7', label: '世界观 · 青云宗' }, { value: 'character:7', label: '角色 · 青云宗' },
    ])
  })

  it('单选和多选共用受控数组，显式清空才返回空数组', () => {
    expect(entrySelectValues(7)).toEqual([7])
    expect(entrySelectValues([7, 8])).toEqual([7, 8])
    expect(entrySelectValues('career:7')).toEqual(['career:7'])
    expect(entrySelectValues(undefined)).toEqual([])
    expect(entrySelectValues([])).toEqual([])
  })
})
