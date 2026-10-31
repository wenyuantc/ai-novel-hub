import type { AdminDashboardHistoryPoint, AdminDashboardTotals } from '../api/types'
import { parseServerTime } from './time'

export type DashboardMetric = Exclude<keyof AdminDashboardTotals, 'pending_submissions'>
export const DASHBOARD_METRICS: Record<DashboardMetric, string> = {
  users: '用户', projects: '作品', chapters: '章节', total_words: '正文总字数',
}
export const DASHBOARD_DAYS = [{ value: 7, label: '近 7 天' }, { value: 30, label: '近 30 天' }, { value: 90, label: '近 90 天' }]
const numberFormat = new Intl.NumberFormat('zh-CN')
const beijingFormat = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
})

export function dashboardCount(value: number | null | undefined): string {
  return value == null ? '无记录' : numberFormat.format(value)
}

export function dashboardDelta(value: number | null): string {
  return value === null ? '无可比数据' : `${value > 0 ? '+' : ''}${numberFormat.format(value)}`
}

/** 统计日直接使用后端 day；只有采样时刻才解析 UTC 并按北京时间展示。 */
export function dashboardTime(value: string | null | undefined): string {
  if (!value) return '无记录'
  const parsed = parseServerTime(value)
  return parsed.isValid() ? `${beijingFormat.format(parsed.toDate())}（北京时间）` : value
}

export interface DashboardPlotPoint {
  x: number
  y: number
  value: number
  day: string
  partial: boolean
}

/** 坐标单位对应实测容器像素，横向适配不再缩放刻度字号和图高。 */
export function dashboardChartFrame(width: number, height = 210) {
  return { width, height, left: 62, right: width - 18, top: 16, bottom: height - 30 }
}

/** 缺失日期直接切断线段，保留有效的零和单点，不把缺失压缩成连续日期。 */
export function dashboardLineSegments(points: AdminDashboardHistoryPoint[], metric: DashboardMetric, width = 600, height = 210) {
  const frame = dashboardChartFrame(width, height)
  const { left, right, top, bottom } = frame
  const values = points.flatMap((point) => point.totals ? [point.totals[metric]] : [])
  const max = Math.max(1, ...values)
  const segments: DashboardPlotPoint[][] = []
  let segment: DashboardPlotPoint[] = []
  for (const [index, point] of points.entries()) {
    if (point.totals === null) {
      if (segment.length) segments.push(segment)
      segment = []
      continue
    }
    const value = point.totals[metric]
    segment.push({
      x: points.length === 1 ? (left + right) / 2 : left + index * (right - left) / (points.length - 1),
      y: bottom - value / max * (bottom - top), value, day: point.day, partial: point.partial,
    })
  }
  if (segment.length) segments.push(segment)
  return { ...frame, segments, max }
}

export function dashboardAxis(value: number): string {
  if (value >= 100_000_000) return `${Number((value / 100_000_000).toFixed(1))}亿`
  if (value >= 10_000) return `${Number((value / 10_000).toFixed(1))}万`
  return dashboardCount(Math.round(value))
}

/** 每日任务的创建与结束是两种事件，不要求它们同日相等。 */
export function dashboardTaskMax(points: AdminDashboardHistoryPoint[]): number {
  return Math.max(1, ...points.flatMap(({ tasks }) => tasks
    ? [tasks.created, tasks.succeeded + tasks.failed + tasks.cancelled + tasks.interrupted]
    : []))
}
