// 零宽格式边界的合并不可修改文字或误定位已有标记。
async (page) => {
  await page.goto('http://127.0.0.1:5179/')
  await page.evaluate(async () => {
    const compiled = await (await fetch('/src/components/scenes/RichSceneEditor.tsx')).text()
    const version = compiled.match(/@tiptap_core\.js\?v=([a-z\d]+)/)?.[1]
    const { Editor } = await import(`/node_modules/.vite/deps/@tiptap_core.js?v=${version}`)
    const { default: StarterKit } = await import(`/node_modules/.vite/deps/@tiptap_starter-kit.js?v=${version}`)
    const { RichMarkerDocument, RichMarkerState } = await import('/src/components/scenes/richMarkerState.ts')
    const { AuthorSection } = await import('/src/components/scenes/richSectionNode.ts')
    const { RichSectionGuard } = await import('/src/components/scenes/richSectionGuard.ts')
    const { RichSectionHistory } = await import('/src/components/scenes/richSectionHistory.ts')
    const { FocusableListItem, RichPrefixes, PreserveRichPrefixes, RichExactSelection, RichPlainPaste, RichEnter } = await import('/src/components/scenes/richTextExtensions.ts')
    const { editorFromSectionDocument, projectSectionDocument } = await import('/src/components/scenes/richSectionProjection.ts')
    const documentData = { content: '甲乙', formatting: { version: 1, boundaries: [1], inline: [], blocks: [] }, sections: [], retired_section_keys: [],
      markers: [{ key: 'm', section_key: null, status: 'active', start: 0, end: 2, color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null }] }
    const mount = document.createElement('div'); document.body.appendChild(mount)
    const editor = new Editor({ element: mount, enableInputRules: false, enablePasteRules: false, injectCSS: false,
      extensions: [StarterKit.configure({ document: false, trailingNode: false, code: false, codeBlock: false,
        horizontalRule: false, hardBreak: false, link: false, strike: false, underline: false, listItem: false, listKeymap: false }),
      RichMarkerDocument, FocusableListItem, RichPrefixes, PreserveRichPrefixes,
      RichSectionGuard.configure({ getSavedDocument: () => documentData }), RichSectionHistory.configure({ getSavedDocument: () => documentData }),
      RichMarkerState.configure({ sectionMode: true, getSavedDocument: () => documentData }), AuthorSection,
      RichExactSelection, RichPlainPaste, RichEnter], content: editorFromSectionDocument(documentData) })
    window.zeroBoundaryProbe = { editor, current: () => projectSectionDocument(editor.getJSON()) }
    const paragraphs = editor.view.dom.querySelectorAll('p'); const text = paragraphs[1].querySelector('.rich-marker').firstChild
    editor.view.focus(); window.getSelection().setBaseAndExtent(text, 0, text, 0)
  })
  await page.waitForTimeout(100); await page.keyboard.press('Backspace'); await page.waitForTimeout(100)
  const result = await page.evaluate(() => window.zeroBoundaryProbe.current())
  if (result.content !== '甲乙' || result.formatting.boundaries.length !== 0 || result.markers[0].status !== 'active'
    || result.markers[0].start !== 0 || result.markers[0].end !== 2) throw new Error(`零宽边界合并不正确 ${JSON.stringify(result)}`)
  return { result: 'PASS', document: result }
}
