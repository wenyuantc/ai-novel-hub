import { describe, expect, it } from 'vitest'
import { deepLinkId, monthCells } from './calendarModel'

describe('连载月历与章节深链', () => {
  it('以周一开头并保留闰月最后一天', () => {
    const feb = monthCells('2024-02')
    expect(feb.slice(0, 4)).toEqual([null, null, null, '2024-02-01'])
    expect(feb.at(-1)).toBe('2024-02-29')
  })
  it('月份在周日开始时保留六个空格，周一开始时首格就是 1 日', () => {
    expect(monthCells('2026-11').indexOf('2026-11-01')).toBe(6)
    expect(monthCells('2026-06')[0]).toBe('2026-06-01')
  })
  it('深链仅解析有效正整数，不把非法参数发给 API', () => {
    for (const value of [null, '', '-1', '2.5', '1e3', '0', '9007199254740992']) expect(deepLinkId(value)).toBeNull()
    expect(deepLinkId('4')).toBe(4)
  })
})
