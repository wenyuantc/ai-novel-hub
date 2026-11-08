import type { Node as PMNode } from '@tiptap/pm/model'
import type { Transaction } from '@tiptap/pm/state'
import { Transform } from '@tiptap/pm/transform'

interface TextBlock { pos: number; text: string; attrs: Record<string, unknown> }
const attributes = (node: PMNode) => ({ prefix: node.attrs.prefix, blockKey: node.attrs.blockKey ?? null,
  prefixMarks: node.attrs.prefixMarks ?? [], boundaryBefore: node.attrs.boundaryBefore ?? false, scaffold: node.attrs.scaffold ?? false })
function textBlocks(doc: PMNode): Map<string | null, TextBlock[]> | null {
  const scopes = new Map<string | null, TextBlock[]>([[null, []]])
  let valid = true
  const collect = (root: PMNode, rootPos: number, scope: string | null) => {
    const values = scopes.get(scope)!
    if (root.isTextblock) values.push({ pos: rootPos, text: root.textContent, attrs: attributes(root) })
    root.descendants((node, pos) => {
      if (node.type.name === 'authorSection') { valid = false; return false }
      if (node.isTextblock) values.push({ pos: rootPos + 1 + pos, text: node.textContent, attrs: attributes(node) })
    })
  }
  doc.forEach((node, pos) => {
    if (node.type.name !== 'authorSection') { collect(node, pos, null); return }
    const key: unknown = node.attrs.key
    if (typeof key !== 'string' || !key || scopes.has(key)) { valid = false; return }
    scopes.set(key, [])
    collect(node, pos, key)
  })
  return valid ? scopes : null
}

/** 同一正文/区块内逐段文字与顺序完全相同时恢复属性，不能按相同字面跨目标匹配。 */
export function repairRichPrefixes(before: PMNode, after: PMNode): Transform | null {
  const old = textBlocks(before); const next = textBlocks(after)
  if (!old || !next || old.size !== next.size) return null
  for (const [scope, values] of old) {
    const updated = next.get(scope)
    if (!updated || values.length !== updated.length || values.some((value, index) => value.text !== updated[index].text)) return null
  }
  const transform = new Transform(after)
  for (const [scope, values] of next) values.forEach((value, index) => {
    const previous = old.get(scope)![index]
    if (JSON.stringify(value.attrs) === JSON.stringify(previous.attrs)) return
    const node = transform.doc.nodeAt(value.pos)
    if (node) transform.setNodeMarkup(value.pos, undefined, { ...node.attrs, ...previous.attrs })
  })
  return transform
}

/** 修复事务只能逐个调整同一文字块的前缀属性，不能借修复替换字面或区块身份。 */
export function verifyRichPrefixRepair(transaction: Transaction): boolean {
  return transaction.steps.length > 0 && transaction.steps.every((item, index) => {
    const step = item.toJSON() as { stepType?: string; structure?: boolean; from?: number; to?: number; gapFrom?: number; gapTo?: number }
    if (step.stepType !== 'replaceAround' || step.structure !== true || typeof step.from !== 'number'
      || typeof step.to !== 'number' || step.gapFrom !== step.from + 1 || step.gapTo !== step.to - 1) return false
    const before = transaction.docs[index]
    const after = index + 1 < transaction.docs.length ? transaction.docs[index + 1] : transaction.doc
    const left = before.nodeAt(step.from); const right = after.nodeAt(step.from)
    if (!left?.isTextblock || !right?.isTextblock || left.type !== right.type || !left.content.eq(right.content)) return false
    const strip = (attrs: Record<string, unknown>) => Object.fromEntries(Object.entries(attrs)
      .filter(([key]) => !['prefix', 'prefixMarks', 'blockKey', 'boundaryBefore', 'scaffold'].includes(key)))
    if (JSON.stringify(strip(left.attrs)) !== JSON.stringify(strip(right.attrs))) return false
    const expected = new Transform(before).setNodeMarkup(step.from, undefined, right.attrs)
    return expected.steps.length === 1 && JSON.stringify(expected.steps[0].toJSON()) === JSON.stringify(item.toJSON())
  })
}
