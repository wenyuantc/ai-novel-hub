import { describe, expect, it } from 'vitest'
import { computeDiff } from './textDiff'

describe('computeDiff', () => {
  it('统计增删字数并能还原两侧文本', () => {
    const a = '他走进房间，坐下。'
    const b = '他缓缓走进昏暗的房间，坐下。'
    const r = computeDiff(a, b)
    expect(r).not.toBeNull()
    expect(r!.added).toBe(5)
    expect(r!.removed).toBe(0)
    const left = r!.parts.filter((p) => p.kind !== 'added').map((p) => p.value).join('')
    const right = r!.parts.filter((p) => p.kind !== 'removed').map((p) => p.value).join('')
    expect(left).toBe(a)
    expect(right).toBe(b)
  })

  it('删除与替换；空白不计字数', () => {
    const r = computeDiff('甲乙 丙', '甲丁')!
    expect(r.removed).toBe(2)
    expect(r.added).toBe(1)
  })

  it('按码点对比，不拆开 emoji', () => {
    const r = computeDiff('a😀b', 'a😃b')!
    expect(r.parts.filter((p) => p.kind !== 'equal').map((p) => p.value)).toEqual(['😀', '😃'])
    expect(r.added).toBe(1)
    expect(r.removed).toBe(1)
  })

  it('相同文本没有增删', () => {
    expect(computeDiff('相同', '相同')).toEqual({ parts: [{ kind: 'equal', value: '相同' }], added: 0, removed: 0 })
  })
})
