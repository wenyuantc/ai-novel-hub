import dayjs from 'dayjs'
import type { SerializationStage } from '../../api/serialization'

export const SERIALIZATION_PAGE_SIZE = 12

export const EDITORIAL_LABEL: Record<SerializationStage, string> = {
  revision: '待修订', proofreading: '待校对', final: '定稿',
}

/** 计划日期是民用日期，仅以年月日运算，与浏览器的 UTC 偏移无关。 */
export function monthCells(month: string): (string | null)[] {
  const first = dayjs(`${month}-01`)
  const blanks = (first.day() + 6) % 7
  return [
    ...Array<null>(blanks).fill(null),
    ...Array.from({ length: first.daysInMonth() }, (_, index) => `${month}-${String(index + 1).padStart(2, '0')}`),
  ]
}

export function deepLinkId(value: string | null): number | null {
  if (!value || !/^\d+$/.test(value)) return null
  const id = Number(value)
  return Number.isSafeInteger(id) && id > 0 ? id : null
}
