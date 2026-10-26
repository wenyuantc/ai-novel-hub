import { describe, expect, it } from 'vitest'
import type { InspirationApplyPreviewItem, InspirationState, InspirationStepKey, InspirationStepState } from '../api/types'
import {
  applyUnchanged,
  applyValueText,
  defaultApplyChecked,
  defaultApplyFields,
  draftSummary,
  handledCount,
  nextFlowStep,
  roundHalfEven,
  shouldSuggestGenerate,
  staleSummary,
  staleText,
  stepDisplayStatus,
  suggestScale,
  tagsSource,
  toScale,
  initialScaleValues,
} from './inspiration'

function step(over: Partial<InspirationStepState> = {}): InspirationStepState {
  return { value: null, status: 'pending', stale: false, source: null, rounds: [], ...over }
}

function makeState(steps: Partial<Record<InspirationStepKey, Partial<InspirationStepState>>> = {}, over: Partial<InspirationState> = {}): InspirationState {
  const keys: InspirationStepKey[] = ['title', 'synopsis', 'theme', 'tags', 'selling_points', 'audience']
  return {
    idea: '一个想法',
    notes: [],
    genre: '',
    scale: null,
    current: 'idea',
    steps: Object.fromEntries(keys.map((k) => [k, step(steps[k])])) as Record<InspirationStepKey, InspirationStepState>,
    ...over,
  }
}

describe('步骤状态推导', () => {
  it('AI 步骤：已定 / 已跳过 / 可能需要更新 / 待定', () => {
    const s = makeState({
      title: { status: 'decided', value: '星河', source: 'ai' },
      synopsis: { status: 'decided', value: '简介', stale: true },
      theme: { status: 'skipped' },
    })
    expect(stepDisplayStatus(s, 'title')).toBe('decided')
    expect(stepDisplayStatus(s, 'synopsis')).toBe('stale')
    expect(stepDisplayStatus(s, 'theme')).toBe('skipped')
    expect(stepDisplayStatus(s, 'tags')).toBe('pending')
    expect(handledCount(s)).toBe(3)
  })

  it('想法 / 规模 / 确认', () => {
    expect(stepDisplayStatus(makeState({}, { idea: '  ' }), 'idea')).toBe('pending')
    expect(stepDisplayStatus(makeState(), 'idea')).toBe('decided')
    expect(stepDisplayStatus(makeState(), 'scale')).toBe('pending')
    const scale = { target_words: 300000, volume_count: 4, chapters_per_volume: 25, character_count: 13 }
    expect(stepDisplayStatus(makeState({}, { scale }), 'scale')).toBe('decided')
    expect(stepDisplayStatus(makeState({}, { scale }), 'confirm')).toBe('pending')
  })

  it('stale 文案与汇总', () => {
    expect(staleText('synopsis')).toContain('「简介」')
    expect(staleText('synopsis')).toContain('保持不变')
    expect(staleSummary(makeState())).toBe('')
    const s = makeState({
      synopsis: { status: 'decided', value: 'a', stale: true },
      tags: { status: 'decided', value: ['x'], stale: true },
    })
    expect(staleSummary(s)).toBe('「简介」「类型标签」可能需要更新')
  })

  it('只对待定且没生成过的步骤建议生成（补全场景已定的步骤不自动建议）', () => {
    const s = makeState({
      title: { status: 'decided', value: '来自项目', source: 'project' },
      synopsis: { rounds: [{ options: ['a'], feedback: '', created_at: '' }] },
      theme: { status: 'skipped' },
    })
    expect(shouldSuggestGenerate(s, 'title')).toBe(false)
    expect(shouldSuggestGenerate(s, 'synopsis')).toBe(false)
    expect(shouldSuggestGenerate(s, 'theme')).toBe(false)
    expect(shouldSuggestGenerate(s, 'tags')).toBe(true)
  })

  it('前进到下一个待定或可能需要更新的步骤，都处理过进入规模', () => {
    const s = makeState({
      title: { status: 'decided', value: 't' },
      synopsis: { status: 'decided', value: 's' },
      theme: { status: 'decided', value: 'th', stale: true },
      tags: { status: 'skipped' },
    })
    expect(nextFlowStep(s, 'idea')).toBe('theme')
    expect(nextFlowStep(s, 'theme')).toBe('selling_points')
    expect(nextFlowStep(s, 'audience')).toBe('scale')
    expect(nextFlowStep(s, 'scale')).toBe('confirm')
    expect(nextFlowStep(s, 'confirm')).toBe('confirm')
    const all = makeState(
      Object.fromEntries(
        (['title', 'synopsis', 'theme', 'tags', 'selling_points', 'audience'] as const).map((k) => [k, { status: 'decided' as const, value: 'v' }]),
      ),
    )
    expect(nextFlowStep(all, 'idea')).toBe('scale')
  })

  it('草稿汇总只显示已定的值，标签用顿号连接', () => {
    const s = makeState({ tags: { status: 'decided', value: ['修仙', '穿越'] }, title: { status: 'skipped' } })
    const rows = draftSummary(s)
    expect(rows.find((r) => r.key === 'tags')).toMatchObject({ text: '修仙、穿越', status: 'decided' })
    expect(rows.find((r) => r.key === 'title')).toMatchObject({ text: '', status: 'skipped' })
  })

  it('类型标签来源：全来自候选 = ai，全自定义 = manual，混合 = edited', () => {
    const rounds = [{ options: ['修仙', '穿越'] }, { options: ['群像'] }]
    expect(tagsSource(['修仙', '群像'], rounds)).toBe('ai')
    expect(tagsSource(['自定义'], rounds)).toBe('manual')
    expect(tagsSource(['修仙', '自定义'], rounds)).toBe('edited')
  })
})

