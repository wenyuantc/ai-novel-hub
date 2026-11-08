// 隔离真实 Chromium + Tiptap；验证同码点区块前/中/后位置，不代表产品已接入。
async (page) => {
  await page.goto('http://127.0.0.1:5179/')
  const result = await page.evaluate(async () => {
    const compiled = await (await fetch('/src/components/scenes/RichSceneEditor.tsx')).text()
    const version = compiled.match(/@tiptap_core\.js\?v=([a-z\d]+)/)?.[1]
    if (!version) throw new Error('Tiptap 依赖版本不存在')
    const { Editor } = await import(`/node_modules/.vite/deps/@tiptap_core.js?v=${version}`)
    const { default: StarterKit } = await import(`/node_modules/.vite/deps/@tiptap_starter-kit.js?v=${version}`)
    const { RichMarkerDocument } = await import('/src/components/scenes/richMarkerState.ts')
    const { AuthorSection } = await import('/src/components/scenes/richSectionNode.ts')
    const { FocusableListItem, RichPrefixes } = await import('/src/components/scenes/richTextExtensions.ts')
    const { editorFromSectionDocument, projectSectionDocument, sameSectionDocument } = await import('/src/components/scenes/richSectionProjection.ts')
    const { richCursorCandidates, resolveRichCursor, findRichCursor } = await import('/src/components/scenes/richEditorPositions.ts')
    const format = () => ({ version: 1, boundaries: [], inline: [], blocks: [] })
    const documentData = { content: '甲🙂\r\n乙', formatting: format(), sections: [
      { key: 'a', anchor: 1, order: 3, title: '甲区块', content: '备🙂\r\n末', formatting: format(), include_in_ai: false },
      { key: 'b', anchor: 1, order: 9, title: '乙区块', content: '内容', formatting: format(), include_in_ai: false },
    ], retired_section_keys: [], markers: [] }
    const mount = document.createElement('div'); document.body.appendChild(mount)
    const editor = new Editor({ element: mount, enableInputRules: false, enablePasteRules: false, injectCSS: false,
      extensions: [StarterKit.configure({ document: false, trailingNode: false, code: false, codeBlock: false, horizontalRule: false,
        hardBreak: false, link: false, strike: false, underline: false, listItem: false, listKeymap: false }),
      RichMarkerDocument, FocusableListItem, RichPrefixes, AuthorSection], content: editorFromSectionDocument(documentData) })
    const result = []
    for (const side of [0, 1, 2]) {
      editor.commands.setContent(editorFromSectionDocument(documentData), { errorOnInvalidContent: true })
      const candidates = richCursorCandidates(editor.state.doc, { scope: 'prose', offset: 1 })
      if (candidates.length !== 3) throw new Error(`同锚位置数量错误 ${JSON.stringify(candidates)}`)
      const point = candidates[side]
      if (JSON.stringify(resolveRichCursor(editor.state.doc, point.pm)) !== JSON.stringify(point.cursor) || findRichCursor(editor.state.doc, point.cursor) !== point.pm) throw new Error('结构光标不可双向映射')
      editor.commands.setTextSelection(point.pm)
      editor.commands.insertContent('X')
      const changed = projectSectionDocument(editor.getJSON())
      const expected = [[2, 2], [1, 2], [1, 1]][side]
      if (changed.content !== '甲X🙂\r\n乙' || JSON.stringify(changed.sections.map((section) => section.anchor)) !== JSON.stringify(expected)) throw new Error(`区块边向错误 ${JSON.stringify(changed)}`)
      result.push({ cursor: point.cursor, anchors: changed.sections.map((section) => section.anchor) })
    }
    editor.commands.setContent(editorFromSectionDocument(documentData), { errorOnInvalidContent: true })
    if (!sameSectionDocument(projectSectionDocument(editor.getJSON()), documentData)) throw new Error('真实节点往返失败')
    if (richCursorCandidates(editor.state.doc, { scope: 'prose', offset: 3 }).length) throw new Error('CRLF 中间被暴露为光标')
    editor.destroy(); mount.remove()
    return result
  })
  return { result: 'PASS', cases: result }
}
