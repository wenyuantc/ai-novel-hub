import type { UsageCurrency, UsageFilters, UsageOffPeakWindow, UsageRow, UsageSource } from '../../api/aiUsage'
import { TASK_KIND_LABELS } from '../../api/tasks'
import type { UsagePricePeriod } from '../../api/types'
import { parseServerTime } from '../../utils/time'

export const USAGE_CURRENCIES = [
  { value: 'CNY', label: '人民币（CNY）' },
  { value: 'USD', label: '美元（USD）' },
] as const

export const USAGE_STATUSES: Record<UsageRow['status'], string> = {
  pending: '待确认', succeeded: '成功', failed: '失败', cancelled: '已取消', interrupted: '中断',
}

export const USAGE_PRICING: Record<UsageRow['pricing_status'], string> = {
  pending: '待计量', priced: '已估算', missing_price: '缺少模型单价', missing_usage: '缺少用量',
  unconfirmed: '发送未确认', private: '作者承担',
}

export const DEFAULT_USAGE_PRICE_TIMEZONE = 'Asia/Shanghai'
export const USAGE_PRICE_PERIOD_LABELS: Record<UsagePricePeriod, string> = {
  fixed: '固定单价', peak: '高峰价', off_peak: '空闲价',
}

export const USAGE_PRICE_CLOCK_TIME = /^(?:[01]\d|2[0-3]):[0-5]\d$/

export function offPeakWindowsError(
  windows?: readonly (Partial<UsageOffPeakWindow> | undefined)[],
): string | undefined {
  if (!windows?.length) return '请添加至少一个空闲时段。'

  const segments: (UsageOffPeakWindow & { position: number })[] = []
  for (const [index, window] of windows.entries()) {
    const { start, end } = window ?? {}
    // 未填写或格式错误的时间由对应的表单项提示。
    if (!start || !end || !USAGE_PRICE_CLOCK_TIME.test(start) || !USAGE_PRICE_CLOCK_TIME.test(end)) continue
    const position = index + 1
    if (start === end) return `第 ${position} 个空闲时段的开始与结束时间须不同。`
    if (start < end) {
      segments.push({ start, end, position })
    } else {
      segments.push({ start, end: '24:00', position })
      if (end !== '00:00') segments.push({ start: '00:00', end, position })
    }
  }

  segments.sort((left, right) => left.start.localeCompare(right.start))
  for (let index = 1; index < segments.length; index++) {
    const previous = segments[index - 1]
    const current = segments[index]
    if (current.start < previous.end) {
      const first = Math.min(previous.position, current.position)
      const second = Math.max(previous.position, current.position)
      return `第 ${first} 个和第 ${second} 个空闲时段重叠，请调整时间。`
    }
  }
}

export const USAGE_KIND_LABELS: Record<string, string> = {
  ...TASK_KIND_LABELS,
  'chapter.generate': '章节生成',
  'chapter.generate.retry': '章节生成重试',
  'chapter.analyze': '章节分析',
  'reader.ai_casting': '阅读页 AI 自动配音',
  'model.test': '模型连通性测试',
}

export const usageKindLabel = (kind: string): string => Object.hasOwn(USAGE_KIND_LABELS, kind) ? USAGE_KIND_LABELS[kind] : kind

const beijingTime = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
})

export function usageTime(value: string): string {
  const parsed = parseServerTime(value)
  return parsed.isValid() ? beijingTime.format(parsed.toDate()) : value
}

export function usageMoney(amount: string, currency: UsageCurrency): string {
  const units = amount.includes('.') ? amount.replace(/0+$/, '').replace(/\.$/, '') : amount
  return `${currency} ${units}`
}

export function usageCount(value: number | null, source: UsageSource): string {
  if (value === null || source === 'unknown') return '未知'
  return `${value.toLocaleString('zh-CN')}（${source === 'provider' ? '模型返回' : '估算'}）`
}

/** Decimal 字符串原样发送，金额上下界同时检查整数与小数位。 */
export function validUsageDecimal(value: string, maximum: number, allowZero = true): boolean {
  if (!/^\d{1,10}(?:\.\d{1,9})?$/.test(value)) return false
  const [integer, fraction = '0'] = value.split('.')
  const whole = Number(integer)
  const hasFraction = /[1-9]/.test(fraction)
  return (allowZero || whole > 0 || hasFraction) && (whole < maximum || (whole === maximum && !hasFraction))
}

export interface UsageFilterFields extends Omit<UsageFilters, 'from' | 'to'> {
  fromDay?: string
  toDay?: string
}

function beijingDayStart(value: string): number {
  const date = Date.parse(`${value}T00:00:00+08:00`)
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || !Number.isFinite(date)
    || new Date(date + 8 * 3600_000).toISOString().slice(0, 10) !== value) {
    throw new Error('请输入有效的日期')
  }
  return date
}

export function usageFilterQuery(fields: UsageFilterFields): UsageFilters {
  const { fromDay, toDay, model, kind, ...filters } = fields
  const start = fromDay ? beijingDayStart(fromDay) : null
  const end = toDay ? beijingDayStart(toDay) : null
  if (start !== null && end !== null && end < start) throw new Error('结束日期应晚于或等于开始日期')
  return {
    ...filters,
    model: model?.trim() || undefined,
    kind: kind?.trim() || undefined,
    from: start === null ? undefined : new Date(start).toISOString(),
    to: end === null ? undefined : new Date(end + 24 * 3600_000).toISOString(),
    page: 1,
  }
}

export function parseUsageId(value: string | null): number | undefined {
  if (!value || !/^[1-9]\d*$/.test(value)) return undefined
  const id = Number(value)
  return Number.isSafeInteger(id) && id <= 2_147_483_647 ? id : undefined
}