describe('规模建议（与后端 test_suggest_scale_boundaries 用例一致）', () => {
  it.each([
    [1000, [1, 10, 7]],
    [30000, [1, 10, 7]],
    [90000, [1, 30, 7]],
    [300000, [4, 25, 13]],
    [1_000_000, [12, 28, 29]],
    [3_000_000, [20, 50, 30]],
    [10_000_000, [20, 100, 30]],
  ])('%i 字', (target, expected) => {
    const s = suggestScale(target)
    expect([s.volume_count, s.chapters_per_volume, s.character_count]).toEqual(expected)
    expect(s.target_words).toBe(target)
  })

  it('舍入与 Python round 一致（.5 取偶）', () => {
    expect(roundHalfEven(10.5)).toBe(10)
    expect(roundHalfEven(11.5)).toBe(12)
    expect(roundHalfEven(10.4)).toBe(10)
    expect(roundHalfEven(10.6)).toBe(11)
    // 31500 / 3000 = 10.5 → 10 章；94500 / 3000 = 31.5 → 32 章 → 2 卷 × 16 章
    expect(suggestScale(31500).chapters_per_volume).toBe(10)
    expect(suggestScale(94500)).toMatchObject({ volume_count: 2, chapters_per_volume: 16 })
  })

  it('表单初始值与请求体', () => {
    const saved = { target_words: null, volume_count: 2, chapters_per_volume: 10, character_count: 5 }
    expect(initialScaleValues(saved)).toEqual(saved)
    expect(initialScaleValues(null)).toEqual(suggestScale(300000))
    expect(toScale({ target_words: null, volume_count: 1, chapters_per_volume: 2, character_count: 3 })).toEqual({
      target_words: null,
      volume_count: 1,
      chapters_per_volume: 2,
      character_count: 3,
    })
  })
})

describe('应用对比的默认勾选', () => {
  const item = (over: Partial<InspirationApplyPreviewItem>): InspirationApplyPreviewItem => ({
    field: 'synopsis',
    label: '简介',
    current: '',
    proposed: '新简介',
    default_checked: true,
    ...over,
  })

  it('项目为空才默认勾选；已有内容 / 相同值不勾', () => {
    expect(defaultApplyChecked(item({}))).toBe(true)
    expect(defaultApplyChecked(item({ current: '   ' }))).toBe(true)
    expect(defaultApplyChecked(item({ current: null, field: 'target_words', proposed: 300000 }))).toBe(true)
    expect(defaultApplyChecked(item({ current: [], field: 'tags', proposed: ['A'] }))).toBe(true)
    expect(defaultApplyChecked(item({ current: '旧简介' }))).toBe(false)
    expect(defaultApplyChecked(item({ current: ['A'], field: 'tags', proposed: ['A'] }))).toBe(false)
    expect(applyUnchanged(item({ current: ['A'], proposed: ['A'] }))).toBe(true)
    expect(applyUnchanged(item({ current: ['A'], proposed: ['A', 'B'] }))).toBe(false)
  })

  it('以后端 default_checked 为准', () => {
    const list = [
      item({ field: 'title', current: '旧书名', proposed: '新书名', default_checked: false }),
      item({ field: 'synopsis' }),
      item({ field: 'theme', current: '', proposed: '主题', default_checked: false }),
    ]
    expect(defaultApplyFields(list)).toEqual(['synopsis'])
  })

  it('值展示', () => {
    expect(applyValueText('synopsis', '')).toBe('（空）')
    expect(applyValueText('tags', ['A', 'B'])).toBe('A、B')
    expect(applyValueText('target_words', 300000)).toBe('300,000 字')
    expect(applyValueText('target_words', null)).toBe('（空）')
  })
})
