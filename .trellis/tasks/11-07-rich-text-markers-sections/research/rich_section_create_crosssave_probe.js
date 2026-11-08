// 新建区块跨保存撤销/重做必须复活原 UUID，不能当作另一个新区块。
async (page) => {
  await page.setViewportSize({ width: 375, height: 900 })
  await page.goto('http://127.0.0.1:8767/')
  const target = await page.evaluate(async () => {
    const call = async (url, method, body) => {
      const response = await fetch(`/api/v1${url}`, { method, headers: { 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
      if (!response.ok) throw new Error(await response.text())
      return response.json()
    }
    await call('/auth/register', 'POST', { username: `sundo${crypto.randomUUID().slice(0, 8)}`, password: 'secret123' })
    const project = await call('/projects', 'POST', { title: '跨保存区块撤销测试', genre: '科幻', premise: '测试', style: '' })
    const chapter = await call(`/projects/${project.id}/chapters`, 'POST', { title: '正文', content: '甲\r\n🙂乙' })
    const scene = (await call(`/projects/${project.id}/chapters/${chapter.id}/scenes`, 'GET')).items[0]
    return { pid: project.id, cid: chapter.id, sid: scene.id }
  })
  const endpoint = `/api/v1/projects/${target.pid}/chapters/${target.cid}/scenes/${target.sid}/document`
  await page.goto(`http://127.0.0.1:8767/projects/${target.pid}/chapters/${target.cid}?scene_id=${target.sid}`)
  const editor = page.getByRole('textbox', { name: '当前场景正文' }); await editor.waitFor({ timeout: 20000 })
  await editor.evaluate((root) => { root.focus(); const text = root.querySelector('p').firstChild; window.getSelection().setBaseAndExtent(text, 1, text, 1) })
  await page.waitForTimeout(100)
  await page.getByRole('button', { name: '插入区块' }).click()
  let pending = page.waitForResponse((response) => response.url().endsWith('/document') && response.request().method() === 'PUT')
  await page.getByRole('button', { name: /保存/ }).first().click()
  let response = await pending
  let saved = await page.evaluate(async (path) => (await fetch(path)).json(), endpoint)
  if (response.status() !== 200 || saved.document.sections.length !== 1) throw new Error(`区块创建保存失败 ${JSON.stringify(saved)}`)
  const key = saved.document.sections[0].key
  await page.getByRole('button', { name: /撤\s*销/ }).click()
  pending = page.waitForResponse((result) => result.url().endsWith('/document') && result.request().method() === 'PUT')
  await page.getByRole('button', { name: /保存/ }).first().click()
  response = await pending
  saved = await page.evaluate(async (path) => (await fetch(path)).json(), endpoint)
  if (response.status() !== 200 || saved.document.sections.length || !saved.document.retired_section_keys.includes(key)) {
    throw new Error(`撤销创建没有保留墓碑 ${JSON.stringify(saved)}`)
  }
  await page.getByRole('button', { name: /重\s*做/ }).click()
  pending = page.waitForResponse((result) => result.url().endsWith('/document') && result.request().method() === 'PUT')
  await page.getByRole('button', { name: /保存/ }).first().click()
  response = await pending
  saved = await page.evaluate(async (path) => (await fetch(path)).json(), endpoint)
  if (response.status() !== 200 || saved.document.sections[0]?.key !== key || saved.document.retired_section_keys.includes(key)
    || saved.document.content !== '甲\r\n🙂乙') throw new Error(`重做没有复活原区块身份 ${JSON.stringify(saved)}`)
  return { result: 'PASS', saved }
}
