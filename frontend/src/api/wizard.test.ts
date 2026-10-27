import { describe, expect, it } from 'vitest'
import type { Task } from './types'
import {
  canResumeWizard,
  markWizardStep,
  parseWizardDone,
  parseWizardMode,
  parseWizardWarnings,
  resumeWizardStep,
  WIZARD_KIND_LABELS,
} from './wizard'

describe('向导结果', () => {
  it('职业体系：items 与 warnings', () => {
    const result = { kind: 'careers', items: [{ id: 1, name: '剑修' }], warnings: ['职业「x」没有阶段，已丢弃', 3] }
    expect(parseWizardDone('careers', result)).toHaveLength(1)
    expect(parseWizardDone('characters', result)).toBeNull()
    expect(parseWizardWarnings(result)).toEqual(['职业「x」没有阶段，已丢弃'])
    expect(parseWizardWarnings({ kind: 'world', items: [] })).toEqual([])
    expect(parseWizardWarnings(null)).toEqual([])
    expect(WIZARD_KIND_LABELS.careers).toBe('职业体系')
  })
})

const STEPS = ['world', 'careers', 'characters', 'outline']

describe('向导进度', () => {
  it('完成世界观、跳过职业后再进入定位到角色步骤（AC7）', () => {
    let progress = { done: [] as string[], skipped: [] as string[], current: null as string | null }
    const apply = (u: ReturnType<typeof markWizardStep>) => {
      progress = { done: u.done ?? progress.done, skipped: u.skipped ?? progress.skipped, current: u.current ?? null }
    }
    apply(markWizardStep(progress, 'world', 'done', 'careers'))
    apply(markWizardStep(progress, 'careers', 'skip', 'characters'))
    expect(progress).toEqual({ done: ['world'], skipped: ['careers'], current: 'characters' })
    expect(resumeWizardStep(progress, STEPS)).toBe(2)
    // 没有 current 时取第一个未处理的步骤
    expect(resumeWizardStep({ ...progress, current: null }, STEPS)).toBe(2)
  })

  it('定位规则：空进度 / 完成页 / 未知步骤 / 全部处理过', () => {
    expect(resumeWizardStep(null, STEPS)).toBe(0)
    expect(resumeWizardStep({ done: [], skipped: [], current: null }, STEPS)).toBe(0)
    expect(resumeWizardStep({ done: [], skipped: [], current: 'finish' }, STEPS)).toBe(4)
    expect(resumeWizardStep({ done: ['world'], skipped: [], current: 'unknown' }, STEPS)).toBe(1)
    expect(resumeWizardStep({ done: STEPS, skipped: [], current: null }, STEPS)).toBe(4)
  })

  it('完成会移出 skipped；已完成的步骤跳过不记入 skipped；不重复', () => {
    expect(markWizardStep({ done: [], skipped: ['careers'], current: null }, 'careers', 'done', 'characters')).toEqual({
      done: ['careers'],
      skipped: [],
      current: 'characters',
    })
    expect(markWizardStep({ done: ['world'], skipped: [], current: null }, 'world', 'skip', 'careers')).toEqual({
      done: ['world'],
      skipped: [],
      current: 'careers',
    })
    expect(markWizardStep({ done: ['world'], skipped: [], current: null }, 'world', 'done', null).done).toEqual(['world'])
  })

  it('大纲向导结果 mode', () => {
    expect(parseWizardMode({ kind: 'outline', items: [], mode: 'append' })).toBe('append')
    expect(parseWizardMode({ kind: 'outline', items: [] })).toBe('replace')
    expect(parseWizardMode(null)).toBe('replace')
  })
})

describe('从中断处继续', () => {
  const task = (status: Task['status']) => ({ id: 1, status }) as Task

  it('失败 / 取消 / 中断且部分输出有完整对象时可继续', () => {
    const text = '{"volumes": [{"chapters": [{"title": "出发"}, {"title": "异'
    for (const s of ['failed', 'cancelled', 'interrupted'] as const) expect(canResumeWizard(task(s), text)).toBe(true)
    expect(canResumeWizard(task('succeeded'), text)).toBe(false)
    expect(canResumeWizard(task('running'), text)).toBe(false)
    expect(canResumeWizard(task('failed'), '{"volumes": [{"vo')).toBe(false)
    expect(canResumeWizard(null, text)).toBe(false)
  })
})
