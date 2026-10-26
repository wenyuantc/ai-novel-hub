// 立项结构化字段（简介 / 主题 / 类型标签 / 核心卖点 / 目标读者 / 目标总字数）的限制与纯函数，与 backend/app/schemas/project.py 一致

export const PROJECT_FIELD_LIMITS = {
  synopsis: 2000,
  theme: 200,
  sellingPoints: 500,
  audience: 100,
  maxTags: 8,
  tagMaxLen: 10,
  targetWordsMin: 1000,
  targetWordsMax: 10_000_000,
} as const

/** 码点长度（后端 len() 按码点计） */
function codePointLength(text: string): number {
  return Array.from(text).length
}

/** 类型标签规范化：去首尾空白、丢弃空串、去重（保持顺序）。与后端 normalize_tags 相同，但不抛错（超限由 tagsError 给出）。 */
export function normalizeTags(values: readonly unknown[] | null | undefined): string[] {
  const out: string[] = []
  for (const v of values ?? []) {
    const t = String(v ?? '').trim()
    if (t && !out.includes(t)) out.push(t)
  }
  return out
}

/** 规范化后的标签是否超限；合法返回 null */
export function tagsError(values: readonly unknown[] | null | undefined): string | null {
  const tags = normalizeTags(values)
  if (tags.length > PROJECT_FIELD_LIMITS.maxTags) return `类型标签最多 ${PROJECT_FIELD_LIMITS.maxTags} 个`
  const long = tags.find((t) => codePointLength(t) > PROJECT_FIELD_LIMITS.tagMaxLen)
  if (long !== undefined) return `类型标签每个最多 ${PROJECT_FIELD_LIMITS.tagMaxLen} 字：${long}`
  return null
}

/** 写作进度：已写字数 / 目标总字数，0–100 取整；没有目标时为 null */
export function wordsProgress(totalWords: number, targetWords: number | null | undefined): number | null {
  if (!targetWords || targetWords <= 0) return null
  return Math.min(100, Math.floor((Math.max(0, totalWords) / targetWords) * 100))
}

/** 字数的中文简写：12000 → 1.2 万字 */
export function formatWordCount(n: number): string {
  if (n >= 10000) {
    const w = n / 10000
    return `${Number.isInteger(w) ? w : w.toFixed(1).replace(/\.0$/, '')} 万字`
  }
  return `${n} 字`
}

/** 表单值 → 请求体字段（字符串 trim、标签规范化、目标字数缺省为 null） */
export interface ProjectInfoValues {
  synopsis?: string
  theme?: string
  tags?: string[]
  selling_points?: string
  audience?: string
  target_words?: number | null
}

export function projectInfoBody(v: ProjectInfoValues): Required<ProjectInfoValues> {
  return {
    synopsis: v.synopsis?.trim() ?? '',
    theme: v.theme?.trim() ?? '',
    tags: normalizeTags(v.tags),
    selling_points: v.selling_points?.trim() ?? '',
    audience: v.audience?.trim() ?? '',
    target_words: v.target_words ?? null,
  }
}
