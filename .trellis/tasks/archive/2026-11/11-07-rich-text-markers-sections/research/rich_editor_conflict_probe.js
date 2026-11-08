// 真实服务、隔离 SQLite 和两页；不替代全部并发/输入法/富结构验收。
async (page) => {
  await page.goto('http://127.0.0.1:8766/')
  const target = await page.evaluate(async () => {
    const call = async (url, body) => {
      const response = await fetch(`/api/v1${url}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })
      if (!response.ok) throw new Error(await response.text())
      return response.json()
    }
    await call('/auth/register', { username: `race${crypto.randomUUID().slice(0, 8)}`, password: 'secret123' })
    const project = await call('/projects', { title: '隔离冲突测试', genre: '科幻', premise: '测试', style: '' })
    const chapter = await call(`/projects/${project.id}/chapters`, { title: '正文', content: '甲\r\n🙂乙\n尾' })
    const scenes = await (await fetch(`/api/v1/projects/${project.id}/chapters/${chapter.id}/scenes`)).json()
    return { pid: project.id, cid: chapter.id, sid: scenes.items[0].id }
  })
  const url = `http://127.0.0.1:8766/projects/${target.pid}/chapters/${target.cid}?scene_id=${target.sid}`
  const other = await page.context().newPage()
  try {
    await page.goto(url); await other.goto(url)
    const first = page.getByRole('textbox', { name: '当前场景正文' })
    const second = other.getByRole('textbox', { name: '当前场景正文' })
    await first.waitFor(); await second.waitFor()
    await second.evaluate((root) => {
      const text = root.querySelectorAll('p')[1].firstChild
      window.getSelection().setBaseAndExtent(text, 2, text, 2)
    })
    await other.keyboard.type('Z')
    await first.evaluate((root) => {
      const text = root.querySelectorAll('p')[1].firstChild
      window.getSelection().setBaseAndExtent(text, 0, text, 2)
    })
    await page.getByRole('button', { name: /粗\s*体/ }).click()
    await page.getByRole('button', { name: /保存/ }).first().click()
    await page.waitForTimeout(400)
    await other.getByRole('button', { name: /保存/ }).first().click()
    await other.getByText('场景已被其他操作修改，本地草稿已保留').waitFor({ timeout: 10000 })
    const local = await second.textContent()
    if (!local.includes('🙂Z乙')) throw new Error('冲突覆盖了本地草稿')
    const checkbox = other.getByLabel('已核对最新场景，保留本地输入继续')
    await checkbox.check()
    await other.getByRole('button', { name: '使用最新版本号，不覆盖本地输入' }).click()
    await other.getByText(/服务端完整文档结构已变化/).waitFor({ timeout: 5000 })
    const current = await other.evaluate(async ({ pid, cid, sid }) => (await fetch(`/api/v1/projects/${pid}/chapters/${cid}/scenes/${sid}/document`)).json(), target)
    if (current.document.content !== '甲\r\n🙂乙\n尾' || current.document.formatting.inline[0]?.kind !== 'bold') throw new Error('冲突覆盖了服务端结构')
    if (!(await second.textContent()).includes('🙂Z乙')) throw new Error('核对动作清空了本地草稿')
    return { result: 'PASS', target, local, remote: current.document }
  } finally { await other.close() }
}
