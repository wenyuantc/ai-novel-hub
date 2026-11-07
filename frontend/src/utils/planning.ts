import { ApiError } from '../api/client'
import type {
  MatrixSettingCell, PlanningBatch, PlanningCollectionOp, PlanningReference, PlanningReferenceKind,
  PlanningReferenceOperation, PlanningRow, PlanningSource, PlanningTone, SubplotNode,
} from '../api/types'

export const PLANNING_REFERENCE_LABELS: Record<PlanningReferenceKind, string> = { character: '角色', world: '世界观', organization: '组织', relationship: '人物关系', career: '职业' }
export const PLANNING_OP_LABELS: Record<PlanningCollectionOp | 'keep', string> = { keep: '保持不变', add: '追加', remove: '移除', replace: '替换全部', clear: '明确无（清空）', inherit: '恢复继承本章' }
export const PLANNING_TONES: { value: PlanningTone; label: string }[] = [
  { value: 'default', label: '默认' }, { value: 'blue', label: '蓝色' }, { value: 'green', label: '绿色' }, { value: 'gold', label: '金色' },
  { value: 'orange', label: '橙色' }, { value: 'red', label: '红色' }, { value: 'cyan', label: '青色' }, { value: 'purple', label: '紫色' },
]
export const planningSourceKey = (source: PlanningSource) => `${source.kind}:${source.id}`
export const planningReferenceKey = (reference: PlanningReference) => `${reference.kind}:${reference.kind === 'relationship' ? reference.timeline_id : reference.id}`
export const planningReferenceId = (reference: PlanningReference) => reference.kind === 'relationship' ? reference.timeline_id : reference.id
export function planningRowLabel(row: PlanningRow): string {
  const prefix = row.chapter_no !== null ? `第 ${row.chapter_no} 章` : row.source.kind === 'detached' ? '待重新关联' : `细纲 ${row.seq ?? '—'}`
  return `${prefix}${row.source.kind === 'scene' ? ` · 场景 ${(row.scene_sort ?? 0) + 1}` : ''} · ${row.title || '未命名'}`
}
export function planningRowLink(pid: number, row: PlanningRow): string | null {
  if (row.chapter_id !== null) return `/projects/${pid}/chapters/${row.chapter_id}${row.source.kind === 'scene' ? `?scene_id=${row.source.id}` : ''}`
  if (row.outline_id !== null) return `/projects/${pid}/outlines?outline_id=${row.outline_id}${row.plan_id === null ? '' : `&plan_id=${row.plan_id}`}`
  return null
}
export function settingCellDescription(cell: MatrixSettingCell): string {
  const parts = [cell.manual ? '手动关联' : '', cell.pov ? 'POV' : ''].filter(Boolean)
  if (!cell.tracking_supported) parts.push('无自动追踪')
  else if (!cell.has_content) parts.push('未写')
  else {
    if (cell.mention_count) parts.push(`正文 ${cell.mention_count}`)
    if (cell.ambiguous_count) parts.push(`歧义 ${cell.ambiguous_count}`)
    if (!cell.mention_count && !cell.ambiguous_count) parts.push('无正文提及')
  }
  return parts.join(' · ')
}
export interface CollectionDraft { op: PlanningCollectionOp | 'keep'; ids: number[] }
export interface PlanningBatchDraft { pov: 'keep' | 'set' | 'clear' | 'inherit'; povId?: number; tags: CollectionDraft; references: Partial<Record<PlanningReferenceKind, CollectionDraft>> }
export function emptyBatchDraft(): PlanningBatchDraft { return { pov: 'keep', tags: { op: 'keep', ids: [] }, references: {} } }
/** 只提交作者明确选择的类型，clear 永不复用残留的下拉框值。 */
export function batchPayload(revision: number, targets: PlanningSource[], draft: PlanningBatchDraft, rows: PlanningRow[] = []): PlanningBatch {
  if (!targets.length) throw new Error('请先选择目标章节或细纲')
  if (targets.length > 100) throw new Error('一次最多调整 100 个目标')
  if (new Set(targets.map(planningSourceKey)).size !== targets.length) throw new Error('目标不能重复')
  const body: PlanningBatch = { expected_revision: revision, targets }
  if (draft.pov === 'set') {
    if (!draft.povId) throw new Error('请选择视角角色')
    body.pov = { op: 'set', character_id: draft.povId }
  } else if (draft.pov === 'clear' || draft.pov === 'inherit') body.pov = { op: draft.pov }
  if (draft.tags.op !== 'keep') body.tags = ['clear', 'inherit'].includes(draft.tags.op) ? { op: draft.tags.op } : { op: draft.tags.op, ids: [...new Set(draft.tags.ids)] }
  const references: PlanningReferenceOperation[] = []
  for (const kind of Object.keys(PLANNING_REFERENCE_LABELS) as PlanningReferenceKind[]) {
    const choice = draft.references[kind]
    if (!choice || choice.op === 'keep') continue
    const empty = choice.op === 'clear' || choice.op === 'inherit'
    const ids = empty ? [] : [...new Set(choice.ids)]
    if (kind === 'relationship') references.push({ kind, op: choice.op, ...(empty ? {} : { timeline_ids: ids }) })
    else references.push({ kind, op: choice.op, ...(empty ? {} : { ids }) })
  }
  if (references.length) body.references = references
  if (!body.pov && !body.tags && !references.length) throw new Error('请选择至少一项修改')
  if (targets.some((target) => target.kind === 'scene')) {
    const chapters = new Map<number, number>()
    for (const target of targets.filter((item) => item.kind === 'scene')) {
      const row = rows.find((item) => item.source.kind === 'scene' && item.source.id === target.id)
      if (!row?.chapter_id || row.content_revision === undefined || row.content_revision === null) throw new Error('请先重新读取场景内容版本')
      if (chapters.has(row.chapter_id) && chapters.get(row.chapter_id) !== row.content_revision) throw new Error('同章场景版本不一致，请重新读取')
      chapters.set(row.chapter_id, row.content_revision)
    }
    body.expected_content_revisions = [...chapters].map(([chapter_id, content_revision]) => ({ chapter_id, content_revision }))
  }
  return body
}
function record(value: unknown): value is Record<string, unknown> { return typeof value === 'object' && value !== null && !Array.isArray(value) }
function positive(value: unknown): value is number { return typeof value === 'number' && Number.isSafeInteger(value) && value > 0 }
function node(value: unknown): value is SubplotNode {
  return record(value) && positive(value.id) && positive(value.subplot_id) && positive(value.unit_id) && typeof value.content === 'string'
}
export interface PlanningMergeConflict {
  revision: number
  unitIds: [number, number]
  survivorUnitId: number
  povIds: [number | null, number | null]
  povNames: Record<number, string>
  nodeConflicts: { subplot_id: number; subplot_name?: string; nodes: SubplotNode[] }[]
  labels: Record<number, string>
}
export function readPlanningConflict(error: unknown): PlanningMergeConflict | null {
  if (!(error instanceof ApiError) || error.status !== 409 || error.body?.code !== 'planning_merge_conflict') return null
  const body = error.body
  if (typeof body.current_revision !== 'number' || !Number.isSafeInteger(body.current_revision) || body.current_revision < 0) return null
  if (!Array.isArray(body.unit_ids) || body.unit_ids.length !== 2 || !body.unit_ids.every(positive) || body.unit_ids[0] === body.unit_ids[1]) return null
  const ids: [number, number] = [body.unit_ids[0], body.unit_ids[1]]
  if (!Array.isArray(body.pov_character_ids) || body.pov_character_ids.length !== 2 || !body.pov_character_ids.every((v) => v === null || positive(v))) return null
  const povIds: [number | null, number | null] = [body.pov_character_ids[0] as number | null, body.pov_character_ids[1] as number | null]
  if (!Array.isArray(body.node_conflicts)) return null
  const conflicts: PlanningMergeConflict['nodeConflicts'] = []
  for (const conflict of body.node_conflicts) {
    if (!record(conflict) || !positive(conflict.subplot_id) || !Array.isArray(conflict.nodes) || conflict.nodes.length !== 2 || !conflict.nodes.every(node)) return null
    conflicts.push({ subplot_id: conflict.subplot_id, subplot_name: typeof conflict.subplot_name === 'string' ? conflict.subplot_name : undefined, nodes: conflict.nodes })
  }
  const labels: Record<number, string> = {}
  const povNames: Record<number, string> = {}
  if (Array.isArray(body.units)) for (const unit of body.units) {
    if (record(unit) && positive(unit.unit_id) && typeof unit.title === 'string') labels[unit.unit_id] = `${typeof unit.chapter_no === 'number' ? `第 ${unit.chapter_no} 章` : '细纲／待关联'} · ${unit.title}`
    if (record(unit) && positive(unit.pov_character_id) && typeof unit.pov_character_name === 'string') povNames[unit.pov_character_id] = unit.pov_character_name
  }
  const survivor = positive(body.survivor_unit_id) && ids.includes(body.survivor_unit_id) ? body.survivor_unit_id : ids[0]
  return { revision: body.current_revision, unitIds: ids, survivorUnitId: survivor, povIds, povNames, nodeConflicts: conflicts, labels }
}
