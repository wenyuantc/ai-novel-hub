import { describe, expect, it } from 'vitest'
import type { SettingFieldSlot, SettingProgression } from '../../api/types'
import { buildProgressionInput, changeProgressionField, fieldChoices, initialProgressionDraft, progressionUpdateInput, progressionValueLabel, sharedProgressionCommand, type ProgressionDraft } from './fields'

const dropdown: SettingFieldSlot = { field_id: 24, definition_id: 8, source: 'shared', label: '阵营', field_type: 'dropdown', base_value: null, value: { option_id: 4 }, options: [{ id: 3, definition_id: 8, label: '旧阵营', archived: true, sort: 0 }, { id: 4, definition_id: 8, label: '新阵营', archived: false, sort: 1 }], reference_types: [], reference: null, base_reference: null, archived: false, sort: 0 }
const reference: SettingFieldSlot = { ...dropdown, definition_id: 9, field_id: 25, field_type: 'reference', label: '所属组织', base_value: null, value: null, options: [], reference_types: ['organization'] }
const line: SettingFieldSlot = { ...dropdown, definition_id: 10, field_id: 26, field_type: 'line', label: '头衔', base_value: '', value: '', options: [] }
const text: SettingFieldSlot = { ...line, definition_id: 11, field_id: 27, field_type: 'text' }
const event: SettingProgression = { id: 6, timeline_id: 2, field_key: null, custom_field_id: 24, field_type: 'dropdown', definition_id: 8, operation: 'replace', value: { option_id: 3 }, anchor_kind: 'planned', chapter_id: null, chapter_no: 20, boundary: 'end', anchor_label: '', resolved_chapter_no: 20, orphaned: false, sort_order: 0, source: 'manual', protection: 'manual', source_change_id: null, source_content_hash: null, revision: 1, active: true, superseded_by_id: null, reverted_at: null, note: '', created_at: '', updated_at: '' }
const fields = fieldChoices('organization', [], [dropdown, reference, line, text])
const draft: ProgressionDraft = { field: 'definition:8', operation: 'replace', value: { option_id: 4 }, position: { anchor_kind: 'planned', chapter_id: null, chapter_no: 20, boundary: 'end' }, sort_order: 0, note: '' }

describe('四类字段事件草稿与原子首写', () => {
  it('空槽首事件保留目录身份和零时间线版本，不用定义ID冒充字段ID', () => {
    const empty = { ...dropdown, field_id: null }
    const choices = fieldChoices('organization', [], [empty])
    const input = buildProgressionInput({ ...draft, value: null }, 0, choices, 5)
    expect(input.ok).toBe(true)
    if (!input.ok) return
    expect(input.value.custom_field_id).toBeNull()
    expect(sharedProgressionCommand(input.value, empty)).toEqual({ definitionId: 8, body: {
      field_type: 'dropdown', value: null, expected_timeline_revision: 0, expected_fields_revision: 5,
      anchor_kind: 'planned', chapter_id: null, chapter_no: 20, boundary: 'end', operation: 'replace', sort_order: 0, note: '',
    } })
    expect(buildProgressionInput(draft, 1, choices).ok).toBe(false)
  })
  it('只有多行文本可追加，单行拒绝换行，替换明确允许清空', () => {
    for (const [field, value] of [['definition:8', null], ['definition:9', { kind: 'organization', id: 7 }], ['definition:10', '头衔']] as const) {
      expect(buildProgressionInput({ ...draft, field, value, operation: 'append' }, 2, fields, 5).ok).toBe(false)
    }
    expect(buildProgressionInput({ ...draft, field: 'definition:10', value: '换\n行' }, 2, fields, 5).ok).toBe(false)
    expect(buildProgressionInput({ ...draft, field: 'definition:11', value: '  新文本\n', operation: 'append' }, 2, fields, 5)).toMatchObject({ ok: true, value: { value: '  新文本\n', expected_fields_revision: 5 } })
  })
  it('编辑明确清空的选择值仍回填null，换字段不会带入上一个字段的对象', () => {
    expect(initialProgressionDraft({ ...event, value: null }, fields, []).value).toBeNull()
    expect(changeProgressionField(draft, fields[1])).toMatchObject({ field: 'definition:9', operation: 'replace', value: null })
    expect(changeProgressionField(draft, fields[2])).toMatchObject({ field: 'definition:10', value: '' })
  })
  it('停用旧值可原样调整备注；未改value时PATCH省略，清空时保留null', () => {
    const oldDraft = initialProgressionDraft(event, fields, [])
    const result = buildProgressionInput({ ...oldDraft, note: '只改备注' }, 2, fields, 5, { field_type: 'dropdown', value: { option_id: 3 } })
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(progressionUpdateInput(result.value, event, fields[0])).not.toHaveProperty('value')
    expect(progressionUpdateInput({ ...result.value, value: null }, event, fields[0])).toMatchObject({ expected_fields_revision: 5, value: null })
  })
  it('下拉和引用历史显示名称与状态，不显示JSON或内部句柄', () => {
    expect(progressionValueLabel(event, fields[0])).toBe('旧阵营（已停用）')
    expect(progressionValueLabel({ ...event, field_type: 'reference', custom_field_id: 25, value: { kind: 'organization', id: 7 }, reference_name: '青云宗', reference_type: 'organization' }, fields[1])).toBe('青云宗')
    expect(progressionValueLabel({ ...event, field_type: 'reference', value: { reference_handle_id: 7 } }, fields[1])).toBe('值暂不可用，请刷新字段配置')
  })
})
