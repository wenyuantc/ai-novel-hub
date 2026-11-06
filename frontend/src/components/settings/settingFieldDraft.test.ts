import { describe, expect, it } from 'vitest'
import type { SettingFieldDefinition } from '../../api/types'
import { definitionDraft, definitionDraftError, definitionInput, fieldValueError, formatFieldValue, optionDraftError, settingFieldTypeFilter } from './settingFieldDraft'

const definition: SettingFieldDefinition = {
  id: 10, project_id: 1, label: '境界', field_type: 'dropdown', applicable_types: ['character'], reference_types: [], sort: 2, archived: false,
  options: [{ id: 17, definition_id: 10, label: '练气', sort: 0, archived: true }],
}

describe('字段配置与类型化值草稿', () => {
  it('四类条目的字段配置深链只接受合法适用类型', () => {
    expect(settingFieldTypeFilter('?type=world.place')).toEqual(['world.place'])
    expect(settingFieldTypeFilter('?type=career')).toEqual(['career'])
    expect(settingFieldTypeFilter('?type=world.bad')).toEqual([])
    expect(settingFieldTypeFilter('')).toEqual([])
  })
  it('配置草稿深复制范围、精确构造版本请求，不混入条目值或默认值', () => {
    const draft = definitionDraft(definition)
    draft.label = '  修为  '
    draft.applicable_types.push('organization')
    expect(definition.applicable_types).toEqual(['character'])
    expect(definitionInput(draft, 5)).toEqual({ expected_fields_revision: 5, label: '修为', field_type: 'dropdown', applicable_types: ['character', 'organization'], reference_types: [], sort: 2, archived: false })
  })
  it('引用范围、空适用类型及整数排序在发送前校验', () => {
    const draft = definitionDraft(null)
    expect(definitionDraftError(draft)).toContain('名称')
    draft.label = '所属'
    expect(definitionDraftError(draft)).toContain('适用')
    draft.applicable_types = ['character']
    draft.field_type = 'reference'
    expect(definitionDraftError(draft)).toContain('引用')
    draft.reference_types = ['organization']
    expect(definitionDraftError(draft)).toBeNull()
    draft.sort = null
    expect(definitionDraftError(draft)).toContain('顺序')
    expect(optionDraftError({ label: ' 已停用 ', sort: 0, archived: true })).toBeNull()
  })
  it('单行按码点限制并拒绝行分隔符，清空选择与空文本保持类型', () => {
    expect(fieldValueError({ field_type: 'line', value: '😀'.repeat(500) })).toBeNull()
    expect(fieldValueError({ field_type: 'line', value: '😀'.repeat(501) })).toContain('500')
    for (const linebreak of ['\n', '\r', '\v', '\f', '\u001c', '\u001d', '\u001e', '\u2028', '\u2029', '\u0085']) expect(fieldValueError({ field_type: 'line', value: `甲${linebreak}乙` })).toContain('换行')
    expect(fieldValueError({ field_type: 'dropdown', value: null })).toBeNull()
    expect(fieldValueError({ field_type: 'reference', value: null })).toBeNull()
    expect(fieldValueError({ field_type: 'text', value: '' })).toBeNull()
  })
  it('显示停用选项名称、引用当前名称和空值，不把对象或ID当正文', () => {
    expect(formatFieldValue({ field_type: 'dropdown', value: { option_id: 17 } }, definition.options)).toBe('练气（已停用）')
    expect(formatFieldValue({ field_type: 'dropdown', value: { option_id: 99 } }, definition.options)).toBe('选项不可用')
    expect(formatFieldValue({ field_type: 'reference', value: { kind: 'organization', id: 8 } }, [], { reference: { kind: 'organization', id: 8 }, type: 'organization', name: '青云宗', category: null })).toBe('青云宗')
    expect(formatFieldValue({ field_type: 'reference', value: { kind: 'organization', id: 8 } })).toBe('引用条目不可用')
    expect(formatFieldValue({ field_type: 'reference', value: null })).toBe('未填写')
  })
})
