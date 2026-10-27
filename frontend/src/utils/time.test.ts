import { describe, expect, it } from 'vitest'
import dayjs from 'dayjs'
import { formatRelative, parseServerTime } from './time'

describe('parseServerTime', () => {
  it('无时区后缀按 UTC 解析', () => {
    expect(parseServerTime('2026-10-22T08:00:00').toISOString()).toBe('2026-10-22T08:00:00.000Z')
  })
  it('保留已有时区', () => {
    expect(parseServerTime('2026-10-22T08:00:00+08:00').toISOString()).toBe('2026-10-22T00:00:00.000Z')
    expect(parseServerTime('2026-10-22T08:00:00.123Z').toISOString()).toBe('2026-10-22T08:00:00.123Z')
  })
})

describe('formatRelative', () => {
  const now = dayjs('2026-10-27T12:00:00Z')
  it('按跨度显示', () => {
    expect(formatRelative('2026-10-27T11:59:40Z', now)).toBe('刚刚')
    expect(formatRelative('2026-10-27T11:30:00Z', now)).toBe('30 分钟前')
    expect(formatRelative('2026-10-27T09:00:00', now)).toBe('3 小时前')
    expect(formatRelative('2026-10-20T12:00:00Z', now)).toBe('7 天前')
    expect(formatRelative('2026-01-02T12:00:00Z', now)).toBe(parseServerTime('2026-01-02T12:00:00Z').format('YYYY-MM-DD'))
    expect(formatRelative(null, now)).toBe('-')
  })
})
