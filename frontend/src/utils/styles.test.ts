import { describe, expect, it } from 'vitest'
import { ApiError } from '../api/client'
import type { StyleBrief, WritingStyle } from '../api/types'
import {
  describeInherit,
  draftToStyleBody,
  groupStyles,
  readStyleReferences,
  referencesText,
  resolveStyleChoice,
  sampleLengthHint,
  styleChoiceLabel,
  styleChoiceOptions,
  summarizeStyleImport,
} from './styles'

function style(id: number, name: string, isPreset = false): WritingStyle {
  return {
    id,
    name,
    is_preset: isPreset,
    preset_key: isPreset ? `p${id}` : null,
    sort: 0,
    references: { projects: 0, chapters: 0 },
    created_at: '2026-10-26T00:00:00',
    updated_at: '2026-10-26T00:00:00',
    description: '',
    pov: '',
    rhythm: '',
    dialogue: '',
    description_density: '',
    tone: '',
    diction: '',
    taboos: '',
    sample: '',
    extra: '',
  }
}

const styles = [style(1, '冷峻简洁', true), style(2, '细腻抒情', true), style(10, '我的风格')]
const projectDefault: StyleBrief = { id: 2, name: '细腻抒情', description: '', is_preset: true }

describe('风格解析优先级（临时 > 章节 > 项目默认 > 无）', () => {
  it('临时指定优先', () => {
    expect(resolveStyleChoice(10, { chapterStyleId: 1, projectDefault, styles })).toEqual({
      id: 10,
      name: '我的风格',
      source: 'request',
    })
  })
  it('继承：章节覆盖 > 项目默认 > 不用风格', () => {
    expect(resolveStyleChoice('inherit', { chapterStyleId: 1, projectDefault, styles }).source).toBe('chapter')
    expect(resolveStyleChoice('inherit', { chapterStyleId: null, projectDefault, styles })).toEqual({
      id: 2,
      name: '细腻抒情',
      source: 'project',
    })
    expect(resolveStyleChoice('inherit', { chapterStyleId: null, projectDefault: null, styles }).source).toBe('none')
  })
  it('none 直接不用风格（即使有章节与项目默认）', () => {
    expect(resolveStyleChoice('none', { chapterStyleId: 1, projectDefault, styles })).toEqual({
      id: null,
      name: null,
      source: 'none',
    })
  })
})

describe('展示文案', () => {
  it('继承显示实际会用的风格', () => {
    expect(describeInherit({ chapterStyleId: 1, projectDefault, styles })).toBe('本章风格「冷峻简洁」')
    expect(describeInherit({ projectDefault, styles })).toBe('项目默认「细腻抒情」')
    expect(describeInherit({})).toBe('不用风格')
    expect(styleChoiceLabel('inherit', { projectDefault })).toBe('继承（项目默认「细腻抒情」）')
    expect(styleChoiceLabel('none', { projectDefault })).toBe('不用风格')
    expect(styleChoiceLabel(10, { styles })).toBe('我的风格')
    expect(styleChoiceLabel(99, { styles })).toBe('风格 #99')
  })

  it('下拉：继承 + 不用风格 + 我的 / 预设分组；批量可替换继承文案', () => {
    const opts = styleChoiceOptions(styles, { chapterStyleId: 10 })
    expect(opts[0]).toEqual({ value: 'inherit', label: '继承（本章风格「我的风格」）' })
    expect(opts[1]).toEqual({ value: 'none', label: '不用风格' })
    expect(opts.slice(2).map((g) => ('options' in g ? [g.label, g.options.map((o) => o.value)] : null))).toEqual([
      ['我的风格', [10]],
      ['预设风格', [1, 2]],
    ])
    expect(styleChoiceOptions([], {}, '按章节或项目默认')[0]).toEqual({ value: 'inherit', label: '按章节或项目默认' })
  })

  it('分组保持顺序', () => {
    const g = groupStyles(styles)
    expect(g.presets.map((s) => s.id)).toEqual([1, 2])
    expect(g.mine.map((s) => s.id)).toEqual([10])
  })
})

describe('引用与导入', () => {
  it('引用数文案', () => {
    expect(referencesText({ projects: 2, chapters: 3 })).toBe('2 个项目、3 个章节')
    expect(referencesText({ projects: 0, chapters: 1 })).toBe('1 个章节')
    expect(referencesText({ projects: 0, chapters: 0 })).toBe('')
    expect(referencesText(null)).toBe('')
  })

  it('从删除 409 响应体读取引用数', () => {
    const err = new ApiError(409, '该风格被引用', { detail: 'x', references: { projects: 1, chapters: 4 } })
    expect(readStyleReferences(err)).toEqual({ projects: 1, chapters: 4 })
    expect(readStyleReferences(new ApiError(409, 'x'))).toBeNull()
    expect(readStyleReferences(new ApiError(400, 'x', { references: { projects: 1 } }))).toBeNull()
    expect(readStyleReferences(new Error('x'))).toBeNull()
  })

  it('导入结果汇总', () => {
    expect(
      summarizeStyleImport({
        items: [
          { id: 1, name: 'A', renamed_from: null },
          { id: 2, name: 'B（导入）', renamed_from: 'B' },
        ],
        warnings: ['第 3 项缺少名称，已跳过'],
      }),
    ).toBe('已导入 2 个风格（1 个因重名已改名）；1 条提示')
    expect(summarizeStyleImport({ items: [], warnings: [] })).toBe('已导入 0 个风格')
  })
})

describe('表单', () => {
  it('提交前 trim 全部字段', () => {
    const body = draftToStyleBody({ ...style(0, '  名 '), tone: ' 冷 ' })
    expect(body.name).toBe('名')
    expect(body.tone).toBe('冷')
    expect(body.extra).toBe('')
  })
  it('示例段落字数按码点计', () => {
    expect(sampleLengthHint('𠮷ab', 2)).toEqual({ text: '3 / 2 字', over: true })
    expect(sampleLengthHint('')).toEqual({ text: '0 / 2000 字', over: false })
  })
})
