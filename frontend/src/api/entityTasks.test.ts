import { describe, expect, it } from 'vitest'
import {
  careerSuggestionLimitError,
  careerSuggestionsToDrafts,
  parseCharacterGenerateResult,
  parseOrganizationGenerateResult,
  parseRelationsParseResult,
  suggestionsToBatch,
  suggestionsToDrafts,
  toEditable,
  toEditableCareers,
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

describe('职业建议', () => {
  const career = (over: Record<string, unknown> = {}) => ({
    career_name: '剑修',
    career_id: 1,
    career_kind: 'main',
    role: 'main',
    stage_name: '筑基',
    stage_id: 12,
    stage_position: 2,
    note: '瓶颈期',
    resolved: true,
    reason: '',
    ...over,
  })

  it('收窄：缺 career_id / stage_id 视为未解析；旧结果没有 career_suggestions 为空数组', () => {
    const r = parseCharacterGenerateResult({
      draft: { name: '林远' },
      career_suggestions: [career(), career({ career_id: null, career_kind: null, role: null, reason: '未找到' }), 'bad'],
    })
    expect(r?.career_suggestions).toHaveLength(2)
    expect(r?.career_suggestions[0]).toMatchObject({ career_id: 1, role: 'main', stage_id: 12, resolved: true })
    expect(r?.career_suggestions[1]).toMatchObject({ resolved: false, role: null, reason: '未找到' })
    expect(parseCharacterGenerateResult({ draft: { name: '林远' } })?.career_suggestions).toEqual([])
  })

  it('勾选的建议 → with-relations careers；超出主 1 副 3 给出提示', () => {
    const parsed = parseCharacterGenerateResult({
      draft: { name: '林远' },
      career_suggestions: [
        career(),
        career({ career_name: '丹师', career_id: 2, career_kind: 'sub', role: 'sub', stage_id: 21, note: '' }),
        career({ career_name: '无名', career_id: null, resolved: false }),
      ],
    })!
    const list = toEditableCareers(parsed.career_suggestions)
    expect(list[2].checked).toBe(false)
    expect(careerSuggestionsToDrafts(list)).toEqual([
      { career_id: 1, stage_id: 12, note: '瓶颈期' },
      { career_id: 2, stage_id: 21, note: '' },
    ])
    expect(careerSuggestionsToDrafts(list.map((c, i) => (i === 1 ? { ...c, checked: false } : c)))).toHaveLength(1)
    expect(careerSuggestionLimitError(list)).toBeNull()
    const twoMain = [...list, { ...list[0], key: 'x', career_id: 3 }]
    expect(careerSuggestionLimitError(twoMain)).toBe('该角色已有主职业')
    const subs = [1, 2, 3, 4].map((i) => ({ ...list[1], key: `s${i}`, career_id: 10 + i }))
    expect(careerSuggestionLimitError(subs)).toBe('副职业最多 3 个')
  })
})
