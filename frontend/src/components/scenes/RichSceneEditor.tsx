import { Editor } from '@tiptap/core'
import { isHistoryTransaction } from '@tiptap/pm/history'
import { EditorState } from '@tiptap/pm/state'
import { StarterKit } from '@tiptap/starter-kit'
import { App, Button, Space, Typography, theme } from 'antd'
import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react'
import type { RichDocument } from '../../api/richDocument'
import { richMarkerPalette } from '../../theme/palette'
import { useResolvedTheme } from '../../theme/themeStore'
import { codePointToUtf16 } from '../../utils/textOffset'
import type { TextSelection } from '../chapter/RewritePanel'
import { editorCodePointSelection, FocusableListItem, PreserveRichPrefixes, RichEnter, RichExactSelection, RichPlainPaste, RichPrefixes, selectEditorCodePoints, selectSectionCodePoints } from './richTextExtensions'
import { findRichCursor, resolveRichCursor, resolveRichSelection, richCursorCandidates } from './richEditorPositions'
import { RichMarkerDocument, RichMarkerState } from './richMarkerState'
import RichMarkerActions from './RichMarkerActions'
import { AuthorSection } from './richSectionNode'
import { RichSectionGuard } from './richSectionGuard'
import { RichSectionHistory } from './richSectionHistory'
import { RichSectionDrag } from './richSectionDrag'
import { editorFromSectionDocument, projectSectionDocument, sameSectionDocument } from './richSectionProjection'
import { createSectionPlanTransaction, mappedSectionIdentities } from './richSectionTransaction'
import type { RichEditEvidence } from './sceneDraftController'
import './RichSceneEditor.css'

export interface RichSceneEditorHandle {
  selection: () => { start: number; end: number } | null
  select: (start: number, end: number) => boolean
  sectionSelection: () => RichSectionSelection | null
  selectSection: (key: string, start: number, end: number) => boolean
}
export interface RichSectionSelection { sectionKey: string; start: number; end: number; selectedText: string }
interface Props {
  document: RichDocument
  savedDocument?: RichDocument
  readOnly: boolean
  onDocument: (document: RichDocument, evidence: RichEditEvidence) => void
  onCompositionChange?: (composing: boolean) => void
  onSelection: (selection: TextSelection) => void
  onSectionConvert?: (key: string) => void
  onSectionRewrite?: (selection: RichSectionSelection) => void
}

