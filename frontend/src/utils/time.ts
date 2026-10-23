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
