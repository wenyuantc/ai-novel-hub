import type { RichDocument, RichFormatting, RichMarker, RichSection } from '../../api/richDocument'

export interface DraftChange<T> { key: string; remote?: T; local?: T }

const STYLE_LABELS = {
  bold: '粗体', italic: '斜体', heading: '标题', bullet_list: '无序列表', ordered_list: '有序列表', blockquote: '引用',
}

function changesByKey<T>(remote: readonly T[], local: readonly T[], keyOf: (item: T) => string, same: (a: T, b: T) => boolean) {
  const before = new Map(remote.map((item) => [keyOf(item), item]))
  const after = new Map(local.map((item) => [keyOf(item), item]))
  return [...new Set([...before.keys(), ...after.keys()])].flatMap((key): DraftChange<T>[] => {
    const r = before.get(key)
    const l = after.get(key)
    return r !== undefined && l !== undefined && same(r, l) ? [] : [{ key, remote: r, local: l }]
  })
}

function formatRanges(format: RichFormatting | undefined) {
  if (!format) return []
  return [
    ...format.boundaries.map((position) => `段落分隔 · 位置 ${position + 1}`),
    ...format.inline.map(({ kind, start, end }) => `${STYLE_LABELS[kind]} · 第 ${start + 1}–${end} 字符`),
    ...format.blocks.map(({ kind, start, end, level }) => {
      const label = kind === 'heading' && level !== null ? `${level} 级标题` : STYLE_LABELS[kind]
      return `${label} · ${start < end ? `第 ${start + 1}–${end} 字符` : `位置 ${start + 1}`}`
    }),
  ]
}

export function draftFormatChanges(remote?: RichFormatting, local?: RichFormatting): DraftChange<string>[] {
  return changesByKey(formatRanges(remote), formatRanges(local), (item) => item, (a, b) => a === b)
}

function sameSection(a: RichSection, b: RichSection) {
  return a.content === b.content && a.title === b.title && a.anchor === b.anchor && a.order === b.order
    && a.include_in_ai === b.include_in_ai && draftFormatChanges(a.formatting, b.formatting).length === 0
}

function sameMarker(a: RichMarker, b: RichMarker) {
  return a.section_key === b.section_key && a.status === b.status && a.start === b.start && a.end === b.end
    && a.color === b.color && a.excerpt === b.excerpt && a.excerpt_truncated === b.excerpt_truncated
    && a.last_start === b.last_start && a.draft_excerpt === b.draft_excerpt
    && a.draft_excerpt_truncated === b.draft_excerpt_truncated && a.draft_last_start === b.draft_last_start
}

/** 区块和标记按身份对齐，正文相同仍能看见格式、退场与归属差异。 */
export function richDraftChanges(remote: RichDocument | undefined, local: RichDocument) {
  return {
    formatting: draftFormatChanges(remote?.formatting, local.formatting),
    sections: changesByKey(remote?.sections ?? [], local.sections, (item) => item.key, sameSection),
    markers: changesByKey(remote?.markers ?? [], local.markers, (item) => item.key, sameMarker),
    retired: changesByKey(remote?.retired_section_keys ?? [], local.retired_section_keys, (item) => item, (a, b) => a === b),
  }
}
