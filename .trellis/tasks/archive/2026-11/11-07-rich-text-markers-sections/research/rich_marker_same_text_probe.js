// 相同文字的显式替换也是旧文字删除；不能用重复词面重新定位标记。
async (page) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('http://127.0.0.1:8766/')
  const target = await page.evaluate(async () => {
    const call = async (url, method, body) => {
      const response = await fetch(`/api/v1${url}`, { method, headers: { 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
      if (!response.ok) throw new Error(await response.text())
      return response.json()
    }
    await call('/auth/register', 'POST', { username: `mksame${crypto.randomUUID().slice(0, 8)}`, password: 'secret123' })
    const project = await call('/projects', 'POST', { title: '重复词标记隔离测试', genre: '科幻', premise: '测试', style: '' })
    const chapter = await call(`/projects/${project.id}/chapters`, 'POST', { title: '正文', content: '甲甲\r\n🙂乙' })
    const scene = (await call(`/projects/${project.id}/chapters/${chapter.id}/scenes`, 'GET')).items[0]
    const endpoint = `/projects/${project.id}/chapters/${chapter.id}/scenes/${scene.id}/document`
    const document = await call(endpoint, 'GET'); const key = crypto.randomUUID()
    document.document.markers.push({ key, section_key: null, status: 'active', start: 1, end: 2, color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null })
    await call(endpoint, 'PUT', { expected_content_revision: document.content_revision, document: document.document, add_section_keys: [], delete_section_keys: [], add_marker_keys: [key], delete_marker_keys: [], mapped_section_keys: [], mapped_marker_keys: [] })
    return { pid: project.id, cid: chapter.id, sid: scene.id }
  })
  const endpoint = `/api/v1/projects/${target.pid}/chapters/${target.cid}/scenes/${target.sid}/document`
  await page.goto(`http://127.0.0.1:8766/projects/${target.pid}/chapters/${target.cid}?scene_id=${target.sid}`)
  const editor = page.getByRole('textbox', { name: '当前场景正文' })
  await editor.waitFor({ timeout: 20000 })
  await editor.evaluate((root) => {
    const text = root.querySelector('.rich-marker').firstChild
    window.getSelection().setBaseAndExtent(text, 0, text, text.length)
  })
  await page.waitForTimeout(100)
  await page.keyboard.insertText('甲')
  await page.waitForTimeout(100)
  const responsePromise = page.waitForResponse((r) => r.url().endsWith('/document') && r.request().method() === 'PUT')
  await page.getByRole('button', { name: /保存/ }).first().click()
  const response = await responsePromise
  const saved = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  const marker = saved.document.markers[0]
  if (response.status() !== 200 || saved.document.content !== '甲甲\r\n🙂乙' || marker.status !== 'stale'
      || marker.excerpt !== '甲' || marker.last_start !== 1 || marker.draft_excerpt !== null) {
    throw new Error(`相同字面替换错误 ${JSON.stringify({ status: response.status(), saved })}`)
  }
  return { result: 'PASS', marker }
}
