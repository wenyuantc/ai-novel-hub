import { runGeneration } from './sse'
import type { WizardItemMap, WizardKind, WizardRequest } from './types'

interface WizardDone<K extends WizardKind> {
  kind: K
  items: WizardItemMap[K][]
}

export function runWizard<K extends WizardKind>(
  projectId: number,
  kind: K,
  body: WizardRequest,
  onDelta: (text: string) => void,
  signal?: AbortSignal,
): Promise<WizardItemMap[K][]> {
  return runGeneration<WizardDone<K>>(`/projects/${projectId}/wizard/${kind}`, body, { onDelta, signal }).then(
    (d) => d.items,
  )
}

export const WIZARD_KIND_LABELS: Record<WizardKind, string> = {
  world: '世界观',
  characters: '角色',
  outline: '大纲',
}
