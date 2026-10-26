// 提示词模板的纯函数：校验错误格式化与定位、分类分组、状态徽标、锁定段切分、导入结果汇总

import { ApiError } from '../api/client'
import type {
  LockedBlock,
  PromptImportResult,
  PromptImportStatus,
  PromptTemplateBrief,
  TemplateIssue,
  TemplatePart,
} from '../api/types'

export const PART_LABELS: Record<TemplatePart, string> = { system: '系统段', user: '用户段' }

/** 与后端 prompt_service.format_issue 同口径：「用户段第 3 行：未知变量：x」 */
export function formatIssue(issue: TemplateIssue): string {
  let where = issue.part ? PART_LABELS[issue.part] : ''
  if (issue.line) where = `${where}第 ${issue.line} 行`
  return where ? `${where}：${issue.message}` : issue.message
}

function isIssue(value: unknown): value is TemplateIssue {
  if (typeof value !== 'object' || value === null) return false
  const v = value as Record<string, unknown>
  return (
    typeof v.message === 'string' &&
    (v.part === null || v.part === 'system' || v.part === 'user' || v.part === undefined) &&
    (v.line === null || v.line === undefined || typeof v.line === 'number')
  )
}

/** 规范化未知来源的问题列表（缺 part / line 补 null） */
export function readIssues(value: unknown): TemplateIssue[] {
  if (!Array.isArray(value)) return []
  return value.filter(isIssue).map((i) => ({ part: i.part ?? null, line: i.line ?? null, message: i.message }))
}

/** 保存 400 响应体里的 errors（不是模板校验错误时返回空数组） */
export function issuesFromError(err: unknown): TemplateIssue[] {
  if (!(err instanceof ApiError) || err.status !== 400 || !err.body) return []
  return readIssues(err.body.errors)
}

/** 某一段的问题（按行号排序，无行号的在最后） */
export function issuesForPart(issues: TemplateIssue[], part: TemplatePart): TemplateIssue[] {
  return issues
    .filter((i) => i.part === part)
    .sort((a, b) => (a.line ?? Number.MAX_SAFE_INTEGER) - (b.line ?? Number.MAX_SAFE_INTEGER))
}

/** 第 line 行（从 1 起）在文本中的 UTF-16 偏移区间 [start, end)，用于定位到出错行；越界时夹到末行 */
export function lineRange(text: string, line: number): [number, number] {
  const lines = text.split('\n')
  const idx = Math.min(Math.max(line, 1), lines.length) - 1
  let start = 0
  for (let i = 0; i < idx; i++) start += lines[i].length + 1
  return [start, start + lines[idx].length]
}

export interface TemplateGroup {
  category: string
  label: string
  items: PromptTemplateBrief[]
}

/** 按分类分组（保持后端注册顺序：分类按首次出现排序，组内按原顺序） */
export function groupTemplates(list: PromptTemplateBrief[]): TemplateGroup[] {
  const groups: TemplateGroup[] = []
  const byCat = new Map<string, TemplateGroup>()
  for (const t of list) {
    let g = byCat.get(t.category)
    if (!g) {
      g = { category: t.category, label: t.category_label || t.category, items: [] }
      byCat.set(t.category, g)
      groups.push(g)
    }
    g.items.push(t)
  }
  return groups
}

/** 列表徽标：已覆盖 / 已停用 / 默认已更新 */
export function templateBadges(
  t: Pick<PromptTemplateBrief, 'overridden' | 'enabled' | 'outdated'>,
): { label: string; color: string }[] {
  if (!t.overridden) return []
  const out = [t.enabled ? { label: '已覆盖', color: 'blue' } : { label: '已停用', color: 'default' }]
  if (t.outdated) out.push({ label: '默认已更新', color: 'orange' })
  return out
}

export interface PreviewSegment {
  text: string
  /** 锁定段的标签；普通文本为 null */
  locked: string | null
}

/** 把渲染结果按锁定内容切分，锁定段在预览中灰底只读展示；每个锁定块只匹配第一次出现 */
export function splitLocked(content: string, locked: LockedBlock[]): PreviewSegment[] {
  const marks: { start: number; end: number; label: string }[] = []
  for (const b of locked) {
    if (!b.value) continue
    const start = content.indexOf(b.value)
    if (start < 0) continue
    const end = start + b.value.length
    if (marks.some((m) => start < m.end && end > m.start)) continue
    marks.push({ start, end, label: b.label })
  }
  marks.sort((a, b) => a.start - b.start)
  const out: PreviewSegment[] = []
  let pos = 0
  for (const m of marks) {
    if (m.start > pos) out.push({ text: content.slice(pos, m.start), locked: null })
    out.push({ text: content.slice(m.start, m.end), locked: m.label })
    pos = m.end
  }
  if (pos < content.length) out.push({ text: content.slice(pos), locked: null })
  return out
}

export const PROMPT_IMPORT_STATUS: Record<PromptImportStatus, { label: string; color: string }> = {
  created: { label: '已新建覆盖', color: 'success' },
  updated: { label: '已更新', color: 'processing' },
  unchanged: { label: '无变化', color: 'default' },
  default: { label: '与默认相同', color: 'default' },
  skipped: { label: '已跳过', color: 'warning' },
  invalid: { label: '校验失败', color: 'error' },
}

/** 导入结果汇总：按状态计数，文案只列出非零项（顺序同 PROMPT_IMPORT_STATUS） */
export function summarizePromptImport(result: PromptImportResult): {
  counts: Record<PromptImportStatus, number>
  text: string
} {
  const counts: Record<PromptImportStatus, number> = {
    created: 0,
    updated: 0,
    unchanged: 0,
    default: 0,
    skipped: 0,
    invalid: 0,
  }
  for (const i of result.items) counts[i.status] += 1
  const parts = (Object.keys(PROMPT_IMPORT_STATUS) as PromptImportStatus[])
    .filter((s) => counts[s] > 0)
    .map((s) => `${PROMPT_IMPORT_STATUS[s].label} ${counts[s]}`)
  return { counts, text: parts.length ? parts.join('，') : '文件中没有模板' }
}