/** 仅挂载已通过投影门禁的文档。所有用户输入都先经结构守卫。 */
const RichSceneEditor = forwardRef<RichSceneEditorHandle, Props>(function RichSceneEditor({ document, savedDocument, readOnly, onDocument, onCompositionChange, onSelection, onSectionConvert, onSectionRewrite }, ref) {
  const { token } = theme.useToken()
  const markerColors = richMarkerPalette(useResolvedTheme())
  const { message, modal } = App.useApp()
  const mount = useRef<HTMLDivElement>(null)
  const editorRef = useRef<Editor | null>(null)
  const initial = useRef(document)
  const callbacks = useRef({ onDocument, onSelection, savedDocument, onCompositionChange, onSectionConvert, onSectionRewrite })
  callbacks.current = { onDocument, onSelection, savedDocument, onCompositionChange, onSectionConvert, onSectionRewrite }
  const composingRef = useRef(false)
  const sectionComposingRef = useRef(new Set<string>())
  const [composing, setComposing] = useState(false)
  const messageRef = useRef(message); messageRef.current = message
  const modalRef = useRef(modal); modalRef.current = modal
  const [generation, setGeneration] = useState(0)
  const sectionSelection = (editor: Editor): RichSectionSelection | null => {
    const { from, to } = editor.state.selection
    const selected = resolveRichSelection(editor.state.doc, from, to)
    if (!selected || selected.start.scope !== 'section' || selected.end.scope !== 'section'
      || selected.start.sectionKey !== selected.end.sectionKey || selected.start.offset >= selected.end.offset) return null
    const doc = projectSectionDocument(editor.getJSON())
    const key = selected.start.sectionKey
    const section = doc.sections.find((item) => item.key === key)
    if (!section) return null
    return { sectionKey: section.key, start: selected.start.offset, end: selected.end.offset,
      selectedText: Array.from(section.content).slice(selected.start.offset, selected.end.offset).join('') }
  }
  const runSectionPlan = (editor: Editor, plan: Parameters<typeof createSectionPlanTransaction>[1]): boolean => {
    if (!editor.isEditable || composingRef.current || sectionComposingRef.current.size) return false
    try {
      const before = editor.state.doc
      const transaction = createSectionPlanTransaction(editor.state, plan, callbacks.current.savedDocument ?? initial.current)
      if (sameSectionDocument(projectSectionDocument(before.toJSON()), projectSectionDocument(transaction.doc.toJSON()))) return true
      editor.view.dispatch(transaction)
      return editor.state.doc !== before
    } catch (error) {
      void messageRef.current.warning(error instanceof Error ? error.message : '区块操作无法安全完成，原稿未改变。')
      return false
    }
  }
  const createSection = () => {
    const editor = editorRef.current
    if (!editor || !editor.state.selection.empty || !globalThis.crypto?.randomUUID) {
      void messageRef.current.info('请先在正式正文中放置光标，并使用支持安全身份的浏览器。'); return
    }
    const cursor = resolveRichCursor(editor.state.doc, editor.state.selection.from)
    if (!cursor || cursor.scope !== 'prose' || findRichCursor(editor.state.doc, cursor) === null) {
      void messageRef.current.info('该位置不能可靠区分区块边界，请重新放置正文光标。'); return
    }
    const key = globalThis.crypto.randomUUID()
    if (runSectionPlan(editor, { action: 'create', key, cursor })) selectSectionCodePoints(editor, key, 0, 0)
  }
  useImperativeHandle(ref, () => ({
    selection: () => editorRef.current ? editorCodePointSelection(editorRef.current) : null,
    select: (start, end) => editorRef.current ? selectEditorCodePoints(editorRef.current, start, end) : false,
    sectionSelection: () => editorRef.current ? sectionSelection(editorRef.current) : null,
    selectSection: (key, start, end) => editorRef.current ? selectSectionCodePoints(editorRef.current, key, start, end) : false,
  }), [])

  useEffect(() => {
    if (!mount.current) return
    let live = true
    const reportComposition = () => {
      const active = composingRef.current || sectionComposingRef.current.size > 0
      if (live) setComposing(active)
      callbacks.current.onCompositionChange?.(active)
    }
    const instance = new Editor({
      element: mount.current,
      enableInputRules: false, enablePasteRules: false, enableContentCheck: true, injectCSS: false,
      extensions: [StarterKit.configure({ document: false, trailingNode: false, code: false, codeBlock: false,
        horizontalRule: false, hardBreak: false, link: false, strike: false, underline: false, listItem: false, listKeymap: false }),
      RichMarkerDocument, FocusableListItem, RichPrefixes, PreserveRichPrefixes,
      RichSectionGuard.configure({ getSavedDocument: () => callbacks.current.savedDocument ?? initial.current,
        isComposing: () => composingRef.current }),
      RichSectionHistory.configure({ getSavedDocument: () => callbacks.current.savedDocument ?? initial.current }),
      RichSectionDrag.configure({ getSavedDocument: () => callbacks.current.savedDocument ?? initial.current }),
      RichMarkerState.configure({ sectionMode: true, getSavedDocument: () => callbacks.current.savedDocument ?? initial.current,
        onCompositionChange: (value) => { composingRef.current = value; reportComposition() },
        onCompositionCommit: (keys, sectionKeys) => {
          if (!live || !editorRef.current) return
          callbacks.current.onDocument(projectSectionDocument(editorRef.current.getJSON()), {
            mappedMarkerKeys: keys, explicitMarkerIntent: false, mappedSectionKeys: sectionKeys,
          })
        } }), AuthorSection, RichExactSelection, RichPlainPaste, RichEnter],
      content: editorFromSectionDocument(initial.current),
      editorProps: { attributes: { 'aria-label': '当前场景正文', role: 'textbox', 'aria-multiline': 'true', spellcheck: 'false',
        'data-section-ai-enabled': String(!!callbacks.current.onSectionRewrite) } },
      onUpdate({ editor, transaction, appendedTransactions }) {
        if (composingRef.current) return
        const history = isHistoryTransaction(transaction)
        const mapped = new Set<string>()
        const sections = new Set<string>()
        const plan = transaction.getMeta('rich-section-plan')
        const sectionIntent = transaction.getMeta('rich-section-intent')
        for (const item of [transaction, ...appendedTransactions]) {
          const keys: unknown = item.getMeta('rich-mapped-marker-keys')
          if (Array.isArray(keys)) for (const key of keys) if (typeof key === 'string') mapped.add(key)
          const sectionKeys = mappedSectionIdentities(item)
          if (sectionKeys) for (const key of sectionKeys) sections.add(key)
        }
        if (history || plan) {
          for (const marker of [...(transaction.before.attrs.markers ?? []), ...(editor.state.doc.attrs.markers ?? [])]) mapped.add(marker.key)
          const original = projectSectionDocument(transaction.before.toJSON())
          const current = projectSectionDocument(editor.getJSON())
          const finalKeys = new Set(current.sections.map((item) => item.key))
          for (const item of original.sections) if (finalKeys.has(item.key)) sections.add(item.key)
        }
        const beforeKeys = new Set<string>((transaction.before.attrs.markers ?? []).map((marker: { key: string }) => marker.key))
        const restored = history ? (editor.state.doc.attrs.markers ?? []).filter((marker: { key: string }) => !beforeKeys.has(marker.key))
          .map((marker: { key: string }) => marker.key) as string[] : []
        const originalSections = history ? new Set(projectSectionDocument(transaction.before.toJSON()).sections.map((item) => item.key)) : new Set<string>()
        const current = projectSectionDocument(editor.getJSON())
        const restoredSections = history ? current.sections.filter((item) => !originalSections.has(item.key)).map((item) => item.key) : []
        callbacks.current.onDocument(current, {
          mappedMarkerKeys: [...mapped], explicitMarkerIntent: history || !!transaction.getMeta('rich-marker-intent'),
          restoredMarkerKeys: restored, mappedSectionKeys: [...sections], explicitSectionIntent: history || !!plan || !!sectionIntent,
          restoredSectionKeys: restoredSections,
        })
      },
      onSelectionUpdate({ editor }) {
        const point = editorCodePointSelection(editor)
        if (!point) { callbacks.current.onSelection({ start: 0, end: 0 }); return }
        const text = projectSectionDocument(editor.getJSON()).content
        callbacks.current.onSelection({ start: codePointToUtf16(text, point.start), end: codePointToUtf16(text, point.end) })
      },
      onTransaction() { if (live) setGeneration((value) => value + 1) },
    })
    editorRef.current = instance
    const rejected = () => { void messageRef.current.warning('操作无法可靠保留正文结构或标记，正文未修改；请缩小范围或先处理标记。') }
    const rejectedPaste = () => { void messageRef.current.warning('无法安全粘贴该内容，原选区未修改；请粘贴纯文本。') }
    const rejectedComposition = () => { void messageRef.current.error('输入法内容未能安全同步，保存已暂停；请复制当前正文后重新加载。') }
    const sectionComposition = (event: Event) => {
      const detail: unknown = (event as CustomEvent<unknown>).detail
      if (!detail || typeof detail !== 'object' || !('key' in detail) || !('active' in detail)
        || typeof detail.key !== 'string' || typeof detail.active !== 'boolean') return
      if (detail.active) sectionComposingRef.current.add(detail.key); else sectionComposingRef.current.delete(detail.key)
      reportComposition()
    }
    const sectionAction = (event: Event) => {
      const detail: unknown = (event as CustomEvent<unknown>).detail
      if (!detail || typeof detail !== 'object' || !('key' in detail) || !('action' in detail)
        || typeof detail.key !== 'string' || typeof detail.action !== 'string' || !instance.isEditable
        || composingRef.current || sectionComposingRef.current.size) return
      const key = detail.key
      const doc = projectSectionDocument(instance.getJSON())
      const section = doc.sections.find((item) => item.key === key)
      if (!section) { void messageRef.current.warning('区块已变化，请重新操作。'); return }
      if (detail.action === 'rewrite-selection') {
        const selected = sectionSelection(instance)
        if (!selected || selected.sectionKey !== key) { void messageRef.current.info('请先在此区块中选择要编辑的文字。'); return }
        callbacks.current.onSectionRewrite?.(selected)
      } else if (detail.action === 'convert') {
        callbacks.current.onSectionConvert?.(key)
      } else if (detail.action === 'delete') {
        const expected = instance.state.doc
        modalRef.current.confirm({ title: `删除区块“${section.title}”？`,
          content: '区块文字将从当前稿删除；关联标记保留为失效记录，历史版本仍可核对。', okText: '确认删除', cancelText: '取消', okButtonProps: { danger: true },
          onOk: () => {
            if (instance.isDestroyed || !instance.isEditable || instance.state.doc !== expected) {
              void messageRef.current.warning('区块或权限已变化，请重新确认。'); return
            }
            runSectionPlan(instance, { action: 'delete', key })
          } })
      } else if (detail.action === 'move-to-cursor') {
        const target = instance.state.selection.empty ? resolveRichCursor(instance.state.doc, instance.state.selection.from) : null
        if (!target || target.scope !== 'prose' || findRichCursor(instance.state.doc, target) === null) {
          void messageRef.current.info('请先在要移动到的正文位置放置光标，再使用此操作。'); return
        }
        runSectionPlan(instance, { action: 'move', key, cursor: target })
      } else if (detail.action === 'move-up' || detail.action === 'move-down') {
        const sorted = [...doc.sections].sort((a, b) => a.anchor - b.anchor || a.order - b.order)
        const index = sorted.findIndex((item) => item.key === key); const up = detail.action === 'move-up'
        const neighbor = sorted[index + (up ? -1 : 1)]
        const offset = neighbor?.anchor ?? (up ? 0 : Array.from(doc.content).length)
        const candidates = richCursorCandidates(instance.state.doc, { scope: 'prose', offset })
          .filter((item) => item.cursor.scope === 'prose' && findRichCursor(instance.state.doc, item.cursor) !== null)
        const target = neighbor ? candidates.find((item) => item.cursor.scope === 'prose'
          && (up ? item.cursor.rightSection === neighbor.key : item.cursor.leftSection === neighbor.key))
          : candidates[up ? 0 : candidates.length - 1]
        if (!target) { void messageRef.current.warning('目标边界无法可靠定位，区块未移动。'); return }
        runSectionPlan(instance, { action: 'move', key, cursor: target.cursor })
      }
    }
    instance.view.dom.addEventListener('rich-mapping-rejected', rejected)
    instance.view.dom.addEventListener('rich-paste-rejected', rejectedPaste)
    instance.view.dom.addEventListener('rich-composition-rejected', rejectedComposition)
    instance.view.dom.addEventListener('rich-section-composition', sectionComposition)
    instance.view.dom.addEventListener('rich-section-action', sectionAction)
    queueMicrotask(() => { if (live) setGeneration((value) => value + 1) })
    return () => { live = false; editorRef.current = null; instance.view.dom.removeEventListener('rich-mapping-rejected', rejected); instance.view.dom.removeEventListener('rich-paste-rejected', rejectedPaste); instance.view.dom.removeEventListener('rich-composition-rejected', rejectedComposition); instance.view.dom.removeEventListener('rich-section-composition', sectionComposition); instance.view.dom.removeEventListener('rich-section-action', sectionAction); instance.destroy() }
  }, [])

  useEffect(() => {
    const editor = editorRef.current
    if (!editor) return
    editor.setEditable(!readOnly, false)
  }, [readOnly, generation])

  useEffect(() => {
    const dom = editorRef.current?.view.dom
    const enabled = String(!!onSectionRewrite)
    if (dom && dom.getAttribute('data-section-ai-enabled') !== enabled) dom.setAttribute('data-section-ai-enabled', enabled)
  }, [onSectionRewrite, generation])

  useEffect(() => {
    const editor = editorRef.current
    if (!editor || composingRef.current || sectionComposingRef.current.size) return
    const current = projectSectionDocument(editor.getJSON())
    if (sameSectionDocument(current, document)) return
    // 只有外部基线变化才重建状态；本地事务和保存同值不清除撤销历史。
    const node = editor.schema.nodeFromJSON(editorFromSectionDocument(document))
    editor.view.updateState(EditorState.create({ schema: editor.schema, doc: node, plugins: editor.state.plugins }))
  }, [document, generation])

  const editor = editorRef.current
  const button = (label: string, active: boolean, action: () => void) => <Button size="small" aria-pressed={active}
    disabled={readOnly || composing || !editor} onMouseDown={(event) => event.preventDefault()} onClick={action}>{label}</Button>
  return <div className="rich-scene-editor" style={{ '--rich-border': token.colorBorder, '--rich-bg': token.colorBgContainer, '--rich-text': token.colorText,
    '--rich-marker-yellow': markerColors.yellow, '--rich-marker-green': markerColors.green, '--rich-marker-blue': markerColors.blue,
    '--rich-marker-purple': markerColors.purple, '--rich-marker-red': markerColors.red } as React.CSSProperties}>
    <Space wrap size={[4, 4]} role="toolbar" aria-label="正文排版工具" style={{ marginBottom: 8 }}>
      {button('粗体', !!editor?.isActive('bold'), () => { editor?.chain().focus().toggleBold().run() })}
      {button('斜体', !!editor?.isActive('italic'), () => { editor?.chain().focus().toggleItalic().run() })}
      {button('标题', !!editor?.isActive('heading', { level: 2 }), () => { editor?.chain().focus().toggleHeading({ level: 2 }).run() })}
      {button('无序列表', !!editor?.isActive('bulletList'), () => { editor?.chain().focus().toggleBulletList().run() })}
      {button('有序列表', !!editor?.isActive('orderedList'), () => { editor?.chain().focus().toggleOrderedList().run() })}
      {button('引用', !!editor?.isActive('blockquote'), () => { editor?.chain().focus().toggleBlockquote().run() })}
      <Button size="small" disabled={readOnly || composing || !editor}
        onMouseDown={(event) => event.preventDefault()} onClick={createSection}>插入区块</Button>
      <RichMarkerActions editor={editor} readOnly={readOnly || composing} />
      <Button size="small" disabled={readOnly || composing || !editor?.can().undo()} onMouseDown={(event) => event.preventDefault()} onClick={() => editor?.chain().focus().undo().run()}>撤销</Button>
      <Button size="small" disabled={readOnly || composing || !editor?.can().redo()} onMouseDown={(event) => event.preventDefault()} onClick={() => editor?.chain().focus().redo().run()}>重做</Button>
    </Space>
    <Typography.Paragraph type="secondary" style={{ marginBottom: 8, fontSize: 12 }}>
      当前场景最多 1000 个区块；单块正文最多 50 万字符，合计最多 200 万字符（按 Unicode 码点计）。正文及每个区块的各类格式最多 2 万项，标记最多 2 万条，元数据最多 8 MiB。超限不会截断草稿。
    </Typography.Paragraph>
    {composing && <span role="status">正在输入文字，组合完成后可保存。</span>}
    <div ref={mount} />
  </div>
})
export default RichSceneEditor
