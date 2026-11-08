// Skill 输入表单：hook 的切换重置与载荷、表单必填标记、入口（局部改写）在必填缺失时阻止提交（node 环境，react-dom/server）
import { App } from 'antd'
import { createElement, type ReactElement } from 'react'
import { renderToString } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import type { SkillBrief, SkillInputDef } from '../../api/types'
import { skillFixture } from '../../utils/skills.test'
import { skillChoiceState } from '../../utils/skillInputs'

const tone: SkillInputDef = { name: 'tone', label: '基调', help: '整体情绪', kind: 'select', required: false, default: '轻松', options: ['轻松', '沉重'] }
const who: SkillInputDef = { name: '人物', label: '重点人物', help: '', kind: 'text', required: true, default: '', options: [] }
const note: SkillInputDef = { name: 'note', label: '备注', help: '', kind: 'textarea', required: false, default: '无', options: [] }

// zustand 在服务端渲染时读取初始状态，所以直接替换 store 模块
const seeded = vi.hoisted(() => ({ skills: [] as SkillBrief[] }))
vi.mock('../../stores/skills', () => ({
  useSkills: () => ({ skills: seeded.skills, loading: false, error: null }),
  useSkillStore: (select: (s: { load: () => Promise<void> }) => unknown) => select({ load: async () => {} }),
}))
seeded.skills = [
  skillFixture({ id: 2, name: '定基调', inputs: [tone, who] }),
  skillFixture({ id: 3, name: '另一个', inputs: [who, note] }),
  skillFixture({ id: 4, name: '无输入项' }),
]

const { useSkillChoice } = await import('../../hooks/useSkillChoice')
const { default: SkillInputsForm } = await import('./SkillInputsForm')
const { default: RewritePanel } = await import('../chapter/RewritePanel')

const render = (el: ReactElement) => renderToString(createElement(App, null, el)).replaceAll('<!-- -->', '')
const text = (html: string) => html.replace(/<[^>]+>/g, '')

/** 在服务端渲染中用渲染期更新驱动 hook：选 2 → 填写 → 切到 3，输出最终状态 */
function Probe({ steps }: { steps: Array<(c: ReturnType<typeof useSkillChoice>) => boolean> }) {
  const choice = useSkillChoice()
  for (const step of steps) if (step(choice)) return null
  return createElement('pre', null, JSON.stringify({ skillId: choice.skillId, values: choice.values, valid: choice.valid, payload: choice.payload }))
}
const probe = (steps: Array<(c: ReturnType<typeof useSkillChoice>) => boolean>) =>
  JSON.parse(text(renderToString(createElement(Probe, { steps }))).replaceAll('&quot;', '"'))

describe('useSkillChoice', () => {
  it('未选 Skill：payload 为空、可以提交', () => {
    expect(probe([])).toEqual({ skillId: null, values: {}, valid: true, payload: {} })
  })

  it('选中后以默认值预填；必填为空 valid=false；填写后 payload 带 skill_id 与 skill_inputs', () => {
    const selected = probe([(c) => { if (c.skillId === null) { c.setSkillId(2); return true } return false }])
    expect(selected.values).toEqual({ tone: '轻松', 人物: '' })
    expect(selected.valid).toBe(false)
    const filled = probe([
      (c) => { if (c.skillId === null) { c.setSkillId(2); return true } return false },
      (c) => { if (!c.values['人物']) { c.setInputs({ ...c.values, 人物: '张三', tone: '沉重' }); return true } return false },
    ])
    expect(filled).toEqual({ skillId: 2, values: { tone: '沉重', 人物: '张三' }, valid: true, payload: { skill_id: 2, skill_inputs: { tone: '沉重', 人物: '张三' } } })
  })

  it('切换 Skill 时清空上一个 Skill 的填写，用新 Skill 的默认值；没有输入项时只带 skill_id', () => {
    const switched = probe([
      (c) => { if (c.skillId === null) { c.setSkillId(2); return true } return false },
      (c) => { if (c.skillId === 2 && !c.values['人物']) { c.setInputs({ ...c.values, 人物: '张三' }); return true } return false },
      (c) => { if (c.skillId === 2) { c.setSkillId(3); return true } return false },
    ])
    expect(switched.values).toEqual({ 人物: '', note: '无' })
    expect(switched.valid).toBe(false)
    const plain = probe([(c) => { if (c.skillId === null) { c.setSkillId(4); return true } return false }])
    expect(plain.payload).toEqual({ skill_id: 4 })
  })
})

describe('SkillInputsForm', () => {
  it('按类型渲染，显示显示名、说明、必填标记与必填提示，带预览按钮', () => {
    const skill = seeded.skills[0]
    const html = render(createElement(SkillInputsForm, { skill, value: { tone: '轻松', 人物: '' }, onChange: () => {} }))
    const plain = text(html)
    expect(plain).toContain('基调')
    expect(plain).toContain('整体情绪')
    expect(plain).toContain('重点人物（必填）')
    expect(plain).toContain('必填项，请填写')
    expect(plain).toContain('预览填入后的 Skill 正文')
    expect(html).toContain('aria-required="true"')
  })

  it('没有输入项 / 未选 Skill 时不渲染', () => {
    expect(render(createElement(SkillInputsForm, { skill: seeded.skills[2], value: {}, onChange: () => {} }))).not.toContain('预览')
    expect(render(createElement(SkillInputsForm, { skill: null, value: {}, onChange: () => {} }))).not.toContain('预览')
  })
})

describe('入口：局部改写', () => {
  const props = {
    pid: 1, chapterId: 7, content: '前文。选中的一段。', selection: { start: 3, end: 8 }, aiConfigId: null, styleChoice: 'inherit' as const,
    disabled: false, ensureSaved: async () => true, onLockChange: () => {}, onApplied: () => {},
  }
  it('Skill 必填项未填时显示提示并禁用开始按钮；填好后可以开始', () => {
    const missing = skillChoiceState(seeded.skills, 2, {})
    const blocked = render(createElement(RewritePanel, { ...props, skill: missing }))
    expect(text(blocked)).toContain('请先填写 Skill「定基调」的输入项：重点人物')
    expect(blocked).toMatch(/<button[^>]*disabled=""[^>]*>(?:(?!<\/button>).)*改写选中内容/s)
    const ready = render(createElement(RewritePanel, { ...props, skill: skillChoiceState(seeded.skills, 2, { 人物: '张三' }) }))
    expect(text(ready)).not.toContain('请先填写 Skill')
  })
})
