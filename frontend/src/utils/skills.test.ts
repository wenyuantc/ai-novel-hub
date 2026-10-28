import { describe, expect, it } from 'vitest'
import type { SkillBrief } from '../api/types'
import { matchTrigger, normalizeTriggers, skillRef, skillSelectOptions, slashQuery, splitSkills, suggestSlug, triggerOptions } from './skills'

export const skillFixture = (over: Partial<SkillBrief> = {}): SkillBrief => ({
  id: 1, scope: 'mine', slug: 'long-continue', name: '长篇续写', description: '续写规范', category: '续写', triggers: ['续写'],
  builtin_key: null, source_skill_id: null, editable: true, body_chars: 100, reference_titles: [], created_at: '', updated_at: '', ...over,
})
const skills = [
  skillFixture({ id: 10, scope: 'global', name: '全局续写', triggers: ['续写', 'XuXie'], editable: false }),
  skillFixture({ id: 2, name: '我的续写', triggers: ['续写', '续写长篇'] }),
  skillFixture({ id: 3, name: '去 AI 味', triggers: ['deslop'] }),
]

describe('Skill `/` 补全', () => {
  it('只在开头的 /前缀（未输入空白时）补全', () => {
    expect(slashQuery('/')).toBe('')
    expect(slashQuery('/续')).toBe('续')
    expect(slashQuery('/续写 帮我')).toBeNull()
    expect(slashQuery('帮我 /续写')).toBeNull()
    expect(triggerOptions('帮我', skills)).toEqual([])
  })
  it('前缀匹配、大小写不敏感，同一触发词我的优先且只出现一次', () => {
    const all = triggerOptions('/', skills)
    expect(all.map((o) => [o.trigger, o.skill.id])).toEqual([['续写', 2], ['续写长篇', 2], ['deslop', 3], ['XuXie', 10]])
    expect(triggerOptions('/续写', skills).map((o) => o.value)).toEqual(['/续写 ', '/续写长篇 '])
    expect(triggerOptions('/xux', skills).map((o) => o.skill.id)).toEqual([10])
    expect(triggerOptions('/', skills, 2)).toHaveLength(2)
  })
  it('匹配与后端一致：开头 /词，后接空白或结束；不做子串匹配；我的优先', () => {
    expect(matchTrigger('/续写 第三章', skills)?.id).toBe(2)
    expect(matchTrigger('/续写', skills)?.id).toBe(2)
    expect(matchTrigger('/XUXIE', skills)?.id).toBe(10)
    expect(matchTrigger('/续', skills)).toBeNull()
    expect(matchTrigger('/续写abc', skills)).toBeNull()
    expect(matchTrigger('请 /续写', skills)).toBeNull()
  })
})

describe('Skill 工具函数', () => {
  it('分组与选项', () => {
    expect(splitSkills(skills).mine.map((s) => s.id)).toEqual([2, 3])
    expect(skillSelectOptions(skills).map((g) => g.label)).toEqual(['我的', '全局'])
    expect(skillSelectOptions([])).toEqual([])
    // 副本与全局 Skill 同名时靠标识区分
    const labels = skillSelectOptions(skills).flatMap((g) => g.options.map((o) => o.label))
    expect(labels.every((l) => /（[a-z0-9-]+）$/.test(l))).toBe(true)
  })
  it('触发词去 /、空白与重复；标识建议；记录收窄', () => {
    expect(normalizeTriggers(['/续写', ' 续写 ', 'A', 'a', '两 个', ''])).toEqual(['续写', 'A'])
    expect(suggestSlug('Long Continue!')).toBe('long-continue')
    expect(suggestSlug('长篇续写')).toBe('')
    expect(skillRef({ id: 1, name: 'x', body: '...' })).toEqual({ id: 1, name: 'x' })
    expect(skillRef({ id: '1', name: 'x' })).toBeNull()
    expect(skillRef(null)).toBeNull()
  })
})
