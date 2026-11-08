// 无拖动的任意正文位置移动；焦点进入按钮后用键盘执行。
async (page) => {
  await page.setViewportSize({ width: 375, height: 900 })
  await page.goto('http://127.0.0.1:8767/')
  const target = await page.evaluate(async () => {
    const call = async (url, method, body) => {
      const response = await fetch(`/api/v1${url}`, { method, headers: { 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
      if (!response.ok) throw new Error(await response.text())
      return response.json()
    }
    await call('/auth/register', 'POST', { username: `smove${crypto.randomUUID().slice(0, 8)}`, password: 'secret123' })
    const project = await call('/projects', 'POST', { title: '区块键盘移动测试', genre: '科幻', premise: '测试', style: '' })
    const chapter = await call(`/projects/${project.id}/chapters`, 'POST', { title: '正文', content: '甲\r\n🙂乙' })
    const scene = (await call(`/projects/${project.id}/chapters/${chapter.id}/scenes`, 'GET')).items[0]
    const endpoint = `/projects/${project.id}/chapters/${chapter.id}/scenes/${scene.id}/document`
    const source = await call(endpoint, 'GET'); const key = crypto.randomUUID(); const marker = crypto.randomUUID()
    source.document.sections.push({ key, anchor: 1, order: 0, title: '移动区块', content: '备用🙂',
      formatting: { version: 1, boundaries: [], inline: [], blocks: [] }, include_in_ai: false })
    source.document.markers.push({ key: marker, section_key: key, status: 'active', start: 0, end: 3,
      color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null })
    await call(endpoint, 'PUT', { expected_content_revision: source.content_revision, document: source.document,
      add_section_keys: [key], delete_section_keys: [], add_marker_keys: [marker], delete_marker_keys: [], mapped_section_keys: [], mapped_marker_keys: [] })
    return { pid: project.id, cid: chapter.id, sid: scene.id, key, marker }
  })
  const endpoint = `/api/v1/projects/${target.pid}/chapters/${target.cid}/scenes/${target.sid}/document`
  await page.goto(`http://127.0.0.1:8767/projects/${target.pid}/chapters/${target.cid}?scene_id=${target.sid}`)
  const editor = page.getByRole('textbox', { name: '当前场景正文' }); await editor.waitFor({ timeout: 20000 })
  await editor.evaluate((root) => {
    root.focus(); const paragraphs = [...root.children].filter((node) => node.tagName === 'P')
    const text = paragraphs.at(-1).firstChild
    window.getSelection().setBaseAndExtent(text, 2, text, 2) // emoji 之后，码点 4
  })
  await page.waitForTimeout(100)
  const move = editor.locator('section.rich-author-section').getByRole('button', { name: '移动到正文光标' })
  await move.focus(); await page.keyboard.press('Enter')
  const pending = page.waitForResponse((response) => response.url().endsWith('/document') && response.request().method() === 'PUT')
  await page.getByRole('button', { name: /保存/ }).first().click()
  const response = await pending
  const saved = await page.evaluate(async (path) => (await fetch(path)).json(), endpoint)
  if (response.status() !== 200 || saved.document.content !== '甲\r\n🙂乙' || saved.document.sections[0].anchor !== 4
    || saved.document.sections[0].key !== target.key || saved.document.markers[0].key !== target.marker
    || saved.document.markers[0].section_key !== target.key) throw new Error(`键盘移动未保留材料 ${JSON.stringify(saved)}`)
  return { result: 'PASS', saved }
}
