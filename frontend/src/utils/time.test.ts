import { describe, expect, it } from 'vitest'
import { parseServerTime } from './time'

describe('parseServerTime', () => {
  it('无时区后缀按 UTC 解析', () => {
    expect(parseServerTime('2026-10-22T08:00:00').toISOString()).toBe('2026-10-22T08:00:00.000Z')
  })
  it('保留已有时区', () => {
    expect(parseServerTime('2026-10-22T08:00:00+08:00').toISOString()).toBe('2026-10-22T00:00:00.000Z')
    expect(parseServerTime('2026-10-22T08:00:00.123Z').toISOString()).toBe('2026-10-22T08:00:00.123Z')
  })
})
