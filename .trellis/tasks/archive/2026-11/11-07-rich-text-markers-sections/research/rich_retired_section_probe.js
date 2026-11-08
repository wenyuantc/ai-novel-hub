// 只有退场区块身份时，编辑正文不得清空墓碑，也不应永久只读。
async (page) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('http://127.0.0.1:8766/')
  const target = await page.evaluate(async () => {
    const call = async (url, method, body) => {
      const response = await fetch(`/api/v1${url}`, { method, headers: { 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
      if (!response.ok) throw new Error(await response.text())
      return response.json()
    }
    await call('/auth/register', 'POST', { username: `retired${crypto.randomUUID().slice(0, 8)}`, password: 'secret123' })
    const project = await call('/projects', 'POST', { title: '退场区块隔离测试', genre: '科幻', premise: '测试', style: '' })
    const chapter = await call(`/projects/${project.id}/chapters`, 'POST', { title: '正文', content: '甲\r\n🙂乙' })
    const scene = (await call(`/projects/${project.id}/chapters/${chapter.id}/scenes`, 'GET')).items[0]
    const endpoint = `/projects/${project.id}/chapters/${chapter.id}/scenes/${scene.id}/document`
    const original = await call(endpoint, 'GET'); const key = crypto.randomUUID()
    const initial = { ...original.document, sections: [{ key, anchor: 1, order: 0, title: '备注', content: '备选', formatting: { version: 1, boundaries: [], inline: [], blocks: [] }, include_in_ai: false }] }
    const created = await call(endpoint, 'PUT', { expected_content_revision: original.content_revision, document: initial, add_section_keys: [key], delete_section_keys: [], add_marker_keys: [], delete_marker_keys: [], mapped_section_keys: [], mapped_marker_keys: [] })
    const removed = { ...created.document, sections: [], retired_section_keys: [key] }
    await call(endpoint, 'PUT', { expected_content_revision: created.content_revision, document: removed, add_section_keys: [], delete_section_keys: [key], add_marker_keys: [], delete_marker_keys: [], mapped_section_keys: [], mapped_marker_keys: [] })
    return { pid: project.id, cid: chapter.id, sid: scene.id, key }
  })
  const endpoint = `/api/v1/projects/${target.pid}/chapters/${target.cid}/scenes/${target.sid}/document`
  await page.goto(`http://127.0.0.1:8766/projects/${target.pid}/chapters/${target.cid}?scene_id=${target.sid}`)
  const editor = page.getByRole('textbox', { name: '当前场景正文' })
  await editor.waitFor({ timeout: 20000 })
  if (await editor.getAttribute('contenteditable') !== 'true') throw new Error('只有墓碑的正文被误设为只读')
  await editor.evaluate((root) => {
    const text = root.querySelectorAll('p')[1].firstChild
    window.getSelection().setBaseAndExtent(text, text.length, text, text.length)
  })
  await page.waitForTimeout(100)
  await page.keyboard.type('!')
  const responsePromise = page.waitForResponse((r) => r.url().endsWith('/document') && r.request().method() === 'PUT')
  await page.getByRole('button', { name: /保存/ }).first().click()
  const response = await responsePromise
  const saved = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  if (response.status() !== 200 || saved.document.content !== '甲\r\n🙂乙!' || JSON.stringify(saved.document.retired_section_keys) !== JSON.stringify([target.key])) throw new Error(`退场区块身份丢失 ${JSON.stringify(saved)}`)
  return { result: 'PASS', content: saved.document.content, retired: saved.document.retired_section_keys }
}
