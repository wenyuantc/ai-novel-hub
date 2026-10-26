import { describe, expect, it } from 'vitest'
import { parseWizardDone, parseWizardWarnings, WIZARD_KIND_LABELS } from './wizard'

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
