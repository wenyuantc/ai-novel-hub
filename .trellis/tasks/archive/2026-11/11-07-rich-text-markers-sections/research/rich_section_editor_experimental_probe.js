// 实际 Chromium + 实验性完整区块 schema/事务守卫；不代表产品已接入。
async (page) => {
  await page.setViewportSize({ width: 375, height: 900 })
  await page.goto('http://127.0.0.1:5179/')
  await page.evaluate(async () => {
    const compiled = await (await fetch('/src/components/scenes/RichSceneEditor.tsx')).text()
    const version = compiled.match(/@tiptap_core\.js\?v=([a-z\d]+)/)?.[1]
    if (!version) throw new Error('Tiptap 依赖版本不存在')
    const { Editor } = await import(`/node_modules/.vite/deps/@tiptap_core.js?v=${version}`)
    const { default: StarterKit } = await import(`/node_modules/.vite/deps/@tiptap_starter-kit.js?v=${version}`)
    const { RichMarkerDocument, RichMarkerState } = await import('/src/components/scenes/richMarkerState.ts')
    const { AuthorSection } = await import('/src/components/scenes/richSectionNode.ts')
    const { RichSectionGuard } = await import('/src/components/scenes/richSectionGuard.ts')
    const { RichSectionHistory } = await import('/src/components/scenes/richSectionHistory.ts')
    const { FocusableListItem, RichPrefixes, PreserveRichPrefixes, RichExactSelection, RichPlainPaste, RichEnter } = await import('/src/components/scenes/richTextExtensions.ts')
    const { editorFromSectionDocument, projectSectionDocument } = await import('/src/components/scenes/richSectionProjection.ts')
    const { richCursorCandidates } = await import('/src/components/scenes/richEditorPositions.ts')
    const { createSectionPlanTransaction } = await import('/src/components/scenes/richSectionTransaction.ts')
    const format = () => ({ version: 1, boundaries: [], inline: [], blocks: [] })
    const documentData = { content: '甲🙂\r\n乙', formatting: format(), sections: [
      { key: 'a', anchor: 1, order: 0, title: '备选', content: '备🙂\r\n末', formatting: format(), include_in_ai: false },
    ], retired_section_keys: [], markers: [
      { key: 'body', section_key: null, status: 'active', start: 0, end: 1, color: 'red', excerpt: '', excerpt_truncated: false, last_start: null },
      { key: 'part', section_key: 'a', status: 'active', start: 0, end: 2, color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null },
    ] }
    let saved = documentData
    document.body.innerHTML = '<div id="probe"></div>'
    const editor = new Editor({ element: document.querySelector('#probe'), enableInputRules: false, enablePasteRules: false, injectCSS: false,
      extensions: [StarterKit.configure({ document: false, trailingNode: false, code: false, codeBlock: false,
        horizontalRule: false, hardBreak: false, link: false, strike: false, underline: false, listItem: false, listKeymap: false }),
      RichMarkerDocument, FocusableListItem, RichPrefixes, PreserveRichPrefixes,
      RichSectionGuard.configure({ getSavedDocument: () => saved }), RichSectionHistory.configure({ getSavedDocument: () => saved }),
      RichMarkerState.configure({ sectionMode: true, getSavedDocument: () => saved }), AuthorSection,
      RichExactSelection, RichPlainPaste, RichEnter], content: editorFromSectionDocument(documentData) })
    const warnings = []
    editor.view.dom.addEventListener('rich-mapping-rejected', () => warnings.push('mapping'))
    const point = (scope, offset, key, side = 0) => {
      const found = richCursorCandidates(editor.state.doc, { scope, offset, sectionKey: key })
      if (!found[side]) throw new Error(`位置不存在 ${scope} ${offset} ${JSON.stringify(found)}`)
      return found[side].pm
    }
    window.experimentalSection = { editor, saved: documentData, warnings,
      select(scope, start, end = start, key = 'a', side = 0) { editor.view.focus(); editor.commands.setTextSelection({ from: point(scope, start, key, side), to: point(scope, end, key, side) }) },
      snapshot() { return projectSectionDocument(editor.getJSON()) },
      saveBaseline() { saved = structuredClone(projectSectionDocument(editor.getJSON())) },
      create() { editor.view.dispatch(createSectionPlanTransaction(editor.state, { action: 'create', key: 'b', cursor: {
        scope: 'prose', offset: 2, leftSection: 'a', rightSection: null,
      } }, saved)) },
      deleteA() { editor.view.dispatch(createSectionPlanTransaction(editor.state, { action: 'delete', key: 'a' }, saved)) },
    }
  })
  const get = () => page.evaluate(() => window.experimentalSection.snapshot())
  await page.evaluate(() => window.experimentalSection.select('prose', 1))
  await page.keyboard.insertText('X')
  let doc = await get()
  if (doc.content !== '甲X🙂\r\n乙' || doc.sections[0].anchor !== 2) throw new Error(`正文插入与锚点不符 ${JSON.stringify(doc)}`)
  await page.evaluate(() => window.experimentalSection.select('section', 1))
  await page.keyboard.insertText('Z')
  doc = await get()
  if (doc.sections[0].content !== '备Z🙂\r\n末' || doc.markers[1].end !== 3) throw new Error(`区块插入与标记不符 ${JSON.stringify(doc)}`)
  await page.waitForTimeout(650)
  await page.evaluate(() => window.experimentalSection.select('section', 0, 3))
  await page.keyboard.press('Backspace')
  doc = await get()
  if (doc.sections[0].content !== '\r\n末' || doc.markers[1].status !== 'stale' || doc.markers[1].excerpt !== '备🙂'
    || doc.markers[1].draft_excerpt !== '备Z🙂') throw new Error(`区块全删及双片段不符 ${JSON.stringify(doc)}`)
  await page.keyboard.press('Meta+z')
  doc = await get()
  if (doc.sections[0].content !== '备Z🙂\r\n末' || doc.markers[1].status !== 'active') throw new Error(`撤销未恢复区块标记 ${JSON.stringify(doc)}`)
  await page.evaluate(() => window.experimentalSection.select('section', 1))
  await page.keyboard.press('Enter')
  doc = await get()
  if (doc.sections[0].content !== '备\nZ🙂\r\n末' || doc.markers[1].end !== 4) throw new Error(`区块 Enter 与标记不符 ${JSON.stringify(doc)}`)
  await page.evaluate(() => window.experimentalSection.create())
  doc = await get()
  if (doc.sections.map((section) => section.key).join(',') !== 'a,b' || doc.sections[1].include_in_ai) throw new Error(`创建区块不符 ${JSON.stringify(doc)}`)
  await page.evaluate(() => window.experimentalSection.saveBaseline())
  await page.keyboard.press('Meta+z')
  doc = await get()
  if (doc.sections.map((section) => section.key).join(',') !== 'a' || !doc.retired_section_keys.includes('b')
    || doc.markers[1].status !== 'active') throw new Error(`跨保存撤销创建丢失墓碑 ${JSON.stringify(doc)}`)
  await page.evaluate(() => window.experimentalSection.saveBaseline())
  await page.keyboard.press('Meta+Shift+z')
  doc = await get()
  if (doc.sections.map((section) => section.key).join(',') !== 'a,b' || doc.retired_section_keys.includes('b')
    || doc.markers[1].status !== 'active') throw new Error(`跨保存重做创建未恢复 ${JSON.stringify(doc)}`)
  await page.evaluate(() => { window.experimentalSection.select('section', 0, 1); window.experimentalSection.editor.commands.toggleBulletList() })
  doc = await get()
  if (doc.sections[0].content !== '备\nZ🙂\r\n末' || doc.sections[0].formatting.blocks[0]?.kind !== 'bullet_list'
    || doc.markers[1].end !== 4) throw new Error(`区块列表转换丢文字或标记 ${JSON.stringify(doc)}`)
  await page.evaluate(() => window.experimentalSection.select('section', 0, 0, 'b'))
  await page.evaluate(() => {
    const data = new DataTransfer(); data.setData('text/plain', '甲\r\n乙\n丙')
    data.setData('text/html', '<img src=x onerror="window.__sectionUnsafe=1">')
    window.experimentalSection.editor.view.dom.dispatchEvent(new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: data }))
  })
  doc = await get()
  if (doc.sections[1].content !== '甲\r\n乙\n丙' || await page.evaluate(() => window.__sectionUnsafe === 1)) {
    throw new Error(`区块多行纯文本粘贴不符 ${JSON.stringify(doc)}`)
  }
  const beforeCross = JSON.stringify(doc)
  await page.evaluate(() => window.experimentalSection.select('prose', 0, 5))
  await page.keyboard.press('Backspace')
  if (JSON.stringify(await get()) !== beforeCross) throw new Error('跨区块删除绕过守卫')
  await page.evaluate(() => { window.experimentalSection.saveBaseline(); window.experimentalSection.deleteA() })
  doc = await get()
  if (doc.sections.map((section) => section.key).join(',') !== 'b' || !doc.retired_section_keys.includes('a')
    || doc.markers[1].status !== 'stale') throw new Error(`删除区块未保留失效标记 ${JSON.stringify(doc)}`)
  await page.evaluate(() => window.experimentalSection.saveBaseline())
  await page.keyboard.press('Meta+z')
  doc = await get()
  if (doc.sections.map((section) => section.key).join(',') !== 'a,b' || doc.retired_section_keys.includes('a')
    || doc.markers[1].status !== 'active') throw new Error(`跨保存撤销删除未恢复标记 ${JSON.stringify(doc)}`)
  await page.evaluate(() => window.experimentalSection.saveBaseline())
  await page.keyboard.press('Meta+Shift+z')
  doc = await get()
  if (doc.sections.map((section) => section.key).join(',') !== 'b' || !doc.retired_section_keys.includes('a')
    || doc.markers[1].status !== 'stale') throw new Error(`跨保存重做删除未保留标记 ${JSON.stringify(doc)}`)
  const warnings = await page.evaluate(() => window.experimentalSection.warnings)
  return { result: 'PASS', doc, warnings }
}
