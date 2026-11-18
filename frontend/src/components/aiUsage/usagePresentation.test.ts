import { describe, expect, it } from 'vitest'
import { offPeakWindowsError, parseUsageId, usageCount, usageFilterQuery, usageKindLabel, usageMoney, usageTime, validUsageDecimal } from './usagePresentation'

describe('AI 用量展示及筛选', () => {
  it('按真实记录的任务类型展示并筛选模型连通性测试', () => {
    expect(usageKindLabel('model.test')).toBe('模型连通性测试')
    expect(usageFilterQuery({ kind: 'model.test' }).kind).toBe('model.test')
  })

  it('区分首次章节生成和批量重试，保留对应的筛选条件', () => {
    expect(usageKindLabel('chapter.generate')).toBe('章节生成')
    expect(usageKindLabel('chapter.generate.retry')).toBe('章节生成重试')
    expect(usageFilterQuery({ kind: 'chapter.generate.retry' }).kind).toBe('chapter.generate.retry')
  })

  it('自然日按北京时间换成 UTC 半开范围，包含结束日全天', () => {
    expect(usageFilterQuery({ fromDay: '2026-11-30', toDay: '2026-12-01', model: ' model ', kind: '  ' })).toEqual({
      model: 'model', kind: undefined, from: '2026-11-29T16:00:00.000Z', to: '2026-12-01T16:00:00.000Z', page: 1,
    })
    expect(() => usageFilterQuery({ fromDay: '2026-02-30' })).toThrow('有效的日期')
    expect(() => usageFilterQuery({ fromDay: '2026-12-01', toDay: '2026-11-30' })).toThrow('结束日期')
    expect(usageTime('2026-11-30T16:00:00')).toBe('2026/12/01 00:00:00')
    expect(parseUsageId('17')).toBe(17)
    for (const value of ['0', '-1', '1.2', '024', '2147483648', 'ab']) expect(parseUsageId(value)).toBeUndefined()
  })

  it('精确显示小额、实际零值、估算和未知用量', () => {
    expect(usageMoney('0.000000001', 'USD')).toBe('USD 0.000000001')
    expect(usageMoney('0.000000000', 'CNY')).toBe('CNY 0')
    expect(usageCount(0, 'provider')).toBe('0（模型返回）')
    expect(usageCount(42, 'estimated')).toBe('42（估算）')
    expect(usageCount(null, 'unknown')).toBe('未知')
  })

  it('预算和价格校验保持定点边界，零价格有效', () => {
    expect(validUsageDecimal('0', 1_000_000)).toBe(true)
    expect(validUsageDecimal('0.000000000', 1_000_000_000, false)).toBe(false)
    expect(validUsageDecimal('0.000000001', 1_000_000_000, false)).toBe(true)
    expect(validUsageDecimal('1000000000.000000000', 1_000_000_000, false)).toBe(true)
    expect(validUsageDecimal('1000000000.000000001', 1_000_000_000, false)).toBe(false)
    expect(validUsageDecimal('0.0000000001', 1_000_000)).toBe(false)
    expect(validUsageDecimal('1e-4', 1_000_000)).toBe(false)
  })
})

describe('每日空闲时段', () => {
  it('允许分离时段、跨零点时段及首尾相邻', () => {
    expect(offPeakWindowsError([
      { start: '12:00', end: '14:00' },
      { start: '23:00', end: '07:00' },
      { start: '07:00', end: '09:00' },
    ])).toBeUndefined()
    expect(offPeakWindowsError([
      { start: '23:00', end: '00:00' }, { start: '00:00', end: '01:00' },
    ])).toBeUndefined()
  })

  it('拒绝普通重叠和跨零点重叠，指出对应的两条时段', () => {
    for (const windows of [
      [{ start: '13:00', end: '15:00' }, { start: '12:00', end: '14:00' }],
      [{ start: '23:00', end: '07:00' }, { start: '01:00', end: '02:00' }],
      [{ start: '23:00', end: '01:00' }, { start: '22:00', end: '23:30' }],
    ]) {
      expect(offPeakWindowsError(windows)).toBe('第 1 个和第 2 个空闲时段重叠，请调整时间。')
    }
  })

  it('要求至少一个时段且时段起止不同', () => {
    expect(offPeakWindowsError([])).toBe('请添加至少一个空闲时段。')
    expect(offPeakWindowsError([{ start: '07:00', end: '07:00' }]))
      .toBe('第 1 个空闲时段的开始与结束时间须不同。')
  })
})
