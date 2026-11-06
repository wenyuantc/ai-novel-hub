import { describe, expect, it } from 'vitest'
import type { SettingFieldSlot } from '../../api/types'
import { emptyFieldValue, entryProgressionTarget, fieldSlotValue, fieldValueFromRaw, sameFieldValue, validateEntryFieldValue } from './valueDraft'

const slot: SettingFieldSlot = { field_id: null, definition_id: 8, source: 'shared', label: '阵营', field_type: 'dropdown', base_value: null, value: { option_id: 4 }, options: [{ id: 3, definition_id: 8, label: '旧阵营', archived: true, sort: 0 }, { id: 4, definition_id: 8, label: '新阵营', archived: false, sort: 1 }], reference_types: [], reference: null, base_reference: null, archived: false, ai_hidden: false, sort: 0 }

describe('条目类型化字段草稿', () => {
  it('当前值和起始值分别读取，不把未来投影反填到基础草稿', () => {
    expect(fieldSlotValue(slot, true)).toEqual({ field_type: 'dropdown', value: null })
    expect(fieldSlotValue(slot)).toEqual({ field_type: 'dropdown', value: { option_id: 4 } })
    expect(emptyFieldValue('reference')).toEqual({ field_type: 'reference', value: null })
  })
  it('只接受公开引用及严格选项身份，不接受内部handle、关系或伪整数', () => {
    expect(fieldValueFromRaw('reference', { kind: 'organization', id: 7 })).toEqual({ field_type: 'reference', value: { kind: 'organization', id: 7 } })
    for (const raw of [{ reference_handle_id: 7 }, { kind: 'relationship', id: 7 }, { kind: 'world', id: '7' }, { kind: 'world', id: true }, { kind: 'world', id: 2_147_483_648 }]) expect(fieldValueFromRaw('reference', raw)).toBeNull()
    expect(fieldValueFromRaw('dropdown', { option_id: 4, extra: true })).toBeNull()
    expect(fieldValueFromRaw('dropdown', null)).toEqual({ field_type: 'dropdown', value: null })
  })
  it('停用旧选项可原样保存或清空，不能新选；不同字段的选项被拒绝', () => {
    const old = { field_type: 'dropdown' as const, value: { option_id: 3 } }
    expect(validateEntryFieldValue(old, slot, old)).toBeNull()
    expect(validateEntryFieldValue(old, slot)).toBeTruthy()
    expect(validateEntryFieldValue({ field_type: 'dropdown', value: { option_id: 99 } }, slot)).toBeTruthy()
    expect(validateEntryFieldValue({ field_type: 'dropdown', value: null }, slot, old)).toBeNull()
  })
  it('四类值按真实身份比较，清空和省略不混用', () => {
    expect(sameFieldValue({ field_type: 'reference', value: { kind: 'world', id: 7 } }, { field_type: 'reference', value: { kind: 'character', id: 7 } })).toBe(false)
    expect(sameFieldValue({ field_type: 'dropdown', value: null }, { field_type: 'dropdown', value: { option_id: 4 } })).toBe(false)
  })
  it('独立组织和职业定义不会使用成员或人物职业主体', () => {
    expect(entryProgressionTarget({ kind: 'organization', id: 7 })).toEqual({ subject_kind: 'organization', organization_id: 7 })
    expect(entryProgressionTarget({ kind: 'career', id: 7 })).toEqual({ subject_kind: 'career_entry', career_id: 7 })
  })
})
