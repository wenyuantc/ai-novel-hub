import { describe, expect, it } from 'vitest'
import type { AdminDashboardHistoryPoint } from '../api/types'
import { dashboardChartFrame, dashboardCount, dashboardDelta, dashboardLineSegments, dashboardTaskMax, dashboardTime } from './adminDashboard'

function point(day: string, users: number | null): AdminDashboardHistoryPoint {
  return {
    day, captured_at: null, totals: users === null ? null : { users, projects: 4, chapters: 8, total_words: 100, pending_submissions: 0 },
    tasks: null, partial: false,
  }
}

describe('管理员统计展示', () => {
  it('真实零、未知和负净变化区分显示，不虚构增幅', () => {
    expect(dashboardCount(0)).toBe('0')
    expect(dashboardCount(null)).toBe('无记录')
    expect(dashboardCount(undefined)).toBe('无记录')
    expect(dashboardDelta(null)).toBe('无可比数据')
    expect(dashboardDelta(-5)).toBe('-5')
    expect(dashboardDelta(0)).toBe('0')
    expect(dashboardDelta(2)).toBe('+2')
  })

  it('缺口分割线段，零和单点保留，x位置仍对应真实日期间距', () => {
    const data = [point('2026-10-28', 0), point('2026-10-29', null), point('2026-10-30', 3), point('2026-10-31', 4)]
    const { segments, bottom } = dashboardLineSegments(data, 'users')
    expect(segments.map((segment) => segment.map((item) => item.value))).toEqual([[0], [3, 4]])
    expect(segments[0][0].y).toBe(bottom)
    expect(segments[1][0].x - segments[0][0].x).toBeCloseTo((segments[1][1].x - segments[1][0].x) * 2)
    expect(segments[1][1].day).toBe('2026-10-31')
    expect(dashboardLineSegments([point('2026-10-31', 0)], 'users').segments[0][0].x).toBeGreaterThan(62)
    expect(dashboardLineSegments([point('2026-10-31', null)], 'users').segments).toEqual([])
  })

  it('图表随实测宽度重排横坐标而非缩放整体，移动端仍保留固定图高与零/单点', () => {
    for (const width of [288, 900]) {
      const frame = dashboardChartFrame(width)
      expect(frame).toMatchObject({ width, height: 210, left: 62, right: width - 18, bottom: 180 })
      const plot = dashboardLineSegments([point('2026-10-30', 0), point('2026-10-31', 8)], 'users', width)
      expect(plot.segments[0][0]).toMatchObject({ x: frame.left, y: frame.bottom, value: 0 })
      expect(plot.segments[0][1]).toMatchObject({ x: frame.right, y: frame.top, value: 8 })
      const single = dashboardLineSegments([point('2026-10-31', 0)], 'users', width)
      expect(single.segments[0][0].x).toBe((frame.left + frame.right) / 2)
      expect(single.segments[0][0].y).toBe(frame.bottom)
    }
  })

  it('服务器无时区时间按UTC解析，再明确转换为北京时间', () => {
    expect(dashboardTime('2026-10-30T16:00:01')).toContain('2026/10/31 00:00:01')
    expect(dashboardTime('2026-10-30T16:00:01Z')).toBe(dashboardTime('2026-10-31T00:00:01+08:00'))
    expect(dashboardTime('2026-10-30T16:00:01Z')).toContain('北京时间')
    expect(dashboardTime(null)).toBe('无记录')
  })

  it('任务图按新建和结束总数的最大值定轴，不假定同日相等', () => {
    const day = point('2026-10-31', 1)
    day.tasks = { created: 2, succeeded: 4, failed: 1, cancelled: 1, interrupted: 2 }
    expect(dashboardTaskMax([day, point('2026-10-30', null)])).toBe(8)
    expect(dashboardTaskMax([point('2026-10-31', null)])).toBe(1)
  })
})
