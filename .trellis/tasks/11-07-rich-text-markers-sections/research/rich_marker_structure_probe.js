// 单独测试真实 ProseMirror 事务；未接入产品标记编辑。
async (page) => {
  await page.goto('http://127.0.0.1:5179/')
  return await page.evaluate(async () => {
    const compiled = await (await fetch('/src/components/scenes/RichSceneEditor.tsx')).text()
    const version = compiled.match(/@tiptap_core\.js\?v=([a-z\d]+)/)?.[1]
    if (!version) throw new Error('Tiptap 依赖版本不存在')
    const { Editor } = await import(`/node_modules/.vite/deps/@tiptap_core.js?v=${version}`)
    const { default: StarterKit } = await import(`/node_modules/.vite/deps/@tiptap_starter-kit.js?v=${version}`)
    const { default: Document } = await import(`/node_modules/.vite/deps/@tiptap_extension-document.js?v=${version}`)
    const { RichPrefixes, PreserveRichPrefixes, RichStructureGuard, RichPlainPaste, RichEnter, FocusableListItem } = await import('/src/components/scenes/richTextExtensions.ts')
    const { editorFromDocument } = await import('/src/components/scenes/richTextProjection.ts')
    const { mapMarkerTransaction } = await import('/src/components/scenes/richMarkerMapping.ts')
    const documentData = { content: '甲\r\n🙂乙\n末', formatting: { version: 1, boundaries: [], inline: [], blocks: [] }, sections: [], retired_section_keys: [], markers: [] }
    const marker = { key: '12345678-1234-4123-8123-123456789def', section_key: null, status: 'active', start: 0, end: 4, color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null }
    const mount = document.createElement('div'); document.body.appendChild(mount)
    const editor = new Editor({ element: mount, enableInputRules: false, enablePasteRules: false, injectCSS: false,
      extensions: [StarterKit.configure({ document: false, trailingNode: false, code: false, codeBlock: false, horizontalRule: false, hardBreak: false, link: false, strike: false, underline: false, listItem: false, listKeymap: false }), Document, FocusableListItem, RichPrefixes, PreserveRichPrefixes, RichStructureGuard, RichPlainPaste, RichEnter],
      content: editorFromDocument(documentData) })
    const result = []
    editor.on('transaction', ({ transaction }) => {
      if (!transaction.docChanged) return
      result.push({ steps: transaction.steps.map((step) => step.toJSON()), mapping: mapMarkerTransaction(transaction, [marker]) })
    })
    editor.commands.setTextSelection(1)
    editor.commands.toggleHeading({ level: 2 })
    editor.commands.toggleBulletList()
    editor.destroy(); mount.remove()
    return result
  })
}
