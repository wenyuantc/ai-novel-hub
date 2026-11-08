import { closeHistory, history, redo, undo } from '@tiptap/pm/history'
import { Schema } from '@tiptap/pm/model'
import { EditorState, type Transaction } from '@tiptap/pm/state'
import { describe, expect, it } from 'vitest'

const schema = new Schema({ nodes: {
  doc: { content: 'paragraph+', attrs: { markers: { default: [] } } },
  paragraph: { content: 'text*', group: 'block' },
  text: { group: 'inline' },
} })

describe('输入法组合历史轮次', () => {
  it('跨超过默认 500ms 的候选延迟，正文与标记仍作为一次操作撤销和重做', () => {
    const active = [{ key: 'one', status: 'active', start: 0, end: 2 }]
    const stale = [{ key: 'one', status: 'stale', start: null, end: null, excerpt: '甲乙' }]
    let state = EditorState.create({ schema, plugins: [history()], doc: schema.node('doc', { markers: active }, [
      schema.node('paragraph', null, schema.text('甲乙尾')),
    ]) })
    const apply = (tr: Transaction) => { state = state.apply(tr) }
    // 对应组合开始时精确删除选区；元数据和后续候选共用同一事务轮次。
    apply(closeHistory(state.tr.delete(1, 3)).setMeta('composition', -2).setTime(1000))
    apply(state.tr.insertText('拼音', 1).setMeta('composition', -2).setTime(4000))
    apply(state.tr.setDocAttribute('markers', stale).setMeta('composition', -2).setTime(7000))
    expect(state.doc.textContent).toBe('拼音尾')
    expect(undo(state, apply)).toBe(true)
    expect(state.doc.textContent).toBe('甲乙尾')
    expect(state.doc.attrs.markers).toEqual(active)
    expect(redo(state, apply)).toBe(true)
    expect(state.doc.textContent).toBe('拼音尾')
    expect(state.doc.attrs.markers).toEqual(stale)
  })
})
