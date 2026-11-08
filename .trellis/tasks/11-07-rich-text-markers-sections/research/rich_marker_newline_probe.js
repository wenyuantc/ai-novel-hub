// 真实服务 + 浏览器：仅覆盖 CRLF 的合法标记必须可发现、定位和调整。
async (page) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('http://127.0.0.1:8766/')
  const target = await page.evaluate(async () => {
    const call = async (url, method, body) => {
      const response = await fetch(`/api/v1${url}`, { method, headers: { 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
      if (!response.ok) throw new Error(await response.text())
      return response.json()
    }
    await call('/auth/register', 'POST', { username: `mkline${crypto.randomUUID().slice(0, 8)}`, password: 'secret123' })
    const project = await call('/projects', 'POST', { title: '换行标记隔离测试', genre: '科幻', premise: '测试', style: '' })
    const chapter = await call(`/projects/${project.id}/chapters`, 'POST', { title: '正文', content: '甲\r\n🙂乙' })
    const scene = (await call(`/projects/${project.id}/chapters/${chapter.id}/scenes`, 'GET')).items[0]
    const endpoint = `/projects/${project.id}/chapters/${chapter.id}/scenes/${scene.id}/document`
    const document = await call(endpoint, 'GET'); const key = crypto.randomUUID()
    document.document.markers.push({ key, section_key: null, status: 'active', start: 1, end: 3, color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null })
    await call(endpoint, 'PUT', { expected_content_revision: document.content_revision, document: document.document, add_section_keys: [], delete_section_keys: [], add_marker_keys: [key], delete_marker_keys: [], mapped_section_keys: [], mapped_marker_keys: [] })
    return { pid: project.id, cid: chapter.id, sid: scene.id, key }
  })
  const endpoint = `/api/v1/projects/${target.pid}/chapters/${target.cid}/scenes/${target.sid}/document`
  await page.goto(`http://127.0.0.1:8766/projects/${target.pid}/chapters/${target.cid}?scene_id=${target.sid}`)
  const editor = page.getByRole('textbox', { name: '当前场景正文' })
  await editor.waitFor({ timeout: 20000 })
  await page.getByRole('button', { name: '仅换行标记（1）' }).click()
  await page.getByRole('button', { name: '定位蓝色换行标记 1–3' }).click()
  await page.getByRole('button', { name: '标记选区' }).click()
  await page.getByRole('dialog').getByText(/所选文字共 2 个字符/).waitFor({ timeout: 3000 })
  await page.getByLabel('标记目标').first().click()
  await page.locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option').filter({ hasText: '蓝色' }).click()
  await page.getByLabel('标记颜色').first().click()
  await page.getByTitle('红色').click()
  await page.getByRole('dialog').getByRole('button', { name: /确\s*定/ }).click()
  await page.getByRole('button', { name: /保存/ }).first().click()
  await page.waitForTimeout(500)
  const stored = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  const marker = stored.document.markers[0]
  if (stored.document.content !== '甲\r\n🙂乙' || marker.key !== target.key || marker.start !== 1 || marker.end !== 3 || marker.color !== 'red') throw new Error(`换行标记更改不正确 ${JSON.stringify(stored)}`)
  return { result: 'PASS', target, marker }
}
