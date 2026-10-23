import { describe, expect, it } from 'vitest'
import {
  parseCharacterGenerateResult,
  parseOrganizationGenerateResult,
  parseRelationsParseResult,
  suggestionsToBatch,
  suggestionsToDrafts,
  toEditable,
} from './entityTasks'
import type { RelationSuggestion } from './types'

const sug = (over: Partial<RelationSuggestion> = {}): RelationSuggestion => ({
  target_name: '张三',
  target_id: 5,
  resolved: true,
  reason: '',
  existing_relationship_id: null,
  self_role: '徒弟',
  target_role: '师父',
  subject_is_a: false,
  type: { type_id: 9, role_a: '师父', role_b: '徒弟', category: 'social' },
  intimacy: 60,
  status: 'normal',
  description: '授业恩师',
  ...over,
})

describe('结果收窄', () => {
  it('角色结果：缺字段补默认，未解析的建议不可勾选', () => {
    const r = parseCharacterGenerateResult({
      draft: { name: '林远', aliases: ['小远', 3] },
      relation_suggestions: [sug(), { ...sug(), target_id: null, resolved: true, reason: '未找到' }, 'bad'],
      warnings: ['w'],
    })
    expect(r?.draft).toMatchObject({ name: '林远', aliases: ['小远'], life_status: 'alive', always_in_context: false })
    expect(r?.relation_suggestions).toHaveLength(2)
    expect(r?.relation_suggestions[1].resolved).toBe(false)
    expect(r?.warnings).toEqual(['w'])
    expect(parseCharacterGenerateResult({ draft: {} })).toBeNull()
    expect(parseCharacterGenerateResult(null)).toBeNull()
  })

  it('组织结果与解析结果', () => {
    const o = parseOrganizationGenerateResult({
      draft: { name: '天机阁', power_level: 70, parent_name: '', parent_id: null },
      member_suggestions: [{ character_name: '林远', character_id: 1, position: '阁主', resolved: true }],
    })
    expect(o?.draft).toMatchObject({ name: '天机阁', power_level: 70, parent_name: null })
    expect(o?.member_suggestions[0]).toMatchObject({ character_id: 1, resolved: true })
    const p = parseRelationsParseResult({ character_id: 3, character_name: '林远', suggestions: [sug()] })
    expect(p?.suggestions).toHaveLength(1)
    expect(parseRelationsParseResult({ suggestions: [] })).toBeNull()
  })
})

describe('建议 → 保存载荷', () => {
  it('解析结果按 subject_is_a 决定 a/b（反向匹配：主体是 b）', () => {
    const list = toEditable([sug(), sug({ target_name: '李四', target_id: null, resolved: false })])
    expect(list[1].checked).toBe(false)
    const items = suggestionsToBatch(3, list)
    expect(items).toEqual([
      {
        a_id: 5,
        b_id: 3,
        type_id: 9,
        role_a: '师父',
        role_b: '徒弟',
        category: 'social',
        intimacy: 60,
        status: 'normal',
        description: '授业恩师',
        source: 'ai_parse',
      },
    ])
  })

  it('改了称呼：丢弃 type_id，主体作为 role_a 一方提交', () => {
    const [s] = toEditable([sug()])
    const edited = { ...s, edit_self_role: '义子', edit_target_role: '义父' }
    expect(suggestionsToBatch(3, [edited])[0]).toMatchObject({ a_id: 3, b_id: 5, type_id: null, role_a: '义子', role_b: '义父' })
    expect(suggestionsToDrafts([edited])[0]).toMatchObject({ target_id: 5, subject_is_a: true, type_id: null, role_a: '义子' })
  })

  it('未勾选 / 称呼为空不提交；新类型（type_id 为空）按主体为 a', () => {
    const [s] = toEditable([sug({ type: { type_id: null, role_a: '徒弟', role_b: '师父', category: 'xx' }, subject_is_a: true })])
    expect(suggestionsToDrafts([{ ...s, checked: false }])).toEqual([])
    expect(suggestionsToDrafts([{ ...s, edit_self_role: ' ' }])).toEqual([])
    expect(suggestionsToDrafts([s])[0]).toMatchObject({ subject_is_a: true, role_a: '徒弟', role_b: '师父', category: 'other' })
  })
})
