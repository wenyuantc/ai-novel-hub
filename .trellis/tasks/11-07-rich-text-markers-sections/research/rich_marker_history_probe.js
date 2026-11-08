// 独立 PM 模型：验证元数据与正文进入同一 undo/redo 事务；尚未开放产品标记编辑。
async (page) => {
  await page.goto('http://127.0.0.1:5179/')
  return await page.evaluate(async () => {
    const compiled = await (await fetch('/src/components/scenes/RichSceneEditor.tsx')).text()
    const version = compiled.match(/@tiptap_core\.js\?v=([a-z\d]+)/)?.[1]
    const { Editor } = await import(`/node_modules/.vite/deps/@tiptap_core.js?v=${version}`)
    const { default: StarterKit } = await import(`/node_modules/.vite/deps/@tiptap_starter-kit.js?v=${version}`)
    const { RichPrefixes, PreserveRichPrefixes, RichStructureGuard, RichPlainPaste, RichEnter, FocusableListItem } = await import('/src/components/scenes/richTextExtensions.ts')
    const { RichMarkerDocument, RichMarkerState } = await import('/src/components/scenes/richMarkerState.ts')
    const historyCode = await (await fetch('/src/components/scenes/richMarkerState.ts')).text()
    const historyVersion = historyCode.match(/@tiptap_pm_history\.js\?v=([a-z\d]+)/)?.[1]
    const { closeHistory } = await import(`/node_modules/.vite/deps/@tiptap_pm_history.js?v=${historyVersion}`)
    const { editorFromDocument, projectEditorDocument } = await import('/src/components/scenes/richTextProjection.ts')
    const original = '甲\r\n🙂乙\n末'
    const doc = { content: original, formatting: { version: 1, boundaries: [], inline: [], blocks: [] }, sections: [], retired_section_keys: [], markers: [] }
    const marker = { key: '12345678-1234-4123-8123-123456789def', section_key: null, status: 'active', start: 0, end: 4, color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null }
    const node = editorFromDocument(doc); node.attrs = { markers: [marker] }
    const mount = document.createElement('div'); document.body.appendChild(mount)
    const editor = new Editor({ element: mount, enableInputRules: false, enablePasteRules: false, injectCSS: false,
      extensions: [StarterKit.configure({ document: false, trailingNode: false, code: false, codeBlock: false, horizontalRule: false, hardBreak: false, link: false, strike: false, underline: false, listItem: false, listKeymap: false }), RichMarkerDocument, FocusableListItem, RichPrefixes, PreserveRichPrefixes, RichStructureGuard, RichMarkerState, RichPlainPaste, RichEnter], content: node })
    const state = () => ({ text: projectEditorDocument(editor.getJSON()).content, mark: editor.state.doc.attrs.markers[0] })
    const expect = (condition, message) => { if (!condition) throw new Error(`${message}: ${JSON.stringify(state())}`) }
    editor.view.dispatch(closeHistory(editor.state.tr))
    editor.commands.insertContentAt(4, 'X')
    expect(state().text === '甲\r\nX🙂乙\n末' && state().mark.end === 5, '内部插入映射')
    editor.commands.undo()
    expect(state().text === original && state().mark.status === 'active' && state().mark.end === 4, '撤销内部插入')
    editor.commands.redo()
    expect(state().text === '甲\r\nX🙂乙\n末' && state().mark.end === 5, '重做内部插入')
    editor.commands.undo(); editor.view.dispatch(closeHistory(editor.state.tr))
    editor.commands.deleteRange({ from: 1, to: 6 })
    expect(state().text === '乙\n末' && state().mark.status === 'stale' && state().mark.excerpt === '甲\r\n🙂', '完全删除')
    editor.commands.undo()
    expect(state().text === original && state().mark.status === 'active' && state().mark.end === 4, '撤销失效')
    editor.commands.redo()
    expect(state().text === '乙\n末' && state().mark.status === 'stale', '重做失效')
    editor.destroy(); mount.remove()
    return { result: 'PASS', operations: ['跨 CRLF 内部插入', '完全删除转失效', '撤销/重做同步标记'] }
  })
}
