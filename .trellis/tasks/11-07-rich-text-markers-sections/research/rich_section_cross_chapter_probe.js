// 跨章区块标记定位之前必须保存当前草稿，并在目标章按区块身份定位。
async (page) => {
  await page.setViewportSize({ width: 375, height: 900 })
  await page.goto('http://127.0.0.1:8767/')
  const target = await page.evaluate(async () => {
    const call = async (url, method, body) => {
      const response = await fetch(`/api/v1${url}`, { method, headers: { 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
      if (!response.ok) throw new Error(await response.text())
      return response.json()
    }
    await call('/auth/register', 'POST', { username: `sxch${crypto.randomUUID().slice(0, 8)}`, password: 'secret123' })
    const project = await call('/projects', 'POST', { title: '跨章区块标记测试', genre: '科幻', premise: '测试', style: '' })
    const first = await call(`/projects/${project.id}/chapters`, 'POST', { title: '第一章', content: '前章\r\n正文' })
    const second = await call(`/projects/${project.id}/chapters`, 'POST', { title: '第二章', content: '后章🙂' })
    const scene1 = (await call(`/projects/${project.id}/chapters/${first.id}/scenes`, 'GET')).items[0]
    const scene2 = (await call(`/projects/${project.id}/chapters/${second.id}/scenes`, 'GET')).items[0]
    const endpoint = `/projects/${project.id}/chapters/${second.id}/scenes/${scene2.id}/document`
    const source = await call(endpoint, 'GET'); const key = crypto.randomUUID(); const marker = crypto.randomUUID()
    source.document.sections.push({ key, anchor: 1, order: 0, title: '跨章目标', content: '跨章🙂片段',
      formatting: { version: 1, boundaries: [], inline: [], blocks: [] }, include_in_ai: false })
    source.document.markers.push({ key: marker, section_key: key, status: 'active', start: 0, end: 3,
      color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null })
    await call(endpoint, 'PUT', { expected_content_revision: source.content_revision, document: source.document,
      add_section_keys: [key], delete_section_keys: [], add_marker_keys: [marker], delete_marker_keys: [], mapped_section_keys: [], mapped_marker_keys: [] })
    return { pid: project.id, first: first.id, second: second.id, scene1: scene1.id }
  })
  await page.goto(`http://127.0.0.1:8767/projects/${target.pid}/chapters/${target.first}?scene_id=${target.scene1}`)
  let editor = page.getByRole('textbox', { name: '当前场景正文' })
  await editor.waitFor({ timeout: 20000 })
  await editor.locator('p').first().click(); await page.keyboard.press('End'); await page.keyboard.insertText('新')
  await page.getByRole('button', { name: '全书标记' }).click()
  await page.getByRole('list', { name: '全书正文标记' }).getByRole('button', { name: /定位第 2 章/ }).click()
  await page.waitForURL(new RegExp(`/chapters/${target.second}(?:\\?|$)`), { timeout: 20000 })
  editor = page.getByRole('textbox', { name: '当前场景正文' })
  await editor.locator('section.rich-author-section').waitFor({ timeout: 20000 })
  await page.waitForTimeout(600)
  if (await page.evaluate(() => String(window.getSelection())) !== '跨章🙂') throw new Error('跨章区块目标没有正确定位')
  const saved = await page.evaluate(async ({ pid, chapter }) => (await fetch(`/api/v1/projects/${pid}/chapters/${chapter}`)).json(), { pid: target.pid, chapter: target.first })
  if (saved.content !== '前章新\r\n正文') throw new Error(`跨章定位前未保护草稿 ${JSON.stringify(saved)}`)
  return { result: 'PASS', saved: saved.content }
}
