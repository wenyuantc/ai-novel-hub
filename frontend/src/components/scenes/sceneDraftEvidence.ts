import type { RichDocument, RichDocumentOut, RichDocumentSave, RichMarker } from '../../api/richDocument'
import { richDocumentCapacityProblem } from './richDocumentCapacity'
import { snapshotInvalidatedMarker } from './richMarkerMapping'
import type { RichEditEvidence, SceneEditEvidence } from './sceneDraftRecord'
import { validateSectionTitle } from './richSectionNode'
import { sameSectionDocument } from './richSectionProjection'
import { validSectionDocument } from './richSectionValidity'

export const sameDocument = (a: RichDocument, b: RichDocument): boolean => a === b || sameSectionDocument(a, b)

export const emptyEvidence = (): SceneEditEvidence => ({
  mappedMarkerKeys: [], restoredMarkerKeys: [], mappedSectionKeys: [], restoredSectionKeys: [],
  explicitMarkerIntent: false, explicitSectionIntent: false,
})

/** 跨快照继续编辑时仅保留现在存活的目标；删除再恢复必须来自最后相关事务。 */
export function appendSceneEvidence(previous: SceneEditEvidence, next: RichEditEvidence, document: RichDocument,
  restoredMarkers: readonly string[], restoredSections: readonly string[]): SceneEditEvidence {
  const keep = (before: readonly string[], after: readonly string[], targets: Set<string>) =>
    [...new Set([...before, ...after].filter((key) => targets.has(key)))]
  const markers = new Set(document.markers.map((item) => item.key))
  const sections = new Set(document.sections.map((item) => item.key))
  return {
    mappedMarkerKeys: keep(previous.mappedMarkerKeys, next.mappedMarkerKeys, markers),
    mappedSectionKeys: keep(previous.mappedSectionKeys, next.mappedSectionKeys ?? [], sections),
    explicitMarkerIntent: previous.explicitMarkerIntent || next.explicitMarkerIntent,
    explicitSectionIntent: previous.explicitSectionIntent || !!next.explicitSectionIntent,
    restoredMarkerKeys: keep(previous.restoredMarkerKeys, restoredMarkers, markers),
    restoredSectionKeys: keep(previous.restoredSectionKeys, restoredSections, sections),
  }
}

/** 保存确认后补齐这次新持久化的退场身份，跨保存历史也用相同规则。 */
export function historyRetiredSections(document: RichDocument, saved: RichDocument): string[] {
  const active = new Set(document.sections.map((item) => item.key))
  const retired = new Set([...document.retired_section_keys, ...saved.retired_section_keys])
  for (const item of saved.sections) if (!active.has(item.key)) retired.add(item.key)
  for (const key of active) retired.delete(key)
  return [...retired]
}

/** 回执只更新派生资料。改变期间捕获的作者草稿片段继续保留。 */
export function rebaseDraftDocument(document: RichDocument, saved: RichDocument): RichDocument {
  const retired = historyRetiredSections(document, saved)
  const confirmed = new Map(saved.markers.map((item) => [item.key, item]))
  const sectionText = new Map(saved.sections.map((section) => [section.key, section.content]))
  const characters = new Map<string | null, string[]>()
  const oldFields = (item: RichMarker) => ({
    excerpt: item.excerpt, excerpt_truncated: item.excerpt_truncated, last_start: item.last_start,
    draft_excerpt: item.draft_excerpt ?? null, draft_excerpt_truncated: item.draft_excerpt_truncated ?? false,
    draft_last_start: item.draft_last_start ?? null,
  })
  const markers = document.markers.map((marker) => {
    const old = confirmed.get(marker.key)
    if (marker.status !== 'stale' || !old || old.section_key !== marker.section_key) return marker
    // 同一个已失效身份保留服务端已记录的历史片段；只允许可编辑的颜色等字段变化。
    if (old.status === 'stale') return { ...marker, ...oldFields(old) }
    let chars = characters.get(old.section_key)
    if (!chars) {
      const text = old.section_key === null ? saved.content : sectionText.get(old.section_key)
      if (text === undefined) return marker
      chars = Array.from(text)
      characters.set(old.section_key, chars)
    }
    const confirmedRange = snapshotInvalidatedMarker(old, chars)
    const draftExcerpt = marker.draft_excerpt ?? marker.excerpt
    const draftStart = marker.draft_last_start ?? marker.last_start
    const truncated = marker.draft_excerpt == null ? marker.excerpt_truncated : !!marker.draft_excerpt_truncated
    const differs = !!draftExcerpt && (draftExcerpt !== confirmedRange.excerpt || truncated !== confirmedRange.excerpt_truncated
      || draftStart !== confirmedRange.last_start)
    return {
      ...marker,
      excerpt: confirmedRange.excerpt, excerpt_truncated: confirmedRange.excerpt_truncated, last_start: confirmedRange.last_start,
      draft_excerpt: differs ? draftExcerpt : null, draft_excerpt_truncated: differs && truncated, draft_last_start: differs ? draftStart : null,
    }
  })
  const next = { ...document, retired_section_keys: retired, markers }
  return sameDocument(next, document) ? document : next
}

