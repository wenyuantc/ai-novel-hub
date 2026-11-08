async (page) => {
  await page.goto('http://127.0.0.1:5178/')
  await page.evaluate(async () => {
    const code = await (await fetch('/src/components/scenes/RichReviewModal.tsx')).text()
    const depsVersion = code.match(/antd\.js\?v=([a-z\d]+)/)?.[1]
    if (!depsVersion) throw new Error('Vite dependency version not found')
    const { default: React } = await import(`/node_modules/.vite/deps/react.js?v=${depsVersion}`)
    const { default: ReactDOM } = await import(`/node_modules/.vite/deps/react-dom_client.js?v=${depsVersion}`)
    const { App } = await import(`/node_modules/.vite/deps/antd.js?v=${depsVersion}`)
    const { default: Modal } = await import('/src/components/scenes/RichReviewModal.tsx')
    const format = { version: 1, boundaries: [], inline: [], blocks: [] }
    const key = '645d44c0-cc16-4949-973b-542022da8421'
    const identity = { task_key: '11a11111-1111-4111-8111-111111111111', run_key: '22a22222-2222-4222-8222-222222222222', token: '33a33333-3333-4333-8333-333333333333', chapter_id: 2, phase: 'awaiting_review' }
    const task = { id: 1, project_id: 1, chapter_id: 2, rich_review: identity, status: 'paused', kind: 'chapter.generate' }
    const preview = { reason: 'rich_document_review', ...identity, status: 'pending', task_status: 'paused', content_revision: 1, scenes: [{
      scene_id: 3, original: { content: '甲旧乙', formatting: format, sections: [{ key, anchor: 1, order: 0, title: '作者备选', content: '不丢失', formatting: format, include_in_ai: false }], retired_section_keys: [], markers: [] },
      candidate: '甲\r\n🙂乙', proposed_formatting: format, proposed_markers: [], proposed_sections: [{ key, anchor: null, order: 0 }], unresolved_sections: [key], format_loss: true,
    }] }
    window.__reviewCalls = []
    const previous = window.fetch
    window.fetch = async (input, init) => {
      const url = String(input)
      if (url.includes('/rich-proposals/') && init?.method === 'POST') {
        window.__reviewCalls.push(JSON.parse(String(init.body)))
        return new Response(JSON.stringify({ status: 'applied', task_status: 'succeeded', chapter_id: 2, content_revision: 2 }), { headers: { 'content-type': 'application/json' } })
      }
      if (url.includes('/rich-proposals/')) return new Response(JSON.stringify(preview), { headers: { 'content-type': 'application/json' } })
      if (url.endsWith('/tasks/1')) return new Response(JSON.stringify({ ...task, status: 'succeeded' }), { headers: { 'content-type': 'application/json' } })
      return previous(input, init)
    }
    const node = document.createElement('div')
    node.id = 'rich-review-probe'
    document.body.appendChild(node)
    ReactDOM.createRoot(node).render(React.createElement(App, null, React.createElement(Modal, { task, onClose: () => { window.__reviewClosed = true }, onChanged: () => {} })))
  })
  await page.getByText('核对 AI 候选稿').waitFor()
  const input = page.getByLabel('场景 1 候选正文')
  await input.waitFor()
  const expected = await input.evaluate((element) => {
    element.focus()
    element.setSelectionRange(1, 1)
    return element.value
  })
  if (expected !== '甲\n🙂乙') throw new Error(`textarea normalisation differed: ${JSON.stringify(expected)}`)
  await page.keyboard.press('ArrowRight')
  await page.getByRole('button', { name: '使用候选光标' }).click()
  const anchor = await page.getByLabel('作者备选的新位置').inputValue()
  if (anchor !== '3') throw new Error(`CRLF cursor mapped to ${anchor}, expected 3`)
  const apply = page.getByRole('button', { name: '确认并采纳整章' })
  if (await apply.isEnabled()) throw new Error('format loss accepted without explicit confirmation')
  await page.getByText('我已核对候选，确认接受所提示的格式损失').click()
  if (!(await apply.isEnabled())) throw new Error('valid confirmed proposal stayed disabled')
  await apply.click()
  await page.waitForFunction(() => window.__reviewClosed === true)
  const calls = await page.evaluate(() => window.__reviewCalls)
  if (calls.length !== 1 || calls[0].sections[0].anchor !== 3 || calls[0].confirm_format_loss !== true) throw new Error(JSON.stringify(calls))
  return 'PASS: Chromium reviewed CRLF/emoji anchor, required format confirmation, sent exact identity and cursor once'
}
