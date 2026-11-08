async (page) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('http://127.0.0.1:5179/')
  await page.evaluate(async () => {
    const source = await (await fetch('/src/components/scenes/richSectionNode.ts')).text()
    const version = source.match(/@tiptap_core\.js\?v=([a-z\d]+)/)?.[1]
    if (!version) throw new Error('Vite dependency version not found')
    const { Editor, Extension } = await import(`/node_modules/.vite/deps/@tiptap_core.js?v=${version}`)
    const { default: StarterKit } = await import(`/node_modules/.vite/deps/@tiptap_starter-kit.js?v=${version}`)
    const { AuthorSection } = await import('/src/components/scenes/richSectionNode.ts')
    const { NodeSelection } = await import(`/node_modules/.vite/deps/@tiptap_pm_state.js?v=${version}`)
    document.body.replaceChildren()
    document.body.style.margin = '0'
    const host = document.createElement('div')
    host.className = 'rich-scene-editor'
    host.style.setProperty('--rich-border', '#999')
    host.style.setProperty('--rich-bg', '#fff')
    host.style.setProperty('--rich-text', '#111')
    document.body.append(host)
    const Prefix = Extension.create({
      name: 'probePrefix',
      addGlobalAttributes() { return [{ types: ['paragraph'], attributes: {
        prefix: { default: null }, blockKey: { default: null }, boundaryBefore: { default: null },
      } }] },
    })
    const probe = window.__sectionProbe = { actions: [], compositions: [], transactions: [], NodeSelection, Editor, StarterKit, Prefix, AuthorSection }
    host.addEventListener('rich-section-action', (event) => probe.actions.push(event.detail))
    host.addEventListener('rich-section-composition', (event) => probe.compositions.push(event.detail))
    probe.editor = new Editor({
      element: host,
      extensions: [StarterKit.configure({ trailingNode: false }), Prefix, AuthorSection],
      content: { type: 'doc', content: [
        { type: 'paragraph', content: [{ type: 'text', text: '正文' }] },
        { type: 'authorSection', attrs: { key: 's-one', title: '备注一', include_in_ai: false }, content: [
          { type: 'paragraph', attrs: { prefix: '\r\n', blockKey: 'original-block', boundaryBefore: 9 },
            content: [{ type: 'text', text: '区块文字', marks: [{ type: 'bold' }] }] },
        ] },
        { type: 'paragraph', content: [{ type: 'text', text: '结尾' }] },
        { type: 'authorSection', attrs: { key: 's-two', title: '备注二', include_in_ai: true }, content: [
          { type: 'paragraph', content: [{ type: 'text', text: '第二块' }] },
        ] },
      ] },
      onTransaction: ({ transaction }) => {
        if (transaction.getMeta('rich-section-intent')) probe.transactions.push(transaction.getMeta('rich-section-intent'))
      },
    })
  })
  const first = page.locator('.rich-author-section').first()
  const input = first.getByRole('textbox', { name: '区块名称' })
  if (await input.isDisabled() || await input.getAttribute('readonly') !== null) throw new Error('initial editable state was not reflected')
  await input.fill('完整标题 😀')
  let state = await page.evaluate(() => window.__sectionProbe.editor.getJSON().content[1])
  if (state.attrs.title !== '完整标题 😀' || state.content[0].attrs.prefix !== '\r\n' || state.content[0].attrs.blockKey !== 'original-block' || state.content[0].attrs.boundaryBefore !== 9) throw new Error('metadata edit corrupted child attributes')
  await input.fill('😀'.repeat(201))
  if (await input.getAttribute('aria-invalid') !== 'true' || !await first.getByText('区块名称不能超过 200 个字符。').isVisible()) throw new Error('overlong name was not reported')
  state = await page.evaluate(() => window.__sectionProbe.editor.getJSON().content[1])
  if (state.attrs.title !== '😀'.repeat(201)) throw new Error('invalid local name was silently lost')
  await input.fill('😀'.repeat(200))
  if (await input.getAttribute('aria-invalid') !== 'false') throw new Error('200 emoji were counted as UTF-16 units')
  await input.fill('初始')
  await page.evaluate(() => { window.__sectionProbe.transactions = []; window.__sectionProbe.compositions = [] })
  await input.evaluate((el) => {
    el.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }))
    el.value = 'pin'
    el.dispatchEvent(new InputEvent('input', { bubbles: true, isComposing: true, data: 'pin' }))
  })
  state = await page.evaluate(() => window.__sectionProbe.editor.getJSON().content[1])
  if (state.attrs.title !== '初始') throw new Error('IME intermediate title was committed')
  await first.getByRole('button', { name: '折叠区块' }).click()
  if (!await first.locator('.rich-author-section__content').isVisible() || !await first.getByText('请先完成当前输入', { exact: false }).isVisible()) throw new Error('fold interrupted an active title composition')
  await input.evaluate((el) => {
    el.value = '拼音'
    el.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: '拼音' }))
    el.dispatchEvent(new InputEvent('input', { bubbles: true, data: '拼音' }))
  })
  const composition = await page.evaluate(() => ({
    title: window.__sectionProbe.editor.getJSON().content[1].attrs.title,
    events: window.__sectionProbe.compositions, transactions: window.__sectionProbe.transactions,
  }))
  if (composition.title !== '拼音' || JSON.stringify(composition.events.map((it) => it.active)) !== '[true,false]' || composition.transactions.length !== 1) throw new Error(`IME sequence wrong: ${JSON.stringify(composition)}`)
  await first.getByRole('checkbox', { name: '允许 AI 自动使用' }).check()
  if (!await page.evaluate(() => window.__sectionProbe.editor.getJSON().content[1].attrs.include_in_ai)) throw new Error('AI checkbox did not update')
  await first.getByText('AI 范围说明').click()
  if (!await first.getByText('之前生成的摘要', { exact: false }).isVisible()) throw new Error('AI retention explanation is missing')
  await page.evaluate(() => {
    const editor = window.__sectionProbe.editor
    editor.commands.focus()
    editor.commands.setTextSelection({ from: 7, to: 9 })
    window.__sectionProbe.sectionBeforeFold = JSON.stringify(editor.getJSON().content[1])
  })
  await page.waitForFunction(() => document.activeElement?.classList.contains('ProseMirror'))
  await first.getByRole('button', { name: '折叠区块' }).click()
  if (await first.locator('.rich-author-section__content').isVisible()) throw new Error('collapse failed')
  const folded = await page.evaluate(() => {
    const editor = window.__sectionProbe.editor
    const $from = editor.state.selection.$from
    return { selectedSection: Array.from({ length: $from.depth }, (_, i) => $from.node(i + 1).type.name).includes('authorSection'),
      unchanged: JSON.stringify(editor.getJSON().content[1]) === window.__sectionProbe.sectionBeforeFold }
  })
  if (folded.selectedSection || !folded.unchanged) throw new Error('fold left a selection inside hidden content or modified it')
  await page.keyboard.type('X')
  await page.keyboard.press('Home')
  await page.keyboard.press('ArrowLeft')
  await page.keyboard.type('Y')
  if (!await page.evaluate(() => JSON.stringify(window.__sectionProbe.editor.getJSON().content[1]) === window.__sectionProbe.sectionBeforeFold)) throw new Error('typing or keyboard navigation modified collapsed content')
  if (!await page.evaluate(() => window.__sectionProbe.editor.state.doc.textContent.includes('X') && window.__sectionProbe.editor.state.doc.textContent.includes('Y'))) throw new Error('typing was lost rather than directed to visible text')
  await first.getByRole('button', { name: '展开区块' }).click()
  if (!await first.locator('.rich-author-section__content').isVisible()) throw new Error('expand failed')
  await first.getByRole('button', { name: '折叠区块' }).focus()
  await page.keyboard.press('Enter')
  if (await first.locator('.rich-author-section__content').isVisible()) throw new Error('keyboard collapse failed')
  await page.evaluate(() => window.__sectionProbe.editor.view.dom.dispatchEvent(new CustomEvent('rich-section-reveal', { detail: { key: 's-two' } })))
  if (await first.locator('.rich-author-section__content').isVisible()) throw new Error('another section identity expanded the wrong section')
  await page.evaluate(() => window.__sectionProbe.editor.view.dom.dispatchEvent(new CustomEvent('rich-section-reveal', { detail: { key: 's-one' } })))
  if (!await first.locator('.rich-author-section__content').isVisible()) throw new Error('timeline reveal did not expand the target section')
  await page.evaluate(() => {
    const editor = window.__sectionProbe.editor
    editor.commands.setTextSelection({ from: 7, to: 9 })
    window.__sectionProbe.before = [editor.state.selection.from, editor.state.selection.to]
  })
  await first.getByRole('button', { name: '选区 AI 编辑' }).click()
  const selection = await page.evaluate(() => ({
    before: window.__sectionProbe.before,
    after: [window.__sectionProbe.editor.state.selection.from, window.__sectionProbe.editor.state.selection.to],
    action: window.__sectionProbe.actions.at(-1),
  }))
  if (JSON.stringify(selection.before) !== JSON.stringify(selection.after) || selection.action.action !== 'rewrite-selection' || selection.action.key !== 's-one') throw new Error(`selection action wrong: ${JSON.stringify(selection)}`)
  await first.getByRole('button', { name: '区块上移' }).click()
  await first.getByRole('button', { name: '区块下移' }).click()
  await first.getByRole('button', { name: '移动到正文光标' }).click()
  if (JSON.stringify(await page.evaluate(() => [window.__sectionProbe.editor.state.selection.from, window.__sectionProbe.editor.state.selection.to])) !== JSON.stringify(selection.before)) throw new Error('move-to-cursor action destroyed the original cursor')
  await first.getByRole('button', { name: '转为正文' }).click()
  await first.getByRole('button', { name: '删除区块' }).click()
  const actions = await page.evaluate(() => window.__sectionProbe.actions.map((it) => it.action))
  if (JSON.stringify(actions) !== JSON.stringify(['rewrite-selection', 'move-up', 'move-down', 'move-to-cursor', 'convert', 'delete'])) throw new Error('delegated actions missing')
  if (await page.locator('.rich-author-section').count() !== 2) throw new Error('node view performed a delegated destructive action')
  const handle = first.getByRole('button', { name: '拖动区块' })
  const box = await handle.boundingBox()
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await page.mouse.down()
  const selected = await page.evaluate(() => window.__sectionProbe.editor.state.selection instanceof window.__sectionProbe.NodeSelection)
  await page.mouse.up()
  if (!selected) throw new Error('drag handle did not select whole section')
  const size = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }))
  if (size.width !== 375 || size.scroll !== 375) throw new Error(`mobile overflow: ${JSON.stringify(size)}`)
  await page.evaluate(() => window.__sectionProbe.editor.setEditable(false, false))
  await page.waitForFunction(() => document.querySelector('.rich-author-section input[aria-label="区块名称"]').readOnly)
  if (!await first.getByRole('button', { name: '删除区块' }).isDisabled() || !await first.getByRole('checkbox', { name: '允许 AI 自动使用' }).isDisabled()) throw new Error('readonly controls still enabled')
  const readonlyResult = await page.evaluate(() => {
    const section = document.querySelector('.rich-author-section')
    const checkbox = section.querySelector('input[type="checkbox"]')
    const before = JSON.stringify(window.__sectionProbe.editor.getJSON())
    checkbox.checked = false
    checkbox.dispatchEvent(new Event('change', { bubbles: true }))
    section.querySelector('button[aria-label="删除区块"]').dispatchEvent(new MouseEvent('click', { bubbles: true }))
    return { unchanged: before === JSON.stringify(window.__sectionProbe.editor.getJSON()), checked: checkbox.checked, count: window.__sectionProbe.actions.length }
  })
  if (!readonlyResult.unchanged || !readonlyResult.checked || readonlyResult.count !== 6) throw new Error('permission revocation allowed interaction')
  await page.evaluate(() => window.__sectionProbe.editor.setEditable(true, false))
  await page.waitForFunction(() => !document.querySelector('.rich-author-section input[aria-label="区块名称"]').readOnly)
  // 有人工编辑权限但没有 AI 权限时，普通区块操作和 AI 自动使用开关仍可用。
  await page.evaluate(() => window.__sectionProbe.editor.view.dom.setAttribute('data-section-ai-enabled', 'false'))
  await page.waitForFunction(() => document.querySelector('button[aria-label="选区 AI 编辑"]').disabled)
  if (await first.getByRole('button', { name: '区块上移' }).isDisabled()
      || await first.getByRole('button', { name: '转为正文' }).isDisabled()
      || await first.getByRole('checkbox', { name: '允许 AI 自动使用' }).isDisabled()) throw new Error('AI 禁用误禁了普通区块编辑')
  const beforeDenied = await page.evaluate(() => window.__sectionProbe.actions.length)
  await first.getByRole('button', { name: '选区 AI 编辑' }).evaluate((button) => button.dispatchEvent(new MouseEvent('click', { bubbles: true })))
  if ((await page.evaluate(() => window.__sectionProbe.actions.length)) !== beforeDenied) throw new Error('AI 撤权后仍派发选区编辑事件')
  await page.evaluate(() => window.__sectionProbe.editor.view.dom.setAttribute('data-section-ai-enabled', 'true'))
  await page.waitForFunction(() => !document.querySelector('button[aria-label="选区 AI 编辑"]').disabled)
  await input.fill('<img src=x onerror=alert(1)>')
  const security = await page.evaluate(() => {
    const html = window.__sectionProbe.editor.getHTML()
    const rendered = document.querySelector('.rich-author-section')
    return { html, images: rendered.querySelectorAll('img').length, input: rendered.querySelector('input[aria-label="区块名称"]').value }
  })
  if (security.images || !security.input.includes('<img') || security.html.includes('s-one') || security.html.includes('onerror')) throw new Error('section metadata leaked into executable HTML')
  await first.getByRole('button', { name: '拖动区块' }).dragTo(page.locator('.rich-author-section').last().locator('.rich-author-section__content'))
  const dragged = await page.evaluate(() => window.__sectionProbe.editor.getJSON().content.filter((it) => it.type === 'authorSection'))
  if (dragged[0].attrs.key !== 's-two' || dragged[1].attrs.key !== 's-one' || dragged[1].content[0].attrs.prefix !== '\r\n' || dragged[1].content[0].attrs.blockKey !== 'original-block') throw new Error('drag failed to move the whole section intact')
  const forged = await page.evaluate(() => {
    const editor = window.__sectionProbe.editor
    editor.commands.setContent('<div data-author-section="s-one" title="伪造">文本</div>')
    return editor.getJSON().content.some((it) => it.type === 'authorSection')
  })
  if (forged) throw new Error('external HTML forged a section identity')
  await page.evaluate(() => {
    const p = window.__sectionProbe
    const host = document.createElement('div')
    document.body.append(host)
    p.solo = new p.Editor({ element: host, extensions: [p.StarterKit.configure({ trailingNode: false }), p.Prefix, p.AuthorSection],
      content: { type: 'doc', content: [{ type: 'authorSection', attrs: { key: 'solo', title: '唯一块', include_in_ai: false },
        content: [{ type: 'paragraph', content: [{ type: 'text', text: '不可隐藏的选区' }] }] }] } })
    p.solo.commands.setTextSelection(3)
  })
  const solo = page.locator('.rich-author-section').last()
  await solo.getByRole('button', { name: '折叠区块' }).click()
  if (!await solo.locator('.rich-author-section__content').isVisible() || !await solo.getByText('请先在区块外添加正文光标位置', { exact: false }).isVisible()) throw new Error('section-only document hid the only editable selection')
  return 'PASS: Chromium 375px metadata, Unicode, simulated IME, safe folding/keyboard, selection, real drag, AI permission changes, readonly and HTML isolation'
}
