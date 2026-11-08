import { Schema, Slice } from '@tiptap/pm/model'
import { EditorState } from '@tiptap/pm/state'
import { ReplaceAroundStep } from '@tiptap/pm/transform'
import { describe, expect, it } from 'vitest'
import type { RichMarker } from '../../api/richDocument'
import { mapMarkerTransaction, snapshotInvalidatedMarker } from './richMarkerMapping'
import type { RichDocument } from '../../api/richDocument'

const schema = new Schema({ nodes: {
  doc: { content: 'paragraph+' },
  paragraph: { content: 'text*', group: 'block', attrs: { prefix: { default: '' } } },
  text: { group: 'inline' },
} })
const marker = (start: number, end: number): RichMarker => ({ key: '12345678-1234-4123-8123-123456789def', section_key: null, status: 'active', start, end, color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null })
function state() {
  return EditorState.create({ schema, doc: schema.node('doc', null, [
    schema.node('paragraph', { prefix: '' }, schema.text('甲')),
    schema.node('paragraph', { prefix: '\r\n' }, schema.text('🙂乙')),
    schema.node('paragraph', { prefix: '\n' }, schema.text('尾')),
  ]) })
}

describe('正文标记事务映射', () => {
  it('跨 CRLF 的长范围内部插入跟随实际位置，不找相同字面', () => {
    const mapped = mapMarkerTransaction(state().tr.insertText('X', 4), [marker(0, 4)])
    expect(mapped?.[0]).toMatchObject({ status: 'active', start: 0, end: 5 })
  })
  it('局部删除保留可证明范围，完全删除保存旧片段并转失效', () => {
    const partial = mapMarkerTransaction(state().tr.delete(4, 6), [marker(0, 4)])
    expect(partial?.[0]).toMatchObject({ status: 'active', start: 0, end: 3 })
    const removed = mapMarkerTransaction(state().tr.delete(1, 6), [marker(0, 4)])
    expect(removed?.[0]).toMatchObject({ status: 'stale', start: null, end: null, excerpt: '甲\r\n🙂', last_start: 0 })
  })
  it('删除换行后的连接只丢失该分隔，后续 LF 和标记准确跟随', () => {
    const mapped = mapMarkerTransaction(state().tr.join(3), [marker(0, 4)])
    expect(mapped?.[0]).toMatchObject({ status: 'active', start: 0, end: 2 })
  })
  it('完全替换为相同字面仍失效，不能按重复字猜测', () => {
    const input = EditorState.create({ schema, doc: schema.node('doc', null, [schema.node('paragraph', { prefix: '' }, schema.text('甲甲'))]) })
    const mapped = mapMarkerTransaction(input.tr.insertText('甲', 1, 2), [marker(0, 1)])
    expect(mapped?.[0]).toMatchObject({ status: 'stale', excerpt: '甲', last_start: 0 })
  })
  it('同一真实多 range Step 删除整段时，失效摘录来自该步骤之前的完整原文', () => {
    const input = EditorState.create({ schema, doc: schema.node('doc', null, [schema.node('paragraph', { prefix: '' }, schema.text('甲🙂乙丙'))]) })
    // ReplaceAroundStep 保留空 gap，两侧的删除是同一个 StepMap 的两个 range。
    const transaction = input.tr.step(new ReplaceAroundStep(1, 6, 2, 2, Slice.empty, 0))
    const mapped = mapMarkerTransaction(transaction, [marker(0, 4)])
    expect(mapped?.[0]).toMatchObject({ status: 'stale', excerpt: '甲🙂乙丙', last_start: 0 })
  })

  it('同一事务分步骤先改动后全删，摘录保留事务开始时的范围', () => {
    const input = EditorState.create({ schema, doc: schema.node('doc', null, [schema.node('paragraph', { prefix: '' }, schema.text('甲🙂乙丙'))]) })
    const transaction = input.tr.delete(4, 5).delete(1, 5)
    const mapped = mapMarkerTransaction(transaction, [marker(0, 4)])
    expect(mapped?.[0]).toMatchObject({ status: 'stale', excerpt: '甲🙂乙丙', last_start: 0 })
  })

  it('未保存草稿经编辑后失效，同时保留已保存基线与删除前草稿片段', () => {
    const saved: RichDocument = { content: '甲\r\n🙂乙', formatting: { version: 1, boundaries: [], inline: [], blocks: [] },
      sections: [], retired_section_keys: [], markers: [marker(0, 4)] }
    const current = EditorState.create({ schema, doc: schema.node('doc', null, [
      schema.node('paragraph', { prefix: '' }, schema.text('甲X')),
      schema.node('paragraph', { prefix: '\r\n' }, schema.text('🙂乙')),
    ]) })
    const mapped = mapMarkerTransaction(current.tr.delete(1, 7), [marker(0, 5)], saved)
    expect(mapped?.[0]).toMatchObject({ status: 'stale', excerpt: '甲\r\n🙂', last_start: 0,
      draft_excerpt: '甲X\r\n🙂', draft_excerpt_truncated: false, draft_last_start: 0 })
  })
  it('长标记的草稿片段按码点截断且不拆 CRLF，与未截断基线独立', () => {
    const saved: RichDocument = { content: '🙂'.repeat(4998) + '\r\n',
      formatting: { version: 1, boundaries: [], inline: [], blocks: [] }, sections: [], retired_section_keys: [],
      markers: [marker(0, 5000)] }
    const draft = '🙂'.repeat(4999) + '\r\n'
    const stale = snapshotInvalidatedMarker(marker(0, 5001), Array.from(draft), saved)
    expect(stale.excerpt).toBe(saved.content)
    expect(stale.excerpt_truncated).toBe(false)
    expect(stale.draft_excerpt).toBe('🙂'.repeat(4999))
    expect(stale.draft_excerpt_truncated).toBe(true)
    expect(stale.draft_last_start).toBe(0)
  })

})
