import { describe, expect, it } from 'vitest'
import type { SkillInputDef } from '../api/types'
import {
  inputDefErrors, inputProblem, inputWarnings, missingRequired, normalizeInputDefs, placeholders, skillChoiceProblemText, skillChoiceState,
} from './skillInputs'
import { skillFixture } from './skills.test'

const def = (over: Partial<SkillInputDef>): SkillInputDef => ({ name: 'x', label: 'X', help: '', kind: 'text', required: false, default: '', options: [], ...over })

describe('Skill 正文占位', () => {
  it('只识别 {{ 标识 }}（含汉字标识），不含组件引用与不合法写法，去重保序', () => {
    expect(placeholders('写 {{ tone }}，{{人物}}，再写{{ tone }}；{{> banned }}、{{ 1x }}、{{ a.b }}')).toEqual(['tone', '人物'])
    expect(placeholders('没有占位')).toEqual([])
  })
  it('未声明的占位与未使用的输入项都给出警告（与后端文案一致）', () => {
    expect(inputWarnings([{ name: 'tone' }, { name: 'who' }], '{{ tone }} {{ other }} {{ a }}')).toEqual([
      '正文中的 {{ a }} 没有声明为输入项，将原样保留',
      '正文中的 {{ other }} 没有声明为输入项，将原样保留',
      '输入项「who」没有在正文中引用',
    ])
    expect(inputWarnings([{ name: 'tone' }], '基调：{{ tone }}')).toEqual([])
    // 草稿里尚未填写的标识不参与
    expect(inputWarnings([{ name: '  ' }], '正文')).toEqual([])
  })
})

describe('输入值校验与载荷', () => {
  const skills = [
    skillFixture({ id: 2, name: '定基调', inputs: [def({ name: 'tone', label: '基调', kind: 'select', default: '轻松', options: ['轻松', '沉重'] }), def({ name: 'who', label: '重点人物', required: true })] }),
    skillFixture({ id: 4, name: '无输入项' }),
  ]
  it('必填、超长、下拉值不在选项中', () => {
    expect(inputProblem(def({ required: true }), '  ')).toBe('必填')
    expect(inputProblem(def({}), '')).toBeNull()
    expect(inputProblem(def({}), 'a'.repeat(201))).toBe('不能超过 200 字')
    expect(inputProblem(def({ kind: 'textarea' }), 'a'.repeat(201))).toBeNull()
    expect(inputProblem(def({ kind: 'select', options: ['甲', '乙'] }), '丙')).toBe('取值不在选项中')
    expect(missingRequired(skills[0].inputs, { tone: '轻松', who: '' }).map((d) => d.name)).toEqual(['who'])
  })
  it('未选 Skill 不带任何字段；有输入项时带 skill_inputs（默认值补齐、忽略未声明的值）', () => {
    expect(skillChoiceState(skills, null, { who: 'a' })).toMatchObject({ valid: true, payload: {} })
    const missing = skillChoiceState(skills, 2, {})
    expect(missing.valid).toBe(false)
    expect(missing.problems).toEqual({ who: '必填' })
    expect(skillChoiceProblemText(missing)).toBe('请先填写 Skill「定基调」的输入项：重点人物')
    const ok = skillChoiceState(skills, 2, { who: '张三', stale: '旧' })
    expect(ok.valid).toBe(true)
    expect(skillChoiceProblemText(ok)).toBeNull()
    expect(ok.payload).toEqual({ skill_id: 2, skill_inputs: { tone: '轻松', who: '张三' } })
    expect(skillChoiceState(skills, 4, {}).payload).toEqual({ skill_id: 4 })
    // 列表里找不到（未加载 / 已删除）：只带 skill_id，由服务端校验
    expect(skillChoiceState(skills, 99, {})).toMatchObject({ valid: true, payload: { skill_id: 99 } })
  })
})

describe('输入项编辑器', () => {
  it('校验与后端一致：重复标识、下拉选项数量、默认值须是选项之一、默认值长度', () => {
    expect(inputDefErrors([def({ name: 'a' }), def({ name: 'a ' })])).toEqual(['输入项标识重复：a'])
    expect(inputDefErrors([def({ label: '基调', kind: 'select', options: ['轻松'] })])).toEqual(['下拉输入项「基调」须有 2–50 个选项'])
    expect(inputDefErrors([def({ label: '基调', kind: 'select', options: ['轻松', '沉重'], default: '欢快' })])).toEqual(['下拉输入项「基调」的默认值须是选项之一'])
    expect(inputDefErrors([def({ label: 'X', default: 'a'.repeat(201) })])).toEqual(['输入项「X」的默认值不超过 200 字'])
    expect(inputDefErrors(Array.from({ length: 21 }, (_, i) => def({ name: `n${i}` })))).toContain('输入项最多 20 个')
  })
  it('保存前去空白、非下拉清空选项、下拉选项去重', () => {
    expect(normalizeInputDefs([{ name: ' a ', label: ' A ', kind: 'text', options: ['x'] }, { name: 'b', label: 'B', kind: 'select', options: [' 甲', '甲', '乙', ''] }])).toEqual([
      { name: 'a', label: 'A', help: '', kind: 'text', required: false, default: '', options: [] },
      { name: 'b', label: 'B', help: '', kind: 'select', required: false, default: '', options: ['甲', '乙'] },
    ])
  })
})
