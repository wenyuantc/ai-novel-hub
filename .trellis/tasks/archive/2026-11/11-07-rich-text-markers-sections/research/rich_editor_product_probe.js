// 隔离真实服务/数据库；只验证当前受支持正文子集，非区块/标记完整验收。
async (page) => {
  await page.goto('http://127.0.0.1:8766/')
  const seeded = await page.evaluate(async () => {
    const register = await fetch('/api/v1/auth/register', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ username: `edit${crypto.randomUUID().slice(0, 8)}`, password: 'secret123' }) })
    if (!register.ok) throw new Error(await register.text())
    const created = await fetch('/api/v1/projects', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ title: '隔离正文测试', genre: '科幻', premise: '测试', style: '' }) })
    if (!created.ok) throw new Error(await created.text())
    const pid = (await created.json()).id
    const response = await fetch(`/api/v1/projects/${pid}/chapters`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ title: '无损编辑测试', content: '甲\r\n🙂乙\n尾' }) })
    if (!response.ok) throw new Error(await response.text())
    const chapter = await response.json()
    const scenes = await (await fetch(`/api/v1/projects/${pid}/chapters/${chapter.id}/scenes`)).json()
    return { projectId: pid, chapter: chapter.id, scene: scenes.items[0].id }
  })
  const projectId = seeded.projectId
  await page.goto(`http://127.0.0.1:8766/projects/${projectId}/chapters/${seeded.chapter}?scene_id=${seeded.scene}`)
  const editor = page.getByRole('textbox', { name: '当前场景正文' })
  await editor.waitFor({ timeout: 20000 })
  if (await editor.getAttribute('contenteditable') !== 'true') throw new Error('未加载可编辑富正文')
  await editor.evaluate((root) => {
    const text = root.querySelectorAll('p')[1].firstChild
    window.getSelection().setBaseAndExtent(text, 2, text, 2)
  })
  await page.keyboard.type('X')
  await page.getByRole('button', { name: /保存/ }).first().click()
  await page.waitForTimeout(500)
  const saved = await page.evaluate(async ({ pid, chapter, scene }) => (await fetch(`/api/v1/projects/${pid}/chapters/${chapter}/scenes/${scene}/document`)).json(), { pid: projectId, ...seeded })
  if (saved.document.content !== '甲\r\n🙂X乙\n尾') throw new Error(`保存不匹配 ${JSON.stringify(saved.document.content)}`)
  await page.reload()
  await editor.waitFor()
  await editor.evaluate((root) => {
    const text = root.querySelectorAll('p')[1].firstChild
    window.getSelection().setBaseAndExtent(text, 0, text, 2)
  })
  await page.getByRole('button', { name: /粗\s*体/ }).click()
  await page.getByRole('button', { name: /保存/ }).first().click()
  await page.waitForTimeout(500)
  const bold = await page.evaluate(async ({ pid, chapter, scene }) => (await fetch(`/api/v1/projects/${pid}/chapters/${chapter}/scenes/${scene}/document`)).json(), { pid: projectId, ...seeded })
  if (bold.document.content !== saved.document.content || JSON.stringify(bold.document.formatting.inline) !== JSON.stringify([{ kind: 'bold', start: 3, end: 4 }])) throw new Error(`格式未完整保存 ${JSON.stringify(bold)}`)
  return { result: 'PASS', seeded, text: bold.document.content, inline: bold.document.formatting.inline }
}
