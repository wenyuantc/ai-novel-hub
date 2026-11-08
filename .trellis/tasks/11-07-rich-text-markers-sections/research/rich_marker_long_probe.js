// 真实浏览器 + SQLite：5000 码点活动标记增长后失效，区分基线与截断的草稿。
async (page) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('http://127.0.0.1:8766/')
  const target = await page.evaluate(async () => {
    const call = async (url, method, body) => {
      const response = await fetch(`/api/v1${url}`, { method, headers: { 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
      if (!response.ok) throw new Error(await response.text())
      return response.json()
    }
    await call('/auth/register', 'POST', { username: `mklong${crypto.randomUUID().slice(0, 8)}`, password: 'secret123' })
    const project = await call('/projects', 'POST', { title: '长标记隔离测试', genre: '科幻', premise: '测试', style: '' })
    const chapter = await call(`/projects/${project.id}/chapters`, 'POST', { title: '正文', content: '🙂'.repeat(4998) + '\r\n尾' })
    const scene = (await call(`/projects/${project.id}/chapters/${chapter.id}/scenes`, 'GET')).items[0]
    const endpoint = `/projects/${project.id}/chapters/${chapter.id}/scenes/${scene.id}/document`
    const document = await call(endpoint, 'GET'); const key = crypto.randomUUID()
    document.document.markers.push({ key, section_key: null, status: 'active', start: 0, end: 5000, color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null })
    await call(endpoint, 'PUT', { expected_content_revision: document.content_revision, document: document.document,
      add_section_keys: [], delete_section_keys: [], add_marker_keys: [key], delete_marker_keys: [], mapped_section_keys: [], mapped_marker_keys: [] })
    return { pid: project.id, cid: chapter.id, sid: scene.id }
  })
  const endpoint = `/api/v1/projects/${target.pid}/chapters/${target.cid}/scenes/${target.sid}/document`
  await page.goto(`http://127.0.0.1:8766/projects/${target.pid}/chapters/${target.cid}?scene_id=${target.sid}`)
  const editor = page.getByRole('textbox', { name: '当前场景正文' })
  await editor.waitFor({ timeout: 20000 })
  await editor.evaluate((root) => {
    const text = root.querySelector('p .rich-marker').firstChild
    window.getSelection().setBaseAndExtent(text, text.length, text, text.length)
  })
  await page.keyboard.insertText('🙂')
  await page.waitForTimeout(100)
  const current = await editor.evaluate((root) => root.pmViewDesc.node.attrs.markers[0])
  if (current.status !== 'active' || current.end !== 5001) throw new Error(`长标记增长错误 ${JSON.stringify(current)}`)
  await editor.evaluate((root) => {
    const first = root.querySelectorAll('p')[0].querySelector('.rich-marker').firstChild
    const second = root.querySelectorAll('p')[1].firstChild
    window.getSelection().setBaseAndExtent(first, 0, second, 0)
  })
  await page.waitForTimeout(100)
  await page.keyboard.press('Backspace')
  await page.waitForTimeout(100)
  if (await editor.textContent() !== '尾') throw new Error('全删后正文不正确')
  const responsePromise = page.waitForResponse((r) => r.url().endsWith('/document') && r.request().method() === 'PUT')
  await page.getByRole('button', { name: /保存/ }).first().click()
  const response = await responsePromise
  const saved = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  const marker = saved.document.markers[0]
  if (response.status() !== 200 || saved.document.content !== '尾' || marker.status !== 'stale'
    || marker.excerpt !== '🙂'.repeat(4998) + '\r\n' || marker.excerpt_truncated
    || marker.draft_excerpt !== '🙂'.repeat(4999) || !marker.draft_excerpt_truncated || marker.draft_last_start !== 0) {
    throw new Error(`长标记截断或基线错误 status=${response.status()}`)
  }
  return { result: 'PASS', savedCodePoints: [...marker.excerpt].length, savedTruncated: marker.excerpt_truncated,
    draftCodePoints: [...marker.draft_excerpt].length, draftTruncated: marker.draft_excerpt_truncated }
}
