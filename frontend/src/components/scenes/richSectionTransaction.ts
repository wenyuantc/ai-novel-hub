import { closeHistory } from '@tiptap/pm/history'
import type { EditorState, Transaction } from '@tiptap/pm/state'
import type { RichDocument } from '../../api/richDocument'
import type { RichCursor } from './richEditorPositions'
import { planSectionCreation, planSectionDeletion, planSectionMove } from './richSectionOperations'
import { editorFromSectionDocument, projectSectionDocument } from './richSectionProjection'

export type RichSectionPlan =
  | { action: 'create'; key: string; cursor: RichCursor }
  | { action: 'move'; key: string; cursor: RichCursor }
  | { action: 'delete'; key: string }

/** 普通文字事务只接受 PM 映射仍能逐个证明的原区块节点，不能按相同 UUID 或字面补猜。 */
export function mappedSectionIdentities(transaction: Transaction): string[] | null {
  const sections = (node: typeof transaction.doc) => {
    const found = new Map<string, number>()
    let invalid = false
    node.forEach((child, pos) => {
      if (child.type.name !== 'authorSection') return
      if (typeof child.attrs.key !== 'string' || !child.attrs.key || found.has(child.attrs.key)) invalid = true
      else found.set(child.attrs.key, pos)
    })
    return invalid ? null : found
  }
  const before = sections(transaction.before); const after = sections(transaction.doc)
  if (!before || !after || before.size !== after.size) return null
  for (const [key, pos] of before) {
    const mapped = transaction.mapping.mapResult(pos, 1)
    if (mapped.deleted || after.get(key) !== mapped.pos) return null
  }
  return [...before.keys()]
}

/** 用完整受检计划生成单个可撤销事务，不能以相同字面或剩余 UUID 冒充映射。 */
export function createSectionPlanTransaction(state: EditorState, plan: RichSectionPlan, saved: RichDocument): Transaction {
  const before = projectSectionDocument(state.doc.toJSON())
  const after = plan.action === 'create' ? planSectionCreation(before, plan.cursor, plan.key)
    : plan.action === 'move' ? planSectionMove(before, plan.key, plan.cursor)
      : planSectionDeletion(before, plan.key, saved)
  const next = state.schema.nodeFromJSON(editorFromSectionDocument(after))
  return closeHistory(state.tr).replaceWith(0, state.doc.content.size, next.content)
    .setDocAttribute('markers', next.attrs.markers).setDocAttribute('retiredSectionKeys', next.attrs.retiredSectionKeys)
    .setDocAttribute('sectionOrders', next.attrs.sectionOrders).setMeta('rich-section-plan', plan)
}

/** 重新构造并逐步骤校验，排除插入同字文本后伪称只移动区块的事务。 */
export function verifySectionPlanTransaction(transaction: Transaction, state: EditorState, saved: RichDocument): boolean {
  const plan: unknown = transaction.getMeta('rich-section-plan')
  if (!plan || typeof plan !== 'object' || !('action' in plan) || !('key' in plan)
    || typeof plan.key !== 'string' || !['create', 'move', 'delete'].includes(String(plan.action))) return false
  try {
    const expected = createSectionPlanTransaction(state, plan as RichSectionPlan, saved)
    return expected.doc.eq(transaction.doc)
      && JSON.stringify(expected.steps.map((item) => item.toJSON())) === JSON.stringify(transaction.steps.map((item) => item.toJSON()))
  } catch { return false }
}

/** 标题与 AI 开关只能改指定区块的节点属性；不得借元数据事务替换任何正文。 */
export function verifySectionMetadataTransaction(transaction: Transaction, state: EditorState): boolean {
  const intent: unknown = transaction.getMeta('rich-section-intent')
  if (!intent || typeof intent !== 'object' || !('action' in intent) || intent.action !== 'metadata'
    || !('key' in intent) || typeof intent.key !== 'string') return false
  try {
    const before = projectSectionDocument(state.doc.toJSON()); const after = projectSectionDocument(transaction.doc.toJSON())
    const oldSection = before.sections.find((item) => item.key === intent.key)
    const newSection = after.sections.find((item) => item.key === intent.key)
    if (!oldSection || !newSection) return false
    let position = -1
    state.doc.forEach((node, pos) => { if (node.type.name === 'authorSection' && node.attrs.key === intent.key) position = pos })
    if (position < 0) return false
    const node = state.doc.nodeAt(position)!
    const expected = state.tr.setNodeMarkup(position, undefined,
      { ...node.attrs, title: newSection.title, include_in_ai: newSection.include_in_ai })
    return expected.doc.eq(transaction.doc)
      && JSON.stringify(expected.steps.map((item) => item.toJSON())) === JSON.stringify(transaction.steps.map((item) => item.toJSON()))
  } catch { return false }
}
