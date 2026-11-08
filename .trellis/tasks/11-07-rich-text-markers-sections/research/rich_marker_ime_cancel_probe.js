// CDP 模拟组合输入落在标记中；不能当成所有操作系统真实输入法的覆盖。
async (page) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('http://127.0.0.1:8766/')
  const target = await page.evaluate(async () => {
    const call = async (url, method, body) => {
      const response = await fetch(`/api/v1${url}`, { method, headers: { 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
      if (!response.ok) throw new Error(await response.text())
      return response.json()
    }
    await call('/auth/register', 'POST', { username: `mkime${crypto.randomUUID().slice(0, 8)}`, password: 'secret123' })
    const project = await call('/projects', 'POST', { title: '输入法标记隔离测试', genre: '科幻', premise: '测试', style: '' })
    const chapter = await call(`/projects/${project.id}/chapters`, 'POST', { title: '正文', content: '甲\r\n🙂乙' })
    const scene = (await call(`/projects/${project.id}/chapters/${chapter.id}/scenes`, 'GET')).items[0]
    const endpoint = `/projects/${project.id}/chapters/${chapter.id}/scenes/${scene.id}/document`
    const document = await call(endpoint, 'GET'); const key = crypto.randomUUID()
    document.document.markers.push({ key, section_key: null, status: 'active', start: 0, end: 1, color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null })
    await call(endpoint, 'PUT', { expected_content_revision: document.content_revision, document: document.document, add_section_keys: [], delete_section_keys: [], add_marker_keys: [key], delete_marker_keys: [], mapped_section_keys: [], mapped_marker_keys: [] })
    return { pid: project.id, cid: chapter.id, sid: scene.id }
  })
  const endpoint = `/api/v1/projects/${target.pid}/chapters/${target.cid}/scenes/${target.sid}/document`
  await page.goto(`http://127.0.0.1:8766/projects/${target.pid}/chapters/${target.cid}?scene_id=${target.sid}`)
  const editor = page.getByRole('textbox', { name: '当前场景正文' })
  await editor.waitFor({ timeout: 20000 })
  await editor.focus()
  await editor.evaluate((root) => {
    const text = root.querySelector('p .rich-marker').firstChild
    window.getSelection().setBaseAndExtent(text, 0, text, text.length)
    window.__imeEvents = []
    root.addEventListener('compositionend', (event) => window.__imeEvents.push(event.data), { once: true })
  })
  await page.waitForTimeout(100)
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('Input.imeSetComposition', { text: 'pin', selectionStart: 3, selectionEnd: 3 })
  await page.waitForTimeout(100)
  if (!(await page.getByRole('button', { name: /保存/ }).first().isDisabled())) throw new Error('组合期间保存未禁用')
  await page.keyboard.press('Control+s')
  const during = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  if (during.document.content !== '甲\r\n🙂乙') throw new Error('组合期间错误写入服务端')
  await cdp.send('Input.imeSetComposition', { text: '', selectionStart: 0, selectionEnd: 0 })
  await page.waitForTimeout(300)
  const after = await editor.evaluate((root) => ({ events: window.__imeEvents, doc: root.pmViewDesc.node.toJSON(), text: root.textContent }))
  if (JSON.stringify(after.events) !== JSON.stringify(['']) || after.text !== '🙂乙' || after.doc.attrs.markers[0].status !== 'stale'
      || after.doc.attrs.markers[0].excerpt !== '甲') throw new Error(`取消输入后的内容或失效标记错误 ${JSON.stringify(after)}`)
  await page.getByRole('button', { name: /保存/ }).first().click()
  await page.waitForTimeout(300)
  const saved = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  if (saved.document.content !== '\r\n🙂乙' || saved.document.markers[0].excerpt !== '甲') throw new Error('取消后保存错误')
  await page.getByRole('button', { name: /撤\s*销/ }).click()
  const undone = await editor.evaluate((root) => root.pmViewDesc.node.toJSON())
  if (undone.content[0].content[0].text !== '甲' || undone.attrs.markers[0].status !== 'active') throw new Error(`取消的撤销不一致 ${JSON.stringify(undone)}`)
  await page.getByRole('button', { name: /保存/ }).first().click()
  await page.waitForTimeout(300)
  await page.getByRole('button', { name: /重\s*做/ }).click()
  await page.getByRole('button', { name: /保存/ }).first().click()
  await page.waitForTimeout(300)
  const redone = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  if (redone.document.content !== '\r\n🙂乙' || redone.document.markers[0].status !== 'stale' || redone.document.markers[0].excerpt !== '甲') throw new Error(`取消的重做不一致 ${JSON.stringify(redone)}`)
  await cdp.detach()
  return { result: 'PASS', cancelledContent: redone.document.content, marker: redone.document.markers[0] }
}
