// 实际服务、隔离数据库和 Chromium；覆盖无区块的跨 CRLF 标记。
async (page) => {
  await page.goto('http://127.0.0.1:8766/')
  const target = await page.evaluate(async () => {
    const call = async (url, method, body) => {
      const response = await fetch(`/api/v1${url}`, { method, headers: { 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
      if (!response.ok) throw new Error(await response.text())
      return response.json()
    }
    await call('/auth/register', 'POST', { username: `marked${crypto.randomUUID().slice(0, 8)}`, password: 'secret123' })
    const project = await call('/projects', 'POST', { title: '隔离标记正文', genre: '科幻', premise: '测试', style: '' })
    const chapter = await call(`/projects/${project.id}/chapters`, 'POST', { title: '正文', content: '甲\r\n🙂乙\n尾' })
    const scene = (await call(`/projects/${project.id}/chapters/${chapter.id}/scenes`, 'GET')).items[0]
    const endpoint = `/projects/${project.id}/chapters/${chapter.id}/scenes/${scene.id}/document`
    const document = await call(endpoint, 'GET')
    const key = '12345678-1234-4123-8123-123456789def'
    document.document.markers.push({ key, section_key: null, status: 'active', start: 0, end: 4, color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null })
    await call(endpoint, 'PUT', { expected_content_revision: document.content_revision, document: document.document,
      add_section_keys: [], delete_section_keys: [], add_marker_keys: [key], delete_marker_keys: [], mapped_section_keys: [], mapped_marker_keys: [] })
    return { pid: project.id, cid: chapter.id, sid: scene.id }
  })
  await page.goto(`http://127.0.0.1:8766/projects/${target.pid}/chapters/${target.cid}?scene_id=${target.sid}`)
  const editor = page.getByRole('textbox', { name: '当前场景正文' })
  await editor.waitFor({ timeout: 20000 })
  if (await editor.getAttribute('contenteditable') !== 'true') throw new Error('标记文档未进入安全编辑器')
  if (!(await editor.locator('.rich-marker').count())) throw new Error('正文没有可感知标记')
  await editor.evaluate((root) => {
    const first = root.querySelectorAll('p')[0].querySelector('.rich-marker').firstChild
    const second = root.querySelectorAll('p')[1].querySelector('.rich-marker').firstChild
    window.getSelection().setBaseAndExtent(first, 0, second, 2)
  })
  await page.keyboard.press('Backspace')
  await page.getByRole('button', { name: /撤\s*销/ }).click()
  if (!(await editor.textContent()).includes('甲🙂乙')) throw new Error('撤销正文失败')
  if (!(await editor.locator('.rich-marker').count())) throw new Error('撤销未恢复标记')
  await page.getByRole('button', { name: /重\s*做/ }).click()
  if ((await editor.textContent()).includes('甲')) throw new Error('重做正文失败')
  await page.getByRole('button', { name: /保存/ }).first().click()
  await page.waitForTimeout(500)
  const stored = await page.evaluate(async ({ pid, cid, sid }) => (await fetch(`/api/v1/projects/${pid}/chapters/${cid}/scenes/${sid}/document`)).json(), target)
  if (stored.document.content !== '乙\n尾' || stored.document.markers[0].status !== 'stale' || stored.document.markers[0].excerpt !== '甲\r\n🙂') throw new Error(`标记或正文保存错误 ${JSON.stringify(stored)}`)
  return { result: 'PASS', target, text: stored.document.content, marker: stored.document.markers[0] }
}
