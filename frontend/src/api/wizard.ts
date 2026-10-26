import { api } from './client'
import type { OutlineMode, Task, WizardItemMap, WizardKind, WizardProgress, WizardProgressUpdate, WizardRequest } from './types'

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

/** 大纲向导 done.result.mode（append 时 items 只含新增条目）；缺省按 replace */
export function parseWizardMode(result: unknown): OutlineMode {
  if (typeof result !== 'object' || result === null) return 'replace'
  return (result as { mode?: unknown }).mode === 'append' ? 'append' : 'replace'
}

export const OUTLINE_MODE_LABELS: Record<OutlineMode, string> = {
  replace: '覆盖',
  append: '追加',
}

/** 向导完成页的步骤名（wizard_progress.current 取值之一） */
export const WIZARD_FINISH_STEP = 'finish'

/**
 * 进入向导时定位的步骤下标（steps.length = 完成页）：
 * current 是已知步骤 → 直接定位；否则取第一个既没完成也没跳过的步骤；全部处理过 → 完成页。
 */
export function resumeWizardStep(progress: WizardProgress | null | undefined, steps: readonly string[]): number {
  if (!progress) return 0
  if (progress.current === WIZARD_FINISH_STEP) return steps.length
  const cur = progress.current === null ? -1 : steps.indexOf(progress.current)
  if (cur >= 0) return cur
  const handled = new Set([...progress.done, ...progress.skipped])
  const first = steps.findIndex((s) => !handled.has(s))
  if (first >= 0) return first
  return handled.size > 0 ? steps.length : 0
}

/**
 * 完成 / 跳过一步后的进度更新（PATCH 载荷）：完成 → 记入 done 并移出 skipped；跳过 → 未完成过时记入 skipped；
 * current 设为下一步（null = 清空）。
 */
export function markWizardStep(
  progress: WizardProgress | null | undefined,
  step: string,
  action: 'done' | 'skip',
  next: string | null,
): WizardProgressUpdate {
  const done = [...(progress?.done ?? [])]
  let skipped = [...(progress?.skipped ?? [])]
  if (action === 'done') {
    if (!done.includes(step)) done.push(step)
    skipped = skipped.filter((s) => s !== step)
  } else if (!done.includes(step) && !skipped.includes(step)) {
    skipped.push(step)
  }
  return { done, skipped, current: next }
}
