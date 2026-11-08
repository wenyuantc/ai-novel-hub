// 已保存的失效正文标记显式重关联到区块，验证目标作用域及哈希。
async (page) => {
  await page.setViewportSize({ width: 375, height: 900 })
  await page.goto('http://127.0.0.1:8767/')
  const target = await page.evaluate(async () => {
    const call = async (url, method, body) => {
      const response = await fetch(`/api/v1${url}`, { method, headers: { 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
      if (!response.ok) throw new Error(await response.text())
      return response.json()
    }
    await call('/auth/register', 'POST', { username: `srel${crypto.randomUUID().slice(0, 8)}`, password: 'secret123' })
    const project = await call('/projects', 'POST', { title: '区块标记重关联测试', genre: '科幻', premise: '测试', style: '' })
    const chapter = await call(`/projects/${project.id}/chapters`, 'POST', { title: '正文', content: '正文🙂' })
    const scene = (await call(`/projects/${project.id}/chapters/${chapter.id}/scenes`, 'GET')).items[0]
    const endpoint = `/projects/${project.id}/chapters/${chapter.id}/scenes/${scene.id}/document`
    const source = await call(endpoint, 'GET'); const key = crypto.randomUUID(); const marker = crypto.randomUUID()
    source.document.sections.push({ key, anchor: 1, order: 0, title: '目标区块', content: '目标🙂末',
      formatting: { version: 1, boundaries: [], inline: [], blocks: [] }, include_in_ai: false })
    source.document.markers.push({ key: marker, section_key: null, status: 'stale', start: null, end: null,
      color: 'blue', excerpt: '旧文', excerpt_truncated: false, last_start: 0 })
    await call(endpoint, 'PUT', { expected_content_revision: source.content_revision, document: source.document,
      add_section_keys: [key], delete_section_keys: [], add_marker_keys: [marker], delete_marker_keys: [], mapped_section_keys: [], mapped_marker_keys: [] })
    return { pid: project.id, cid: chapter.id, sid: scene.id, key, marker }
  })
  const endpoint = `/api/v1/projects/${target.pid}/chapters/${target.cid}/scenes/${target.sid}/document`
  await page.goto(`http://127.0.0.1:8767/projects/${target.pid}/chapters/${target.cid}?scene_id=${target.sid}`)
  const editor = page.getByRole('textbox', { name: '当前场景正文' })
  await editor.waitFor({ timeout: 20000 })
  await editor.locator('section.rich-author-section .rich-author-section__content').evaluate((root) => {
    const text = root.querySelector('p').firstChild
    window.getSelection().setBaseAndExtent(text, 0, text, 4) // 目标🙂 的 UTF-16 长度
  })
  await page.waitForTimeout(100)
  await page.getByRole('button', { name: '全书标记' }).click()
  const list = page.getByRole('list', { name: '全书正文标记' })
  await list.getByRole('button', { name: /重新关联/ }).click()
  await page.waitForTimeout(500)
  const saved = await page.evaluate(async (path) => (await fetch(path)).json(), endpoint)
  const marker = saved.document.markers[0]
  if (marker.key !== target.marker || marker.section_key !== target.key || marker.status !== 'active'
    || marker.start !== 0 || marker.end !== 3 || saved.document.content !== '正文🙂') {
    throw new Error(`区块标记重关联失败 ${JSON.stringify(saved)}`)
  }
  return { result: 'PASS', marker }
}
