import type { Editor } from '@tiptap/core'
import { closeHistory } from '@tiptap/pm/history'
import type { Node as PMNode } from '@tiptap/pm/model'
import { App, Button, Form, List, Modal, Select } from 'antd'
import { useMemo, useState } from 'react'
import type { MarkerColor, RichMarker } from '../../api/richDocument'
import { selectEditorCodePoints, selectSectionCodePoints } from './richTextExtensions'
import { resolveRichSelection } from './richEditorPositions'
import { markersWithoutTextNodes } from './richMarkerVisibility'
import { projectSectionDocument } from './richSectionProjection'

const COLORS: { value: MarkerColor; label: string }[] = [
  { value: 'yellow', label: '黄色' }, { value: 'green', label: '绿色' },
  { value: 'blue', label: '蓝色' }, { value: 'purple', label: '紫色' }, { value: 'red', label: '红色' },
]
interface SelectionSnapshot { doc: PMNode; start: number; end: number; sectionKey: string | null }

/** 以目标身份和明确选区创建或调整标记；不从相同字面推断目标。 */
export default function RichMarkerActions({ editor, readOnly }: { editor: Editor | null; readOnly: boolean }) {
  const { message } = App.useApp()
  const [snapshot, setSnapshot] = useState<SelectionSnapshot | null>(null)
  const [showInvisible, setShowInvisible] = useState(false)
  const [target, setTarget] = useState('')
  const [color, setColor] = useState<MarkerColor>('yellow')
  const open = () => {
    if (!editor || readOnly) return
    const { from, to } = editor.state.selection
    const selection = resolveRichSelection(editor.state.doc, from, to)
    let crossesSection = false
    if (selection?.start.scope === 'prose' && from !== to) editor.state.doc.nodesBetween(from, to, (node) => {
      if (node.type.name === 'authorSection') { crossesSection = true; return false }
    })
    if (!selection || crossesSection || selection.start.offset === selection.end.offset) {
      void message.info('请先在同一正文或区块中选择要标记的文字。'); return
    }
    const sectionKey = selection.start.scope === 'section' ? selection.start.sectionKey : null
    setSnapshot({ doc: editor.state.doc, start: selection.start.offset, end: selection.end.offset, sectionKey }); setTarget(''); setColor('yellow')
  }
  const editorDocument = editor?.state.doc
  const invisible = useMemo(() => editorDocument ? markersWithoutTextNodes(editorDocument) : [], [editorDocument])
  const markers = (snapshot?.doc.attrs.markers ?? []) as RichMarker[]
  const document = snapshot ? projectSectionDocument(snapshot.doc.toJSON()) : null
  const text = snapshot ? Array.from(snapshot.sectionKey === null ? document!.content
    : document!.sections.find((item) => item.key === snapshot.sectionKey)?.content ?? '') : []
  const options = markers.filter((item) => item.status === 'active' && item.section_key === snapshot?.sectionKey)
    .sort((a, b) => a.start! - b.start! || a.end! - b.end! || a.key.localeCompare(b.key))
    .map((item) => {
      const chars = text.slice(item.start!, item.end!)
      const excerpt = chars.slice(0, 30).join('') + (chars.length > 30 ? '…' : '')
      return { value: item.key, label: `${COLORS.find((value) => value.value === item.color)?.label} · ${excerpt}（${item.start}–${item.end}）` }
    })
  const apply = () => {
    if (!editor || !snapshot || readOnly || editor.state.doc !== snapshot.doc) {
      void message.warning('正文或权限已变化，请重新选择文字。'); setSnapshot(null); return
    }
    let next: RichMarker[]
    if (target) {
      const existing = markers.find((item) => item.key === target && item.status === 'active' && item.section_key === snapshot.sectionKey)
      if (!existing) { void message.warning('所选标记已变化，请重新选择。'); setSnapshot(null); return }
      next = markers.map((item) => item.key === target ? { ...item, start: snapshot.start, end: snapshot.end, color } : item)
    } else {
      if (!globalThis.crypto?.randomUUID) { void message.error('当前环境无法生成安全的标记身份，请更换安全浏览器。'); return }
      next = [...markers, { key: globalThis.crypto.randomUUID(), section_key: snapshot.sectionKey, status: 'active',
        start: snapshot.start, end: snapshot.end, color, excerpt: '', excerpt_truncated: false, last_start: null,
        draft_excerpt: null, draft_excerpt_truncated: false, draft_last_start: null }]
    }
    editor.view.dispatch(closeHistory(editor.state.tr).setDocAttribute('markers', next).setMeta('rich-marker-intent', true))
    setSnapshot(null)
    void message.success('标记已更新，请保存场景。')
    editor.commands.focus()
  }
  return <>
    <Button size="small" disabled={readOnly || !editor} onMouseDown={(event) => event.preventDefault()} onClick={open}>标记选区</Button>
    {invisible.length > 0 && <Button size="small" onClick={() => setShowInvisible(true)}>仅换行标记（{invisible.length}）</Button>}
    <Modal title="仅覆盖换行的标记" open={showInvisible} onCancel={() => setShowInvisible(false)} footer={null} destroyOnHidden>
      <p>这些标记只覆盖段落间换行，无法在文字上显示下划线；可定位后调整，或在全书标记中删除。</p>
      <List size="small" dataSource={invisible} pagination={invisible.length > 20 ? { pageSize: 20 } : false} renderItem={(item) => <List.Item
        key={item.key} actions={[<Button key="jump" size="small" aria-label={`定位${COLORS.find((value) => value.value === item.color)?.label}换行标记 ${item.start}–${item.end}`}
          onClick={() => {
            setShowInvisible(false)
            const selected = editor && (item.section_key === null ? selectEditorCodePoints(editor, item.start!, item.end!)
              : selectSectionCodePoints(editor, item.section_key, item.start!, item.end!))
            if (!selected) void message.warning('换行位置已变化，请重新加载后定位。')
          }}>定位</Button>]}>
        {item.section_key === null ? '正文' : '区块'} · {COLORS.find((value) => value.value === item.color)?.label}换行标记 · {item.start}–{item.end}
      </List.Item>} />
    </Modal>
    <Modal title={snapshot?.sectionKey === null ? '标记正文选区' : '标记区块选区'} open={snapshot !== null} onCancel={() => setSnapshot(null)} onOk={apply} okText="确定" cancelText="取消" destroyOnHidden>
      <p>所选文字共 {snapshot ? snapshot.end - snapshot.start : 0} 个字符。可新建标记，或明确选择一个现有标记调整其范围与颜色。</p>
      <Form layout="vertical">
        <Form.Item label="标记目标">
          <Select aria-label="标记目标" value={target} onChange={(value: string) => {
            setTarget(value)
            const existing = markers.find((item) => item.key === value)
            if (existing) setColor(existing.color)
          }} options={[{ value: '', label: '新建标记' }, ...options]} showSearch optionFilterProp="label" />
        </Form.Item>
        <Form.Item label="标记颜色">
          <Select aria-label="标记颜色" value={color} onChange={setColor} options={COLORS} virtual={false} />
        </Form.Item>
      </Form>
    </Modal>
  </>
}
