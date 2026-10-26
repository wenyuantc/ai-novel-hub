import { api } from './client'
import type { Task, WizardItemMap, WizardKind, WizardRequest } from './types'

/** 向导：创建后台任务（202）；结果在任务 done.result = {kind, items} 中。 */
export function runWizard(projectId: number, kind: WizardKind, body: WizardRequest): Promise<Task> {
  return api.post<Task>(`/projects/${projectId}/wizard/${kind}`, body)
}

/** 校验向导任务的 done.result；kind 不匹配或结构不对时返回 null。 */
export function parseWizardDone<K extends WizardKind>(kind: K, result: unknown): WizardItemMap[K][] | null {
  if (typeof result !== 'object' || result === null) return null
  const r = result as { kind?: unknown; items?: unknown }
  if (r.kind !== kind || !Array.isArray(r.items)) return null
  return r.items as WizardItemMap[K][]
}

/** 角色 / 职业向导 done.result.warnings（职业对不上、超出上限等被忽略的条目）；没有时为空数组 */
export function parseWizardWarnings(result: unknown): string[] {
  if (typeof result !== 'object' || result === null) return []
  const w = (result as { warnings?: unknown }).warnings
  return Array.isArray(w) ? w.filter((x): x is string => typeof x === 'string') : []
}

export const WIZARD_KIND_LABELS: Record<WizardKind, string> = {
  world: '世界观',
  careers: '职业体系',
  characters: '角色',
  outline: '大纲',
}
