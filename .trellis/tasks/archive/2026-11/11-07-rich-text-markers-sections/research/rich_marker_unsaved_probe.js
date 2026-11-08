// 验证两次未保存事务：先在标记中插入，再删除标记的完整选区。
async (page) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('http://127.0.0.1:8766/')
  const target = await page.evaluate(async () => {
    const call = async (url, method, body) => {
      const response = await fetch(`/api/v1${url}`, { method, headers: { 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
      if (!response.ok) throw new Error(await response.text())
      return response.json()
    }
    await call('/auth/register', 'POST', { username: `mkdraft${crypto.randomUUID().slice(0, 8)}`, password: 'secret123' })
    const project = await call('/projects', 'POST', { title: '标记未保存隔离测试', genre: '科幻', premise: '测试', style: '' })
    const chapter = await call(`/projects/${project.id}/chapters`, 'POST', { title: '正文', content: '甲\r\n🙂乙' })
    const scene = (await call(`/projects/${project.id}/chapters/${chapter.id}/scenes`, 'GET')).items[0]
    const endpoint = `/projects/${project.id}/chapters/${chapter.id}/scenes/${scene.id}/document`
    const document = await call(endpoint, 'GET'); const key = crypto.randomUUID()
    document.document.markers.push({ key, section_key: null, status: 'active', start: 0, end: 4, color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null })
    await call(endpoint, 'PUT', { expected_content_revision: document.content_revision, document: document.document, add_section_keys: [], delete_section_keys: [], add_marker_keys: [key], delete_marker_keys: [], mapped_section_keys: [], mapped_marker_keys: [] })
    return { pid: project.id, cid: chapter.id, sid: scene.id, key }
  })
  const endpoint = `/api/v1/projects/${target.pid}/chapters/${target.cid}/scenes/${target.sid}/document`
  await page.goto(`http://127.0.0.1:8766/projects/${target.pid}/chapters/${target.cid}?scene_id=${target.sid}`)
  const editor = page.getByRole('textbox', { name: '当前场景正文' })
  await editor.waitFor({ timeout: 20000 })
  await editor.evaluate((root) => {
    const first = root.querySelectorAll('p')[0].querySelector('.rich-marker').firstChild
    window.getSelection().setBaseAndExtent(first, 1, first, 1)
  })
  await page.keyboard.type('X')
  await page.waitForTimeout(700) // 分开历史组，验证撤销删除并保存后再重做。
  await editor.evaluate((root) => {
    const first = root.querySelectorAll('p')[0].querySelector('.rich-marker').firstChild
    const second = root.querySelectorAll('p')[1].querySelector('.rich-marker').firstChild
    window.getSelection().setBaseAndExtent(first, 0, second, 2)
  })
  await page.waitForTimeout(100) // 等待浏览器将真实 DOM 选区同步给 ProseMirror。
  await page.keyboard.press('Backspace')
  await page.waitForTimeout(100)
  if (await editor.textContent() !== '乙') throw new Error(`删除未生效: ${JSON.stringify(await editor.evaluate((el) => ({html: el.innerHTML, sel: String(window.getSelection()), focus: document.activeElement?.outerHTML.slice(0, 200)})))}`)
  const responsePromise = page.waitForResponse((r) => r.url().endsWith('/document') && r.request().method() === 'PUT')
  await page.getByRole('button', { name: /保存/ }).first().click()
  const response = await responsePromise
  let saved = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  const first = saved.document.markers[0]
  if (response.status() !== 200 || saved.document.content !== '乙' || first.excerpt !== '甲\r\n🙂'
      || first.draft_excerpt !== '甲X\r\n🙂' || first.draft_last_start !== 0) {
    throw new Error(`双片段保存失败: ${JSON.stringify({ status: response.status(), saved })}`)
  }
  await page.getByRole('button', { name: '全书标记' }).click()
  const list = page.getByRole('list', { name: '全书正文标记' })
  await list.getByText('草稿末态片段：由作者未保存事务捕获，服务端无法独立验证。').waitFor({ timeout: 5000 })
  if (!(await list.getByText(/甲X/).count())) throw new Error('时间线未展示草稿片段')
  await list.getByText('查看原有片段').click()
  if (!(await list.getByText(/甲\s*🙂/).count())) throw new Error('时间线未保留原有片段')
  await page.getByRole('dialog', { name: '全书标记' }).getByRole('button', { name: 'Close' }).click()
  await page.getByRole('button', { name: /撤\s*销/ }).click()
  await page.getByRole('button', { name: /保存/ }).first().click()
  await page.waitForTimeout(300)
  saved = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  if (saved.document.content !== '甲X\r\n🙂乙' || saved.document.markers[0].status !== 'active') {
    throw new Error(`撤销并保存未恢复正文和活动标记: ${JSON.stringify(saved)}`)
  }
  await page.getByRole('button', { name: /重\s*做/ }).click()
  const redoResponse = page.waitForResponse((r) => r.url().endsWith('/document') && r.request().method() === 'PUT')
  await page.getByRole('button', { name: /保存/ }).first().click()
  const redo = await redoResponse
  saved = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  if (redo.status() !== 200 || saved.document.content !== '乙' || saved.document.markers[0].status !== 'stale'
      || saved.document.markers[0].excerpt !== '甲X\r\n🙂' || saved.document.markers[0].draft_excerpt !== '甲X\r\n🙂') {
    throw new Error(`跨保存重做未按新基线计算: ${JSON.stringify({ status: redo.status(), saved })}`)
  }
  return { result: 'PASS', first, redo: saved.document.markers[0] }
}
