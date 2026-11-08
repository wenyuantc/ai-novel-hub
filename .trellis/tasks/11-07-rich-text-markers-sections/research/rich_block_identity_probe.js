// 真实服务+浏览器：相邻列表范围的原身份不能因为编辑器逐行表示而丢失。
async (page) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('http://127.0.0.1:8766/')
  const targets = await page.evaluate(async () => {
    const call = async (url, method, body) => {
      const response = await fetch(`/api/v1${url}`, { method, headers: { 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
      if (!response.ok) throw new Error(await response.text())
      return response.json()
    }
    await call('/auth/register', 'POST', { username: `blockid${crypto.randomUUID().slice(0, 8)}`, password: 'secret123' })
    const project = await call('/projects', 'POST', { title: '块范围隔离测试', genre: '科幻', premise: '测试', style: '' })
    const result = []
    for (const split of [false, true]) {
      const chapter = await call(`/projects/${project.id}/chapters`, 'POST', { title: split ? '两个范围' : '单个范围', content: '甲\r\n乙\n丙' })
      const scene = (await call(`/projects/${project.id}/chapters/${chapter.id}/scenes`, 'GET')).items[0]
      const endpoint = `/projects/${project.id}/chapters/${chapter.id}/scenes/${scene.id}/document`
      const document = await call(endpoint, 'GET')
      document.document.formatting.blocks = split ? [{ kind: 'bullet_list', start: 0, end: 1, level: null }, { kind: 'bullet_list', start: 3, end: 6, level: null }] : [{ kind: 'bullet_list', start: 0, end: 6, level: null }]
      await call(endpoint, 'PUT', { expected_content_revision: document.content_revision, document: document.document, add_section_keys: [], delete_section_keys: [], add_marker_keys: [], delete_marker_keys: [], mapped_section_keys: [], mapped_marker_keys: [] })
      result.push({ pid: project.id, cid: chapter.id, sid: scene.id, split })
    }
    return result
  })
  const results = []
  for (const target of targets) {
    const endpoint = `/api/v1/projects/${target.pid}/chapters/${target.cid}/scenes/${target.sid}/document`
    await page.goto(`http://127.0.0.1:8766/projects/${target.pid}/chapters/${target.cid}?scene_id=${target.sid}`)
    const editor = page.getByRole('textbox', { name: '当前场景正文' })
    await editor.waitFor({ timeout: 20000 })
    if (await editor.getAttribute('contenteditable') !== 'true') throw new Error('合法列表文档被误判为只读')
    await editor.evaluate((root) => {
      const text = root.querySelectorAll('li p')[2].firstChild
      window.getSelection().setBaseAndExtent(text, text.length, text, text.length)
    })
    await page.waitForTimeout(100)
    await page.keyboard.type('!')
    const responsePromise = page.waitForResponse((r) => r.url().endsWith('/document') && r.request().method() === 'PUT')
    await page.getByRole('button', { name: /保存/ }).first().click()
    const response = await responsePromise
    const saved = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
    const expected = target.split ? [{ kind: 'bullet_list', start: 0, end: 1, level: null }, { kind: 'bullet_list', start: 3, end: 7, level: null }] : [{ kind: 'bullet_list', start: 0, end: 7, level: null }]
    if (response.status() !== 200 || saved.document.content !== '甲\r\n乙\n丙!' || JSON.stringify(saved.document.formatting.blocks) !== JSON.stringify(expected)) throw new Error(`块范围变形 ${JSON.stringify({status: response.status(), saved})}`)
    results.push(saved.document.formatting.blocks)
  }
  return { result: 'PASS', results }
}
