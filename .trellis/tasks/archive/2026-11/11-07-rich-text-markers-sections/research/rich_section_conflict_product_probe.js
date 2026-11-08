// 两个真实浏览器页共享一个测试账号，验证区块竞争不覆盖本地草稿。
async (page) => {
  await page.setViewportSize({ width: 375, height: 900 })
  await page.goto('http://127.0.0.1:8767/')
  const target = await page.evaluate(async () => {
    const call = async (url, method, body) => {
      const response = await fetch(`/api/v1${url}`, { method, headers: { 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
      if (!response.ok) throw new Error(await response.text())
      return response.json()
    }
    await call('/auth/register', 'POST', { username: `scon${crypto.randomUUID().slice(0, 8)}`, password: 'secret123' })
    const project = await call('/projects', 'POST', { title: '区块竞争隔离测试', genre: '科幻', premise: '测试', style: '' })
    const chapter = await call(`/projects/${project.id}/chapters`, 'POST', { title: '正文', content: '甲\r\n🙂乙' })
    const scene = (await call(`/projects/${project.id}/chapters/${chapter.id}/scenes`, 'GET')).items[0]
    const endpoint = `/projects/${project.id}/chapters/${chapter.id}/scenes/${scene.id}/document`
    const source = await call(endpoint, 'GET'); const key = crypto.randomUUID()
    source.document.sections.push({ key, anchor: 1, order: 0, title: '同一区块', content: '旧🙂稿',
      formatting: { version: 1, boundaries: [], inline: [], blocks: [] }, include_in_ai: false })
    await call(endpoint, 'PUT', { expected_content_revision: source.content_revision, document: source.document,
      add_section_keys: [key], delete_section_keys: [], add_marker_keys: [], delete_marker_keys: [], mapped_section_keys: [], mapped_marker_keys: [] })
    return { pid: project.id, cid: chapter.id, sid: scene.id }
  })
  const url = `http://127.0.0.1:8767/projects/${target.pid}/chapters/${target.cid}?scene_id=${target.sid}`
  const endpoint = `/api/v1/projects/${target.pid}/chapters/${target.cid}/scenes/${target.sid}/document`
  await page.goto(url)
  const other = await page.context().newPage(); await other.setViewportSize({ width: 375, height: 900 }); await other.goto(url)
  const section = (tab) => tab.getByRole('textbox', { name: '当前场景正文' }).locator('section.rich-author-section .rich-author-section__content')
  await section(page).waitFor({ timeout: 20000 }); await section(other).waitFor({ timeout: 20000 })
  await section(page).locator('p').first().click(); await page.keyboard.press('End'); await page.keyboard.insertText('甲')
  await section(other).locator('p').first().click(); await other.keyboard.press('End'); await other.keyboard.insertText('乙')
  const first = page.waitForResponse((response) => response.url().endsWith('/document') && response.request().method() === 'PUT')
  await page.getByRole('button', { name: /保存/ }).first().click()
  if ((await first).status() !== 200) throw new Error('第一页保存失败')
  const second = other.waitForResponse((response) => response.url().endsWith('/document') && response.request().method() === 'PUT')
  await other.getByRole('button', { name: /保存/ }).first().click()
  if ((await second).status() !== 409) throw new Error('过期区块草稿没有 CAS 保护')
  if (await section(other).innerText() !== '旧🙂稿乙') throw new Error('冲突覆盖了第二页草稿')
  const saved = await page.evaluate(async (path) => (await fetch(path)).json(), endpoint)
  if (saved.document.content !== '甲\r\n🙂乙' || saved.document.sections[0].content !== '旧🙂稿甲') throw new Error(`服务端被旧草稿覆盖 ${JSON.stringify(saved)}`)
  if (!(await other.getByText(/草稿|冲突|版本/).count())) throw new Error('冲突未提示核对')
  await other.close({ runBeforeUnload: false })
  return { result: 'PASS', saved, preservedDraft: '旧🙂稿乙' }
}
