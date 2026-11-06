import { describe, expect, it } from 'vitest'
import type { SettingCategory, SettingFieldDefinition } from '../../api/types'
import { categoryDraft, categoryDraftInput, categoryPatch, moveCategoryRule, ruleDescription } from './categoryDraft'

const field: SettingFieldDefinition = { id: 3, project_id: 1, label: '阵营', field_type: 'dropdown', applicable_types: ['character'], reference_types: [], sort: 0, archived: false, ai_hidden: false, options: [{ id: 7, definition_id: 3, label: '同盟', sort: 0, archived: false }] }
const category: SettingCategory = { id: 4, project_id: 1, name: '同盟', applicable_types: ['character'], match_mode: 'all', sort: 0,
  rules: [{ id: 8, category_id: 4, kind: 'dropdown', definition_id: 3, option_id: 7, sort: 12 }] }

describe('分类规则编辑草稿', () => {
  it('分类名称与服务端去空白口径一致，改顺序不隐式改写合法BOM名称', () => {
    const original = { ...category, name: '\ufeff同盟\ufeff' }
    const draft = categoryDraft(original)
    draft.sort = 5
    const result = categoryDraftInput(draft, [field], original)
    if (!result.ok) throw new Error('valid category expected')
    expect(result.input.name).toBe('\ufeff同盟\ufeff')
    expect(categoryPatch(original, result.input)).toEqual({ sort: 5 })
    draft.name = '\u0085同盟\u001c'
    expect(categoryDraftInput(draft, [field], original)).toMatchObject({ ok: true, input: { name: '同盟' } })
    draft.name = '\u0085\u001c'
    expect(categoryDraftInput(draft, [field], original)).toMatchObject({ ok: false, issues: [{ message: '分类名称须为 1–100 字' }] })
  })
  it('标签条件沿用条目标签的Unicode规范化，名称修改不重写合法BOM身份的规则', () => {
    const original: SettingCategory = { ...category, rules: [{ id: 9, category_id: 4, kind: 'tag', tag: '\ufeff门派\ufeff', sort: 0 }] }
    const draft = categoryDraft(original)
    draft.name = '改名后分类'
    const result = categoryDraftInput(draft, [], original)
    expect(result).toMatchObject({ ok: true, input: { rules: [{ kind: 'tag', tag: '\ufeff门派\ufeff', sort: 0 }] } })
    if (!result.ok) throw new Error('valid tag expected')
    expect(categoryPatch(original, result.input)).toEqual({ name: '改名后分类' })
    draft.rules = [{ key: 'tag', kind: 'tag', tag: '\u0085门派\u001c', sort: 0 }]
    expect(categoryDraftInput(draft, [], original)).toMatchObject({ ok: true, input: { rules: [{ kind: 'tag', tag: '门派', sort: 0 }] } })
    draft.rules = [{ key: 'tag', kind: 'tag', tag: '\u0085\u001c', sort: 0 }]
    expect(categoryDraftInput(draft, [], original)).toMatchObject({ ok: false, issues: [{ ruleIndex: 0 }] })
  })
  it('输出只含业务条件，无规则行ID；只改名称/顺序的PATCH省略rules以保留原身份', () => {
    const draft = categoryDraft(category)
    draft.name = '  同盟成员  '
    draft.sort = 5
    const result = categoryDraftInput(draft, [field], category)
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('valid draft expected')
    expect(result.input.rules).toEqual([{ kind: 'dropdown', definition_id: 3, option_id: 7, sort: 12 }])
    expect(categoryPatch(category, result.input)).toEqual({ name: '同盟成员', sort: 5 })
    expect(category.name).toBe('同盟')
  })
  it('空规则/适用类型/名称和超限标签在提交前定位，缺选项不自动猜测', () => {
    const draft = categoryDraft(null)
    expect(categoryDraftInput(draft, [], null)).toMatchObject({ ok: false })
    draft.name = '同盟'; draft.applicable_types = ['character']; draft.rules = [{ key: 'a', kind: 'dropdown', definition_id: 3, option_id: null, sort: 0 }]
    expect(categoryDraftInput(draft, [field], null)).toMatchObject({ ok: false, issues: [{ ruleIndex: 0 }] })
    draft.rules = [{ key: 'b', kind: 'tag', tag: '😀'.repeat(51), sort: 0 }]
    expect(categoryDraftInput(draft, [], null)).toMatchObject({ ok: false, issues: [{ ruleIndex: 0 }] })
    draft.rules = []
    expect(categoryDraftInput(draft, [], null)).toMatchObject({ ok: false })
  })
  it('旧停用身份只可保留同一字段/选项，新条件或跨组选项不能借同名绕过', () => {
    const archived = { ...field, archived: true, options: field.options.map((item) => ({ ...item, archived: true })) }
    expect(categoryDraftInput(categoryDraft(category), [archived], category).ok).toBe(true)
    expect(categoryDraftInput(categoryDraft(category), [archived], null)).toMatchObject({ ok: false, issues: [{ ruleIndex: 0 }] })
    const draft = categoryDraft(category)
    if (draft.rules[0].kind !== 'dropdown') throw new Error('dropdown expected')
    draft.rules[0].option_id = 99
    expect(categoryDraftInput(draft, [field], category)).toMatchObject({ ok: false, issues: [{ ruleIndex: 0 }] })
    expect(ruleDescription(draft.rules[0], [field])).not.toContain('99')
  })
  it('类型不相交拒绝；规则重排写规范sort但不改其他草稿', () => {
    const draft = categoryDraft(category)
    draft.applicable_types = ['organization']
    expect(categoryDraftInput(draft, [field], category)).toMatchObject({ ok: false, issues: [{ ruleIndex: 0 }] })
    draft.rules.push({ key: 'tag', kind: 'tag', tag: '同行', sort: 20 })
    const moved = moveCategoryRule(draft.rules, 1, -1)
    expect(moved.map((rule) => [rule.key, rule.sort])).toEqual([['tag', 0], ['saved-8', 1]])
    expect(draft.rules[0].sort).toBe(12)
  })
})
