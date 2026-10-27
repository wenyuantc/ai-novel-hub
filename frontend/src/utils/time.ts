import dayjs from 'dayjs'

/** 后端时间可能不带时区后缀，按 UTC 解析后转为本地时间显示。 */
export function parseServerTime(value: string): dayjs.Dayjs {
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/i.test(value)
  return dayjs(hasZone ? value : `${value}Z`)
}

export function formatTime(value: string | null | undefined, fmt = 'YYYY-MM-DD HH:mm'): string {
  if (!value) return '-'
  const d = parseServerTime(value)
  return d.isValid() ? d.format(fmt) : value
}

/** 相对时间：刚刚 / N 分钟前 / N 小时前 / N 天前（30 天内），更早显示日期 */
export function formatRelative(value: string | null | undefined, now: dayjs.Dayjs = dayjs()): string {
  if (!value) return '-'
  const d = parseServerTime(value)
  if (!d.isValid()) return value
  const minutes = now.diff(d, 'minute')
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days} 天前`
  return d.format('YYYY-MM-DD')
}
