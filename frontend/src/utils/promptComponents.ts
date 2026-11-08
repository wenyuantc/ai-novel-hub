// 提示词组件的纯函数：引用写法、插入、解析结构化错误（占用 / 影响 / 修订冲突）、引用方跳转地址、导入汇总

import { ApiError } from '../api/client'
import type {
  PromptComponentFailure,
  PromptComponentImportResult,
  PromptComponentUsage,
} from '../api/types'
import { readIssues } from './prompts'

/** 与后端 ai/components.py::REF_RE 同口径 */
const REF_RE = /\{\{>\s*([a-z0-9][a-z0-9-]{0,47})\s*\}\}/g

/** 引用写法 `{{> slug }}` */
export const componentRef = (slug: string) => `{{> ${slug} }}`

/** 文本中引用的组件标识（按首次出现顺序去重） */
export function refSlugs(text: string): string[] {
  return [...new Set([...text.matchAll(REF_RE)].map((m) => m[1]))]
}

/** 在选区 [start, end) 处插入片段（选区为空时插在末尾）；返回新文本与插入后的光标位置 */
export function insertSnippet(text: string, snippet: string, range: [number, number] | null): { text: string; caret: number } {
  const [start, end] = range ?? [text.length, text.length]
  const s = Math.max(0, Math.min(start, text.length))
  const e = Math.max(s, Math.min(end, text.length))
  return { text: text.slice(0, s) + snippet + text.slice(e), caret: s + snippet.length }
}

function errorCode(err: unknown): string | null {
  return err instanceof ApiError && typeof err.body?.code === 'string' ? err.body.code : null
}

/** 409 stale_revision：组件已被别处修改 */
export function isStaleRevision(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409 && errorCode(err) === 'stale_revision'
}

function isUsage(value: unknown): value is PromptComponentUsage {
  if (typeof value !== 'object' || value === null) return false
  const v = value as Record<string, unknown>
  return (v.kind === 'skill' || v.kind === 'override') && typeof v.name === 'string'
}

/** 409 component_in_use 的引用方（删除 / 改标识被拒）；不是该错误时返回 null */
export function readUsages(err: unknown): PromptComponentUsage[] | null {
  if (!(err instanceof ApiError) || err.status !== 409 || errorCode(err) !== 'component_in_use') return null
  const raw = err.body?.usages
  if (!Array.isArray(raw)) return []
  return raw.filter(isUsage).map((u) => ({
    kind: u.kind,
    id: typeof u.id === 'number' ? u.id : null,
    key: typeof u.key === 'string' ? u.key : null,
    name: u.name,
    enabled: typeof u.enabled === 'boolean' ? u.enabled : null,
  }))
}

/** 规范化 failures 列表（400 component_breaks_usages 或导入逐项结果） */
export function normalizeFailures(raw: unknown): PromptComponentFailure[] {
  if (!Array.isArray(raw)) return []
  return raw
    .filter((f): f is Record<string, unknown> => typeof f === 'object' && f !== null && typeof (f as Record<string, unknown>).key === 'string')
    .map((f) => ({ key: f.key as string, name: typeof f.name === 'string' ? f.name : (f.key as string), errors: readIssues(f.errors) }))
}

/** 400 component_breaks_usages：改内容会让这些覆盖校验失败；不是该错误时返回 null */
export function readFailures(err: unknown): PromptComponentFailure[] | null {
  if (!(err instanceof ApiError) || err.status !== 400 || errorCode(err) !== 'component_breaks_usages') return null
  return normalizeFailures(err.body?.failures)
}

/** 保存组件被拒的原因：修订冲突 / 被引用（改标识）/ 会让引用它的覆盖校验失败；其他错误返回 null（直接提示 detail） */
export type ComponentRejection =
  | { kind: 'stale' }
  | { kind: 'in_use'; usages: PromptComponentUsage[]; detail: string }
  | { kind: 'breaks'; failures: PromptComponentFailure[] }

export function classifySaveError(err: unknown): ComponentRejection | null {
  if (isStaleRevision(err)) return { kind: 'stale' }
  const usages = readUsages(err)
  if (usages) return { kind: 'in_use', usages, detail: (err as ApiError).detail }
  const failures = readFailures(err)
  if (failures) return { kind: 'breaks', failures }
  return null
}

/** 引用方的编辑页地址：Skill → 技能库并打开抽屉；覆盖 → 提示词模板并选中该 key */
export function usageHref(u: Pick<PromptComponentUsage, 'kind' | 'id' | 'key'>): string | null {
  if (u.kind === 'skill') return u.id ? `/skills?skill=${u.id}` : null
  return u.key ? `/prompt-templates?key=${encodeURIComponent(u.key)}` : null
}

export function usageLabel(u: Pick<PromptComponentUsage, 'kind' | 'enabled'>): string {
  if (u.kind === 'skill') return 'Skill'
  return u.enabled === false ? '模板覆盖（已停用）' : '模板覆盖'
}

const IMPORT_LABELS = { created: '新建', updated: '更新', unchanged: '不变', invalid: '无效' } as const
export const COMPONENT_IMPORT_LABELS: Record<keyof typeof IMPORT_LABELS, string> = IMPORT_LABELS

/** 「新建 2 个、更新 1 个、无效 1 个」；没有条目时说明文件为空 */
export function summarizeComponentImport(r: PromptComponentImportResult): string {
  const counts = new Map<string, number>()
  for (const item of r.items) counts.set(item.status, (counts.get(item.status) ?? 0) + 1)
  const parts = (Object.keys(IMPORT_LABELS) as Array<keyof typeof IMPORT_LABELS>)
    .filter((k) => counts.get(k))
    .map((k) => `${IMPORT_LABELS[k]} ${counts.get(k)} 个`)
  return parts.length ? parts.join('、') : '文件中没有组件'
}
