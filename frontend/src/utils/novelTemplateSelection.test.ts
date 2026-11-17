import { describe, expect, it } from 'vitest'
import { addTemplateDependencies, EMPTY_TEMPLATE_SELECTION, templateSelectionSize } from './novelTemplateSelection'

describe('模板选择', () => {
  it('只有明确补选时添加依赖，按类型区分同名及相同数字身份', () => {
    const original = { ...EMPTY_TEMPLATE_SELECTION, character_ids: [4] }
    const result = addTemplateDependencies(original, [
      { kind: 'world', id: 4, name: '同名', reason: '故乡' },
      { kind: 'world', id: 4, name: '同名', reason: '引用' },
      { kind: 'career', id: 8, name: '剑修', reason: '当前职业' },
    ])
    expect(result).toMatchObject({ character_ids: [4], world_ids: [4], career_ids: [8] })
    expect(original.world_ids).toEqual([])
    expect(templateSelectionSize(result)).toBe(3)
  })
})
