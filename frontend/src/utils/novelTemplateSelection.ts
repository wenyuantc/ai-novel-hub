import type { NovelTemplateDependency, NovelTemplateKind, NovelTemplateSelection } from '../api/novelTemplates'

export const EMPTY_TEMPLATE_SELECTION: NovelTemplateSelection = {
  include_config: true, character_ids: [], world_ids: [], organization_ids: [], career_ids: [], outline_ids: [],
}

const KIND_FIELDS: Record<NovelTemplateKind, keyof Pick<NovelTemplateSelection, 'character_ids' | 'world_ids' | 'organization_ids' | 'career_ids'>> = {
  character: 'character_ids', world: 'world_ids', organization: 'organization_ids', career: 'career_ids',
}

/** 仅由用户明确点击后补选缺失对象；同一对象不会重复加入。 */
export function addTemplateDependencies(selection: NovelTemplateSelection, missing: NovelTemplateDependency[]): NovelTemplateSelection {
  const next = { ...selection }
  for (const dependency of missing) {
    const field = KIND_FIELDS[dependency.kind]
    if (!field || !Number.isSafeInteger(dependency.id) || dependency.id < 1) continue
    if (!next[field].includes(dependency.id)) next[field] = [...next[field], dependency.id]
  }
  return next
}

export function templateSelectionSize(selection: NovelTemplateSelection): number {
  return selection.character_ids.length + selection.world_ids.length + selection.organization_ids.length + selection.career_ids.length
}

export function templateSizeLabel(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MiB`
}
