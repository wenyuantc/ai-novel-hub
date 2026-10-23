import { diffChars } from 'diff'

export type DiffKind = 'equal' | 'added' | 'removed'

export interface DiffPart {
  kind: DiffKind
  value: string
}

export interface DiffResult {
  parts: DiffPart[]
  /** 新增 / 删除字数（按非空白字符计，与 countWords 口径一致） */
  added: number
  removed: number
}

/** 超过该耗时（毫秒）放弃逐字对比，避免两份差异极大的长文卡住页面 */
export const DIFF_TIMEOUT_MS = 3000

/** 与 api/chapters.ts 的 countWords 同口径：非空白码点数（与后端 len() 一致） */
function countWords(text: string): number {
  return Array.from(text.replace(/\s+/g, '')).length
}

/** 逐字（按码点）对比 a → b；超时返回 null。 */
export function computeDiff(a: string, b: string, timeout = DIFF_TIMEOUT_MS): DiffResult | null {
  const changes = diffChars(a, b, { timeout })
  if (!changes) return null
  let added = 0
  let removed = 0
  const parts: DiffPart[] = changes.map((c) => {
    if (c.added) {
      added += countWords(c.value)
      return { kind: 'added', value: c.value }
    }
    if (c.removed) {
      removed += countWords(c.value)
      return { kind: 'removed', value: c.value }
    }
    return { kind: 'equal', value: c.value }
  })
  return { parts, added, removed }
}
