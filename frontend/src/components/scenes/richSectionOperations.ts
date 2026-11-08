import type { RichDocument, RichSection } from '../../api/richDocument'
import type { RichCursor } from './richEditorPositions'
import { snapshotInvalidatedMarker } from './richMarkerMapping'

/** 显式区块操作的纯计划，仍须通过编辑器的结构门禁。 */
export class SectionOperationError extends Error {}

const sorted = (items: RichSection[]) => [...items].sort((a, b) => a.anchor - b.anchor || a.order - b.order || a.key.localeCompare(b.key))
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)
function insertionIndex(group: RichSection[], left: string | null, right: string | null): number {
  const index = left === null ? 0 : group.findIndex((item) => item.key === left) + 1
  if (index < 0 || left !== null && (index === 0 || group[index - 1]?.key !== left)
    || (group[index]?.key ?? null) !== right) throw new SectionOperationError('区块边界已变化，请重新选择插入位置。')
  return index
}
function validateCursor(text: string, cursor: RichCursor): asserts cursor is Extract<RichCursor, { scope: 'prose' }> {
  if (cursor.scope !== 'prose') throw new SectionOperationError('请选择正式正文中的位置，不能嵌套区块。')
  const chars = Array.from(text)
  if (!Number.isSafeInteger(cursor.offset) || cursor.offset < 0 || cursor.offset > chars.length
    || cursor.offset > 0 && cursor.offset < chars.length && chars[cursor.offset - 1] === '\r' && chars[cursor.offset] === '\n') {
    throw new SectionOperationError('区块位置无效或位于 CRLF 中间。')
  }
}

/** 插入点必须同时携带同锚区块的左右身份，不能凭正文整数偏移猜前后。 */
export function planSectionCreation(document: RichDocument, cursor: RichCursor, key: string): RichDocument {
  validateCursor(document.content, cursor)
  if (!key || document.sections.some((item) => item.key === key) || document.retired_section_keys.includes(key)) {
    throw new SectionOperationError('区块身份已存在，请重新创建。')
  }
  const group = sorted(document.sections.filter((item) => item.anchor === cursor.offset))
  const index = insertionIndex(group, cursor.leftSection, cursor.rightSection)
  const section: RichSection = { key, anchor: cursor.offset, order: 0, title: '新建区块', content: '',
    formatting: { version: 1, boundaries: [], inline: [], blocks: [] }, include_in_ai: false }
  group.splice(index, 0, section)
  const sections = document.sections.filter((item) => item.anchor !== cursor.offset)
  return { ...document, sections: sorted([...sections, ...group.map((item, order) => ({ ...item, order }))]) }
}

/** 仅移动区块身份，正文、格式、其他区块内容和全部标记不参与重定位。 */
export function planSectionMove(document: RichDocument, key: string, cursor: RichCursor): RichDocument {
  validateCursor(document.content, cursor)
  const original = sorted(document.sections)
  const section = original.find((item) => item.key === key)
  if (!section) throw new SectionOperationError('区块已不存在，请重新加载。')
  const oldGroup = original.filter((item) => item.anchor === cursor.offset)
  // 先验证原始目标边向，防止把过时的相邻身份当作新插入点。
  const originalIndex = insertionIndex(oldGroup, cursor.leftSection, cursor.rightSection)
  const preceding = oldGroup.slice(0, originalIndex).filter((item) => item.key !== key).length
  const target = oldGroup.filter((item) => item.key !== key)
  target.splice(preceding, 0, { ...section, anchor: cursor.offset })
  const other = original.filter((item) => item.anchor !== cursor.offset && item.key !== key)
  return { ...document, sections: sorted([...other, ...target.map((item, order) => ({ ...item, order }))]) }
}

/** 删除不会抹掉附属标记：活动标记先按目标原文失效。 */
export function planSectionDeletion(document: RichDocument, key: string, saved: RichDocument): RichDocument {
  const section = document.sections.find((item) => item.key === key)
  if (!section) throw new SectionOperationError('区块已不存在，请重新加载。')
  const persisted = saved.sections.some((item) => item.key === key) || saved.retired_section_keys.includes(key)
  if (!persisted && document.markers.some((item) => item.section_key === key)) {
    throw new SectionOperationError('此新区块含有尚未保存的标记，请先保存，再删除区块以保留标记记录。')
  }
  const chars = Array.from(section.content)
  const markers = document.markers.map((marker) => {
    if (marker.section_key !== key || marker.status !== 'active') return marker
    const captured = snapshotInvalidatedMarker(marker, chars, saved)
    const previous = saved.markers.find((item) => item.key === marker.key && item.section_key === key)
    if (previous?.status !== 'stale') return captured
    // 上次保存的失效片段不可被未经服务端验证的中间状态覆盖。
    if (captured.excerpt !== previous.excerpt || captured.excerpt_truncated !== previous.excerpt_truncated
      || captured.last_start !== previous.last_start) throw new SectionOperationError('已恢复标记的草稿原文有变化，请先保存再删除区块。')
    return previous
  })
  const retired = persisted && !document.retired_section_keys.includes(key)
    ? [...document.retired_section_keys, key] : document.retired_section_keys
  return { ...document, sections: document.sections.filter((item) => item.key !== key), retired_section_keys: retired, markers }
}

/** 不把规范化顺序值当成新增/丢失区块；顺序身份与各自内容仍必须逐项一致。 */
export function equivalentSectionMaterials(a: RichDocument, b: RichDocument): boolean {
  const material = (item: RichSection) => ({ ...item, order: 0 })
  return a.content === b.content && same(a.formatting, b.formatting) && same(a.retired_section_keys, b.retired_section_keys)
    && same(a.markers, b.markers) && same(sorted(a.sections).map(material), sorted(b.sections).map(material))
}
