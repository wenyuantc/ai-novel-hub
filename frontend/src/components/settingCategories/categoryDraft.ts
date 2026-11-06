import { settingCategoryToInput } from '../../api/settingCategories'
import type { SettingCategory, SettingCategoryInput, SettingCategoryRuleInput, SettingCategorySpec, SettingCategoryUpdate, SettingEntryType, SettingFieldDefinition } from '../../api/types'
import { normalizeSettingTags, stripSettingWhitespace } from '../settingTags/tagDraft'

export type CategoryRuleDraft = { key: string; sort: number } & (
  | { kind: 'tag'; tag: string }
  | { kind: 'dropdown'; definition_id: number | null; option_id: number | null }
)
export interface CategoryDraft { name: string; applicable_types: SettingEntryType[]; match_mode: 'all' | 'any'; sort: number | null; rules: CategoryRuleDraft[] }
export interface CategoryIssue { message: string; ruleIndex?: number }
export type CategoryInputResult = { ok: true; input: SettingCategoryInput } | { ok: false; issues: CategoryIssue[] }
const validSort = (sort: number | null): sort is number => sort !== null && Number.isInteger(sort) && sort >= -2_147_483_648 && sort <= 2_147_483_647

export function categoryDraft(value: SettingCategory | SettingCategorySpec | null): CategoryDraft {
  return value ? { name: value.name, applicable_types: [...value.applicable_types], match_mode: value.match_mode, sort: value.sort,
    rules: value.rules.map((rule, index) => ({ ...rule, key: 'id' in rule ? `saved-${rule.id}` : `rule-${index}` })) }
    : { name: '', applicable_types: [], match_mode: 'all', sort: 0, rules: [{ key: 'first', kind: 'tag', tag: '', sort: 0 }] }
}

export function retainedDropdown(rule: { definition_id: number | null; option_id: number | null }, original: SettingCategory | null): boolean {
  return !!original?.rules.some((saved) => saved.kind === 'dropdown' && saved.definition_id === rule.definition_id && saved.option_id === rule.option_id)
}

/** 所有写入都从草稿重建明确载荷；数据库规则行ID永不进入payload。 */
export function categoryDraftInput(draft: CategoryDraft, definitions: readonly SettingFieldDefinition[], original: SettingCategory | null): CategoryInputResult {
  const issues: CategoryIssue[] = []
  const name = stripSettingWhitespace(draft.name)
  if (!name || [...name].length > 100) issues.push({ message: '分类名称须为 1–100 字' })
  if (!draft.applicable_types.length) issues.push({ message: '请选择至少一种适用类型' })
  if (!validSort(draft.sort)) issues.push({ message: '显示顺序必须是有效整数' })
  if (!draft.rules.length || draft.rules.length > 20) issues.push({ message: '分类须包含 1–20 条条件' })
  const rules: SettingCategoryRuleInput[] = []
  draft.rules.forEach((rule, ruleIndex) => {
    const fail = (message: string) => { issues.push({ message, ruleIndex }) }
    if (!validSort(rule.sort)) { fail('条件顺序必须是有效整数'); return }
    if (rule.kind === 'tag') {
      try {
        const tag = normalizeSettingTags([rule.tag])[0]
        if (!tag) fail('标签条件须为 1–50 字')
        else rules.push({ kind: 'tag', tag, sort: rule.sort })
      } catch { fail('标签条件须为 1–50 字') }
      return
    }
    const field = definitions.find((item) => item.id === rule.definition_id)
    const option = field?.options.find((item) => item.id === rule.option_id)
    if (!field || field.field_type !== 'dropdown') { fail('请选择可用的下拉字段'); return }
    if (!draft.applicable_types.some((type) => field.applicable_types.includes(type))) { fail('分类与条件字段没有共同适用类型'); return }
    if (!option) { fail('请选择这个字段的具体选项'); return }
    if ((field.archived || option.archived) && !retainedDropdown(rule, original)) { fail('新条件不能选择已停用的字段或选项'); return }
    rules.push({ kind: 'dropdown', definition_id: field.id, option_id: option.id, sort: rule.sort })
  })
  return issues.length ? { ok: false, issues } : { ok: true, input: { name, applicable_types: [...draft.applicable_types], match_mode: draft.match_mode, sort: draft.sort ?? 0, rules } }
}

/** 无变化的rules绝不重发，避免只改名字或顺序时替换规则行身份。 */
export function categoryPatch(original: SettingCategory, input: SettingCategoryInput): Omit<SettingCategoryUpdate, 'expected_categories_revision' | 'expected_fields_revision'> {
  const before = settingCategoryToInput(original)
  return { ...(input.name !== before.name ? { name: input.name } : {}),
    ...(JSON.stringify(input.applicable_types) !== JSON.stringify(before.applicable_types) ? { applicable_types: input.applicable_types } : {}),
    ...(input.match_mode !== before.match_mode ? { match_mode: input.match_mode } : {}),
    ...(input.sort !== before.sort ? { sort: input.sort } : {}),
    ...(JSON.stringify(input.rules) !== JSON.stringify(before.rules) ? { rules: input.rules } : {}) }
}

export function categoryDirty(draft: CategoryDraft, original: SettingCategory | null): boolean { return JSON.stringify(draft) !== JSON.stringify(categoryDraft(original)) }
export function moveCategoryRule(rules: readonly CategoryRuleDraft[], index: number, direction: -1 | 1): CategoryRuleDraft[] {
  const next = [...rules]
  if (index + direction < 0 || index + direction >= next.length) return next
  ;[next[index], next[index + direction]] = [next[index + direction], next[index]]
  return next.map((rule, sort) => ({ ...rule, sort }))
}
export function ruleDescription(rule: CategoryRuleDraft, definitions: readonly SettingFieldDefinition[]): string {
  if (rule.kind === 'tag') return `整理标签包含「${rule.tag || '未填写'}」`
  const field = definitions.find((item) => item.id === rule.definition_id)
  const option = field?.options.find((item) => item.id === rule.option_id)
  return `${field?.label ?? '原字段不可用'} = ${option?.label ?? '尚未选择选项'}${field?.archived || option?.archived ? '（已停用）' : ''}`
}
