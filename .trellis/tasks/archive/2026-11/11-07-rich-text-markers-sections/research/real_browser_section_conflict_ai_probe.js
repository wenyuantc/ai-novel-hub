// 实际双页与真实服务：失效选区和登出后的关闭块确认都不得发送模型。
async (page) => {
  await page.setViewportSize({ width: 375, height: 812 })
  const seeded = await page.evaluate(async () => {
    const token = sessionStorage.getItem('codex-probe-token')
    const json = { 'content-type': 'application/json' }
    const api = async (path, init = {}) => {
      const response = await fetch(path, init)
      if (!response.ok) throw new Error(`${path}: ${response.status} ${await response.text()}`)
      return response
    }
    const username = (await (await api('/api/v1/auth/me')).json()).username
    const project = await (await api('/api/v1/projects', { method: 'POST', headers: json,
      body: JSON.stringify({ title: '双页 AI 竞争', genre: '科幻', premise: '合成测试', style: '' }) })).json()
    const base = `/api/v1/projects/${project.id}`
    const chapter = await (await api(`${base}/chapters`, { method: 'POST', headers: json,
      body: JSON.stringify({ title: '过期确认', content: '正式正文' }) })).json()
    const chapterBase = `${base}/chapters/${chapter.id}`
    const scene = (await (await api(`${chapterBase}/scenes`)).json()).items[0]
    const endpoint = `${chapterBase}/scenes/${scene.id}/document`
    const data = await (await api(endpoint)).json()
    const key = crypto.randomUUID()
    data.document.sections = [{ key, anchor: 1, order: 0, title: '原区块', content: '甲选中🙂尾', include_in_ai: false,
      formatting: { version: 1, boundaries: [], inline: [], blocks: [] } }]
    const saved = await (await api(endpoint, { method: 'PUT', headers: json, body: JSON.stringify({
      expected_content_revision: data.content_revision, document: data.document, add_section_keys: [key], delete_section_keys: [],
      add_marker_keys: [], delete_marker_keys: [], mapped_section_keys: [], mapped_marker_keys: [],
    }) })).json()
    await api('/api/__codex_probe/reset', { method: 'POST', headers: { 'x-codex-probe-token': token } })
    await api('/api/__codex_probe/responses', { method: 'POST', headers: { ...json, 'x-codex-probe-token': token },
      body: JSON.stringify({ items: ['不得生成'] }) })
    return { projectId: project.id, chapterId: chapter.id, sceneId: scene.id, saved, username }
  })
  const url = `http://127.0.0.1:5181/projects/${seeded.projectId}/chapters/${seeded.chapterId}?scene_id=${seeded.sceneId}`
  const other = await page.context().newPage()
  await other.setViewportSize({ width: 375, height: 812 })
  try {
    await page.goto(url)
    await other.goto(url)
    const editor = page.getByRole('textbox', { name: '当前场景正文' })
    const otherEditor = other.getByRole('textbox', { name: '当前场景正文' })
    await editor.waitFor({ timeout: 30000 })
    await otherEditor.waitFor({ timeout: 30000 })
    const selectAndConfirm = async () => {
      const section = editor.locator('.rich-author-section').first()
      await section.locator('.rich-author-section__content p').first().evaluate((element) => {
        const node = document.createTreeWalker(element, NodeFilter.SHOW_TEXT).nextNode()
        const value = node.textContent
        const offset = value.indexOf('选中🙂')
        if (offset < 0) throw new Error('找不到预期选区')
        const range = document.createRange()
        range.setStart(node, offset)
        range.setEnd(node, offset + '选中🙂'.length)
        const selected = window.getSelection()
        selected.removeAllRanges()
        selected.addRange(range)
      })
      await section.getByRole('button', { name: '选区 AI 编辑' }).click()
      await page.getByRole('dialog', { name: '区块选区 AI 编辑' }).getByRole('button', { name: '继续并确认发送' }).click()
      await page.getByRole('dialog', { name: '确认本次发送' }).getByText('选中🙂').waitFor()
    }
    const calls = async () => page.evaluate(async () => {
      const token = sessionStorage.getItem('codex-probe-token')
      return (await (await fetch('/api/__codex_probe/calls', { headers: { 'x-codex-probe-token': token } })).json()).calls
    })
    await selectAndConfirm()
    await otherEditor.locator('.rich-author-section').first().getByRole('textbox', { name: '区块名称' }).fill('第二页的新标题')
    const otherSave = other.waitForResponse((response) => response.url().endsWith('/document')
      && response.request().method() === 'PUT' && response.status() === 200)
    await other.getByRole('button', { name: /保\s*存/ }).first().click()
    await otherSave
    const stale = page.waitForResponse((response) => response.url().includes('/sections/')
      && response.url().endsWith('/rewrite') && response.request().method() === 'POST' && response.status() === 409)
    await page.getByRole('dialog', { name: '确认本次发送' }).getByRole('button', { name: '确认并发送' }).click()
    await stale
    if ((await calls()).length) throw new Error('过期选区仍发送了模型')
    await page.getByRole('dialog', { name: '区块选区 AI 编辑' }).getByRole('button', { name: '放弃并关闭' }).click()
    await page.waitForFunction(() => document.querySelector('[aria-label="当前场景正文"]')?.getAttribute('contenteditable') === 'true')
    await page.reload()
    await editor.waitFor({ timeout: 30000 })
    await selectAndConfirm()
    const logout = await other.evaluate(async () => (await fetch('/api/v1/auth/logout', { method: 'POST' })).status)
    if (logout !== 204) throw new Error(`测试登出失败 ${logout}`)
    const denied = page.waitForResponse((response) => response.url().includes('/sections/')
      && response.url().endsWith('/rewrite') && response.request().method() === 'POST' && response.status() === 401)
    await page.getByRole('dialog', { name: '确认本次发送' }).getByRole('button', { name: '确认并发送' }).click()
    await denied
    if ((await calls()).length) throw new Error('登出后仍发送了模型')
    return { status: 'PASS', checked: '另一页改区块后过期确认 409，登出后确认 401，均无 FakeLLM 调用' }
  } finally {
    await other.close()
    await page.evaluate(async (username) => {
      const me = await fetch('/api/v1/auth/me')
      if (me.ok) return
      const login = await fetch('/api/v1/auth/login', { method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ username, password: 'secret123' }) })
      if (!login.ok) throw new Error('隔离测试账户恢复登录失败')
    }, seeded.username)
  }
}
