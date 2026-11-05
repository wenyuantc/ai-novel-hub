import { describe, expect, it } from 'vitest'
import { ApiError } from '../../api/client'
import { buildProgressionInput, compareEditedEvent, compareTextField, fieldChoices, isStaleRevisionError, type ProgressionDraft } from './fields'

const fields = [{ id: 24, timeline_id: 3, label: '伤势', base_value: '无', sort: 0 }]
const draft: ProgressionDraft = {
  field: 'builtin:content', operation: 'append', value: '剑身出现裂痕', sort_order: 0, note: '',
  position: { anchor_kind: 'planned', chapter_id: null, chapter_no: 10, boundary: 'end' },
}

describe('变化字段与提交校验', () => {
  it('内置字段与自定义字段独立，字段改名不改变选择所用的稳定 ID', () => {
    expect(fieldChoices('world', fields)).toEqual([
      expect.objectContaining({ id: 'builtin:content', field_key: 'content', label: '内容', supportsAppend: true }),
      expect.objectContaining({ id: 'custom:24', field_key: null, custom_field_id: 24, label: '伤势', supportsAppend: true }),
    ])
    expect(fieldChoices('character', [{ ...fields[0], label: '伤口' }])).toContainEqual(
      expect.objectContaining({ id: 'custom:24', label: '伤口' }),
    )
  })

  it('按实际字段和章节提交单条变化，保留作者输入的文本', () => {
    const result = buildProgressionInput({ ...draft, field: 'custom:24', value: '  伤口加深\n' }, 7, fieldChoices('world', fields))
    expect(result).toEqual({ ok: true, value: {
      expected_revision: 7, field_key: null, custom_field_id: 24, operation: 'append', value: '  伤口加深\n',
      anchor_kind: 'planned', chapter_id: null, chapter_no: 10, boundary: 'end', sort_order: 0, note: '',
    } })
  })

  it('拒绝无效章号、全空白追加和对结构化状态的追加；允许替换为空', () => {
    const world = fieldChoices('world', [])
    expect(buildProgressionInput({ ...draft, position: { ...draft.position, chapter_no: null } }, 1, world).ok).toBe(false)
    expect(buildProgressionInput({ ...draft, value: ' \n ' }, 1, world).ok).toBe(false)
    expect(buildProgressionInput({ ...draft, operation: 'replace', value: '' }, 1, world).ok).toBe(true)
    const character = fieldChoices('character', [])
    expect(buildProgressionInput({ ...draft, field: 'builtin:life_status', value: 'alive' }, 1, character).ok).toBe(false)
    expect(buildProgressionInput({ ...draft, field: 'builtin:life_status', operation: 'replace', value: 'unknown' }, 1, character).ok).toBe(false)
  })

  it('拒绝不存在的字段、负数顺序和超长备注', () => {
    const world = fieldChoices('world', [])
    expect(buildProgressionInput({ ...draft, field: 'custom:24' }, 1, world).ok).toBe(false)
    expect(buildProgressionInput({ ...draft, sort_order: -1 }, 1, world).ok).toBe(false)
    expect(buildProgressionInput({ ...draft, note: '字'.repeat(201) }, 1, world).ok).toBe(false)
  })

  it('冲突刷新时按事件修订判断是否需要作者比较，已删除事件不能覆盖', () => {
    const original = { id: 6, revision: 2, active: true }
    expect(compareEditedEvent(original, { id: 6, revision: 2, active: true })).toBe('unchanged')
    expect(compareEditedEvent(original, { id: 6, revision: 3, active: true })).toBe('changed')
    expect(compareEditedEvent(original, { id: 6, revision: 3, active: false })).toBe('deleted')
    expect(compareEditedEvent(original, null)).toBe('deleted')
  })

  it('自定义字段冲突刷新时比较字段名与基础值，保留用户草稿', () => {
    const original = fields[0]
    expect(compareTextField(original, { ...original })).toBe('unchanged')
    expect(compareTextField(original, { ...original, base_value: '愈合' })).toBe('changed')
    expect(compareTextField(original, { ...original, label: '伤口' })).toBe('changed')
    expect(compareTextField(original, null)).toBe('deleted')
  })

  it('只有修订号过期的 409 触发刷新对照，其它 409 按普通错误展示', () => {
    expect(isStaleRevisionError(new ApiError(409, '设定已被其他操作修改，请刷新后重试', {
      code: 'stale_revision', detail: '设定已被其他操作修改，请刷新后重试',
    }))).toBe(true)
    // 分析来源事件改字段引用、写入冲突、生命周期冲突：刷新后重试必然再次失败，不能误判为版本过期
    for (const code of ['source_field_immutable', 'progression_conflict', 'progression_lifecycle_conflict']) {
      expect(isStaleRevisionError(new ApiError(409, '无法保存', { code }))).toBe(false)
    }
    expect(isStaleRevisionError(new ApiError(409, '无标记', null))).toBe(false)
    expect(isStaleRevisionError(new ApiError(400, '参数错误', { code: 'stale_revision' }))).toBe(false)
    expect(isStaleRevisionError(new Error('网络错误'))).toBe(false)
  })
})
