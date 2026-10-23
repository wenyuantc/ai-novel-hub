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

export const WIZARD_KIND_LABELS: Record<WizardKind, string> = {
  world: '世界观',
  characters: '角色',
  outline: '大纲',
}
