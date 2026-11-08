import { getSchema } from '@tiptap/core'
import { history, redo, undo } from '@tiptap/pm/history'
import { EditorState } from '@tiptap/pm/state'
import { StarterKit } from '@tiptap/starter-kit'
import { describe, expect, it } from 'vitest'
import type { RichDocument } from '../../api/richDocument'
import { richCursorCandidates } from './richEditorPositions'
import { RichMarkerDocument } from './richMarkerState'
import { AuthorSection } from './richSectionNode'
import { editorFromSectionDocument, projectSectionDocument, sameSectionDocument } from './richSectionProjection'
import { createSectionPlanTransaction, mappedSectionIdentities, verifySectionMetadataTransaction, verifySectionPlanTransaction } from './richSectionTransaction'
import { FocusableListItem, RichPrefixes } from './richTextExtensions'

const schema = getSchema([StarterKit.configure({ document: false, trailingNode: false, code: false, codeBlock: false,
  horizontalRule: false, hardBreak: false, link: false, strike: false, underline: false, listItem: false, listKeymap: false }),
RichMarkerDocument, FocusableListItem, RichPrefixes, AuthorSection])
const format = () => ({ version: 1 as const, boundaries: [], inline: [], blocks: [] })
const original = (): RichDocument => ({ content: '甲🙂\r\n乙', formatting: format(), sections: [
  { key: 'a', anchor: 1, order: 0, title: '备选', content: '旧🙂', formatting: format(), include_in_ai: false },
], retired_section_keys: [], markers: [{ key: 'm', section_key: 'a', status: 'active', start: 0, end: 2,
  color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null }] })
const setup = (document: RichDocument) => EditorState.create({ schema, doc: schema.nodeFromJSON(editorFromSectionDocument(document)), plugins: [history()] })

describe('显式区块事务证明', () => {
  it('正文及区块内部输入只信任由真实 StepMap 跟踪到的节点位置', () => {
    const saved = original(); const state = setup(saved)
    const prose = richCursorCandidates(state.doc, { scope: 'prose', offset: 1 })[0].pm
    const section = richCursorCandidates(state.doc, { scope: 'section', sectionKey: 'a', offset: 1 })[0].pm
    expect(mappedSectionIdentities(state.tr.insertText('X', prose))).toEqual(['a'])
    expect(mappedSectionIdentities(state.tr.insertText('X', section))).toEqual(['a'])
    let from = -1; let node = state.doc.firstChild!
    state.doc.forEach((child, pos) => { if (child.type.name === 'authorSection') { from = pos; node = child } })
    expect(mappedSectionIdentities(state.tr.replaceWith(from, from + node.nodeSize, node))).toBeNull()
  })
  it('新增和撤销/重做保持正文、同锚次序、区块和标记', () => {
    const saved = original(); let state = setup(saved)
    const transaction = createSectionPlanTransaction(state, { action: 'create', key: 'b', cursor: {
      scope: 'prose', offset: 1, leftSection: 'a', rightSection: null,
    } }, saved)
    expect(verifySectionPlanTransaction(transaction, state, saved)).toBe(true)
    state = state.apply(transaction)
    const created = projectSectionDocument(state.doc.toJSON())
    expect(created.content).toBe(saved.content)
    expect(created.sections.map((item) => item.key)).toEqual(['a', 'b'])
    expect(created.markers).toEqual(saved.markers)
    expect(undo(state, (tr) => { state = state.apply(tr) })).toBe(true)
    expect(sameSectionDocument(projectSectionDocument(state.doc.toJSON()), saved)).toBe(true)
    expect(redo(state, (tr) => { state = state.apply(tr) })).toBe(true)
    expect(sameSectionDocument(projectSectionDocument(state.doc.toJSON()), created)).toBe(true)
  })
  it('移动或删除是整个计划事务，删除时附属标记和墓碑进入同一历史', () => {
    const saved = original(); let state = setup(saved)
    const move = createSectionPlanTransaction(state, { action: 'move', key: 'a', cursor: {
      scope: 'prose', offset: 4, leftSection: null, rightSection: null,
    } }, saved)
    expect(verifySectionPlanTransaction(move, state, saved)).toBe(true)
    state = state.apply(move)
    const moved = projectSectionDocument(state.doc.toJSON())
    expect(moved.sections[0].anchor).toBe(4)
    expect(moved.markers).toEqual(saved.markers)
    const deletion = createSectionPlanTransaction(state, { action: 'delete', key: 'a' }, saved)
    expect(verifySectionPlanTransaction(deletion, state, saved)).toBe(true)
    state = state.apply(deletion)
    expect(projectSectionDocument(state.doc.toJSON())).toMatchObject({ sections: [], retired_section_keys: ['a'],
      markers: [{ key: 'm', status: 'stale', excerpt: '旧🙂' }] })
    expect(undo(state, (tr) => { state = state.apply(tr) })).toBe(true)
    expect(sameSectionDocument(projectSectionDocument(state.doc.toJSON()), moved)).toBe(true)
  })
  it('额外替换或重新输入同字不能借显式区块意图伪造可靠映射', () => {
    const saved = original(); const state = setup(saved)
    const transaction = createSectionPlanTransaction(state, { action: 'create', key: 'b', cursor: {
      scope: 'prose', offset: 1, leftSection: 'a', rightSection: null,
    } }, saved)
    transaction.insertText('甲', 1, 2)
    expect(verifySectionPlanTransaction(transaction, state, saved)).toBe(false)
  })
  it('节点元数据意图严格限定为单个原区块标题或 AI 开关', () => {
    const saved = original(); const state = setup(saved)
    let pos = -1
    state.doc.forEach((node, at) => { if (node.type.name === 'authorSection') pos = at })
    const node = state.doc.nodeAt(pos)!
    const transaction = state.tr.setNodeMarkup(pos, undefined, { ...node.attrs, title: '新名', include_in_ai: true })
      .setMeta('rich-section-intent', { action: 'metadata', key: 'a' })
    expect(verifySectionMetadataTransaction(transaction, state)).toBe(true)
    transaction.insertText('甲', 1, 2)
    expect(verifySectionMetadataTransaction(transaction, state)).toBe(false)
  })
})
