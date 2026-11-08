// 真实 Tiptap 事务探针：标题转换列表必须保留每段原始前缀。
async (page) => {
  await page.goto('http://127.0.0.1:5179/')
  return page.evaluate(async () => {
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
    const formatting = () => ({ version: 1, boundaries: [], inline: [], blocks: [] })
    const documentData = { content: '甲\r\n乙\n末', formatting: formatting(), sections: [
      { key: 'a', anchor: 3, order: 0, title: '备注', content: '备', formatting: formatting(), include_in_ai: false },
    ], retired_section_keys: [], markers: [
      { key: 'body', section_key: null, status: 'active', start: 0, end: 1, color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null },
      { key: 'part', section_key: 'a', status: 'active', start: 0, end: 1, color: 'red', excerpt: '', excerpt_truncated: false, last_start: null },
    ] }
    const mount = document.createElement('div'); document.body.appendChild(mount); const events = []
    const editor = new Editor({ element: mount, enableInputRules: false, enablePasteRules: false, injectCSS: false,
      extensions: [StarterKit.configure({ document: false, trailingNode: false, code: false, codeBlock: false,
        horizontalRule: false, hardBreak: false, link: false, strike: false, underline: false, listItem: false, listKeymap: false }),
      RichMarkerDocument, FocusableListItem, RichPrefixes, PreserveRichPrefixes,
      RichSectionGuard.configure({ getSavedDocument: () => documentData }), RichSectionHistory.configure({ getSavedDocument: () => documentData }),
      RichMarkerState.configure({ sectionMode: true, getSavedDocument: () => documentData }), AuthorSection,
      RichExactSelection, RichPlainPaste, RichEnter], content: editorFromSectionDocument(documentData),
      onTransaction({ editor, transaction, appendedTransactions }) {
        let projection
        try { projection = projectSectionDocument(editor.getJSON()) } catch (error) { projection = `ERROR: ${error.message}` }
        events.push({ steps: transaction.steps.map((step) => step.toJSON()), appended: appendedTransactions.map((tr) => tr.steps.map((step) => step.toJSON())),
          doc: editor.getJSON(), projection })
      } })
    editor.view.focus(); editor.commands.setTextSelection(1)
    editor.commands.toggleHeading({ level: 2 })
    editor.commands.toggleBulletList()
    const final = events.at(-1)
    if (typeof final.projection === 'string' || final.projection.content !== documentData.content
      || final.projection.formatting.blocks[0]?.kind !== 'bullet_list'
      || JSON.stringify(final.projection.markers) !== JSON.stringify(documentData.markers)
      || final.projection.sections[0].anchor !== 3 || final.projection.sections[0].content !== '备') throw new Error(JSON.stringify(events))
    editor.destroy(); mount.remove()
    return { result: 'PASS', events: events.length }
  })
}
