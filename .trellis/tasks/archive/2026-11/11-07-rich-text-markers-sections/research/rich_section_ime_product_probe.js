// CDP 模拟区块内中文组合输入；不代表所有系统真实输入法。
async (page) => {
  await page.setViewportSize({ width: 375, height: 900 })
  await page.goto('http://127.0.0.1:8767/')
  const target = await page.evaluate(async () => {
    const call = async (url, method, body) => {
      const response = await fetch(`/api/v1${url}`, { method, headers: { 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
      if (!response.ok) throw new Error(await response.text())
      return response.json()
    }
    await call('/auth/register', 'POST', { username: `sime${crypto.randomUUID().slice(0, 8)}`, password: 'secret123' })
    const project = await call('/projects', 'POST', { title: '区块输入法隔离测试', genre: '科幻', premise: '测试', style: '' })
    const chapter = await call(`/projects/${project.id}/chapters`, 'POST', { title: '正文', content: '正文🙂' })
    const scene = (await call(`/projects/${project.id}/chapters/${chapter.id}/scenes`, 'GET')).items[0]
    const endpoint = `/projects/${project.id}/chapters/${chapter.id}/scenes/${scene.id}/document`
    const source = await call(endpoint, 'GET'); const key = crypto.randomUUID(); const marker = crypto.randomUUID()
    source.document.sections.push({ key, anchor: 1, order: 0, title: '输入法区块', content: '甲\r\n🙂乙',
      formatting: { version: 1, boundaries: [], inline: [], blocks: [] }, include_in_ai: false })
    source.document.markers.push({ key: marker, section_key: key, status: 'active', start: 0, end: 4,
      color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null })
    await call(endpoint, 'PUT', { expected_content_revision: source.content_revision, document: source.document,
      add_section_keys: [key], delete_section_keys: [], add_marker_keys: [marker], delete_marker_keys: [], mapped_section_keys: [], mapped_marker_keys: [] })
    return { pid: project.id, cid: chapter.id, sid: scene.id }
  })
  const endpoint = `/api/v1/projects/${target.pid}/chapters/${target.cid}/scenes/${target.sid}/document`
  await page.goto(`http://127.0.0.1:8767/projects/${target.pid}/chapters/${target.cid}?scene_id=${target.sid}`)
  const editor = page.getByRole('textbox', { name: '当前场景正文' })
  await editor.waitFor({ timeout: 20000 })
  await editor.focus()
  await editor.locator('section.rich-author-section .rich-author-section__content').evaluate((root) => {
    const first = root.querySelectorAll('p')[0].querySelector('.rich-marker').firstChild
    const second = root.querySelectorAll('p')[1].querySelector('.rich-marker').firstChild
    window.getSelection().setBaseAndExtent(first, 0, second, second.length)
  })
  await page.waitForTimeout(100)
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('Input.imeSetComposition', { text: 'pin', selectionStart: 3, selectionEnd: 3 })
  await page.waitForTimeout(150)
  if (!(await page.getByRole('button', { name: /保存/ }).first().isDisabled())) throw new Error('组合期间未禁保存')
  await page.keyboard.press('Control+s')
  const during = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  if (during.document.sections[0].content !== '甲\r\n🙂乙') throw new Error('组合期间错误提交')
  await cdp.send('Input.imeSetComposition', { text: '拼音', selectionStart: 2, selectionEnd: 2 })
  await cdp.send('Input.insertText', { text: '拼音' })
  await page.waitForTimeout(250)
  let pending = page.waitForResponse((response) => response.url().endsWith('/document') && response.request().method() === 'PUT')
  await page.getByRole('button', { name: /保存/ }).first().click()
  let response = await pending
  let saved = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  if (response.status() !== 200 || saved.document.content !== '正文🙂' || saved.document.sections[0].content !== '拼音乙'
    || saved.document.markers[0].status !== 'stale' || saved.document.markers[0].excerpt !== '甲\r\n🙂') {
    throw new Error(`区块组合保存错误 ${JSON.stringify({ status: response.status(), saved })}`)
  }
  await page.getByRole('button', { name: /撤\s*销/ }).click()
  pending = page.waitForResponse((result) => result.url().endsWith('/document') && result.request().method() === 'PUT')
  await page.getByRole('button', { name: /保存/ }).first().click()
  response = await pending
  saved = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  if (response.status() !== 200 || saved.document.sections[0].content !== '甲\r\n🙂乙' || saved.document.markers[0].status !== 'active') {
    throw new Error(`撤销区块组合未恢复 ${JSON.stringify(saved)}`)
  }
  await page.getByRole('button', { name: /重\s*做/ }).click()
  pending = page.waitForResponse((result) => result.url().endsWith('/document') && result.request().method() === 'PUT')
  await page.getByRole('button', { name: /保存/ }).first().click()
  response = await pending
  saved = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  if (response.status() !== 200 || saved.document.sections[0].content !== '拼音乙' || saved.document.markers[0].status !== 'stale') {
    throw new Error(`重做区块组合未恢复 ${JSON.stringify(saved)}`)
  }
  await cdp.detach()
  return { result: 'PASS', saved }
}