/** 恢复记录与编辑快照均通过此处重建请求，不发来源不明的协议字段。 */
export function documentSave(source: RichDocumentOut, draft: RichDocument, evidence: SceneEditEvidence,
  retiredMarkers: ReadonlySet<string>): RichDocumentSave {
  const capacityProblem = richDocumentCapacityProblem(draft)
  if (capacityProblem) throw new Error(capacityProblem)
  if (!validSectionDocument(draft)) throw new Error('文档位置或格式无效，草稿未提交。')
  for (const item of draft.sections) {
    const problem = validateSectionTitle(item.title)
    if (problem) throw new Error(`区块名称无效：${problem}`)
  }
  const old = source.document
  const originalSections = new Map(old.sections.map((item) => [item.key, item]))
  const nextSections = new Set(draft.sections.map((item) => item.key))
  const retiredSections = new Set(old.retired_section_keys)
  const restoredSections = [...nextSections].filter((key) => !originalSections.has(key) && retiredSections.has(key))
  const restoredSectionEvidence = new Set(evidence.restoredSectionKeys)
  if (restoredSections.some((key) => !restoredSectionEvidence.has(key))) throw new Error('缺少区块的历史重做事务，草稿未提交。')
  const addedSections = [...nextSections].filter((key) => !originalSections.has(key) && !retiredSections.has(key))
  const deletedSections = [...originalSections.keys()].filter((key) => !nextSections.has(key))
  if ((addedSections.length || restoredSections.length || deletedSections.length) && !evidence.explicitSectionIntent) {
    throw new Error('缺少明确的区块增删事务，草稿未提交。')
  }
  const expectedRetired = new Set([...retiredSections, ...deletedSections])
  for (const key of restoredSections) expectedRetired.delete(key)
  if (expectedRetired.size !== draft.retired_section_keys.length || draft.retired_section_keys.some((key) => !expectedRetired.has(key))) {
    throw new Error('区块退场身份与已保存版本不一致，草稿未提交。')
  }
  const mappedSections = new Set(evidence.mappedSectionKeys)
  for (const item of draft.sections) {
    const before = originalSections.get(item.key)
    if (!before || mappedSections.has(item.key)) continue
    if (old.content !== draft.content || before.content !== item.content || JSON.stringify(before.formatting) !== JSON.stringify(item.formatting)) {
      throw new Error('区块锚点缺少可靠事务映射，草稿未提交。')
    }
    if ((before.anchor !== item.anchor || before.order !== item.order || before.title !== item.title
      || before.include_in_ai !== item.include_in_ai) && !evidence.explicitSectionIntent) {
      throw new Error('区块资料缺少明确事务，草稿未提交。')
    }
  }
  const original = new Map(old.markers.map((item) => [item.key, item]))
  const next = new Set(draft.markers.map((item) => item.key))
  const restored = [...next].filter((key) => !original.has(key) && retiredMarkers.has(key))
  const restoredMarkerEvidence = new Set(evidence.restoredMarkerKeys)
  if (restored.some((key) => !restoredMarkerEvidence.has(key))) throw new Error('缺少标记的历史重做事务，草稿未提交。')
  const added = [...next].filter((key) => !original.has(key) && !retiredMarkers.has(key))
  const deleted = [...original.keys()].filter((key) => !next.has(key))
  if ((added.length || restored.length || deleted.length) && !evidence.explicitMarkerIntent) throw new Error('缺少明确的标记增删事务，草稿未提交。')
  const mappedMarkers = new Set(evidence.mappedMarkerKeys)
  const nextSectionText = new Map(draft.sections.map((item) => [item.key, item.content]))
  for (const item of draft.markers) {
    const before = original.get(item.key)
    if (!before) continue
    if (before.section_key !== item.section_key) throw new Error('标记目标与已保存版本不一致，草稿未提交。')
    if (mappedMarkers.has(item.key)) continue
    const oldText = before.section_key === null ? old.content : originalSections.get(before.section_key)?.content
    const newText = item.section_key === null ? draft.content : nextSectionText.get(item.section_key)
    if ((before.status === 'active' && oldText !== newText)
      || before.status === 'stale' && item.status === 'active'
      || JSON.stringify(before) !== JSON.stringify(item) && !evidence.explicitMarkerIntent) {
      throw new Error('标记缺少可靠事务映射，草稿未提交。')
    }
  }
  return {
    expected_content_revision: source.content_revision,
    document: draft,
    add_section_keys: addedSections, delete_section_keys: deletedSections, restore_section_keys: restoredSections,
    add_marker_keys: added, delete_marker_keys: deleted, restore_marker_keys: restored,
    mapped_section_keys: [...originalSections.keys()].filter((key) => nextSections.has(key) && mappedSections.has(key)),
    mapped_marker_keys: [...original.keys()].filter((key) => next.has(key) && mappedMarkers.has(key)),
  }
}
