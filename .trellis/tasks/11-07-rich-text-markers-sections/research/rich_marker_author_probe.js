// 隔离数据库 + 真实浏览器：正文选区创建、撤销重做、改色改范围、保存及重载。
async (page) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('http://127.0.0.1:8766/')
  const target = await page.evaluate(async () => {
    const call = async (url, method, body) => {
      const response = await fetch(`/api/v1${url}`, { method, headers: { 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
      if (!response.ok) throw new Error(await response.text())
      return response.json()
    }
    await call('/auth/register', 'POST', { username: `mkauthor${crypto.randomUUID().slice(0, 8)}`, password: 'secret123' })
    const project = await call('/projects', 'POST', { title: '标记交互隔离测试', genre: '科幻', premise: '测试', style: '' })
    const chapter = await call(`/projects/${project.id}/chapters`, 'POST', { title: '正文', content: '甲\r\n🙂乙\n尾' })
    const scene = (await call(`/projects/${project.id}/chapters/${chapter.id}/scenes`, 'GET')).items[0]
    return { pid: project.id, cid: chapter.id, sid: scene.id }
  })
  const endpoint = `/api/v1/projects/${target.pid}/chapters/${target.cid}/scenes/${target.sid}/document`
  await page.goto(`http://127.0.0.1:8766/projects/${target.pid}/chapters/${target.cid}?scene_id=${target.sid}`)
  const editor = page.getByRole('textbox', { name: '当前场景正文' })
  await editor.waitFor({ timeout: 20000 })
  await editor.evaluate((root) => {
    const first = root.querySelectorAll('p')[0].firstChild
    const second = root.querySelectorAll('p')[1].firstChild
    window.getSelection().setBaseAndExtent(first, 0, second, 2)
  })
  await page.getByRole('button', { name: '标记选区' }).click()
  await page.getByLabel('标记颜色').first().click()
  await page.getByTitle('蓝色').click()
  await page.getByRole('dialog').getByRole('button', { name: /确\s*定/ }).click()
  if (!(await editor.locator('.rich-marker-blue').count())) throw new Error('未渲染蓝色标记')
  await page.getByRole('button', { name: /撤\s*销/ }).click()
  if (await editor.locator('.rich-marker').count()) throw new Error('撤销未移除新增标记')
  await page.getByRole('button', { name: /重\s*做/ }).click()
  if (!(await editor.locator('.rich-marker-blue').count())) throw new Error('重做未恢复新增标记')
  await page.getByRole('button', { name: /保存/ }).first().click()
  await page.waitForTimeout(500)
  let stored = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  if (stored.document.content !== '甲\r\n🙂乙\n尾' || stored.document.markers.length !== 1 || stored.document.markers[0].start !== 0 || stored.document.markers[0].end !== 4 || stored.document.markers[0].color !== 'blue') throw new Error(`创建标记错误 ${JSON.stringify(stored)}`)
  const key = stored.document.markers[0].key
  await editor.evaluate((root) => {
    const paragraph = root.querySelectorAll('p')[1]
    const last = paragraph.lastChild
    window.getSelection().setBaseAndExtent(last, 0, last, 1)
  })
  await page.getByRole('button', { name: '标记选区' }).click()
  await page.getByLabel('标记目标').first().click()
  await page.locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option').filter({ hasText: '蓝色' }).click()
  await page.getByLabel('标记颜色').first().click()
  await page.getByTitle('红色').click()
  await page.getByRole('dialog').getByRole('button', { name: /确\s*定/ }).click()
  await page.getByRole('button', { name: /保存/ }).first().click()
  await page.waitForTimeout(500)
  stored = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  if (stored.document.markers.length !== 1 || stored.document.markers[0].key !== key || stored.document.markers[0].start !== 4 || stored.document.markers[0].end !== 5 || stored.document.markers[0].color !== 'red') throw new Error(`调整标记错误 ${JSON.stringify(stored)}`)
  await page.reload()
  await editor.waitFor({ timeout: 20000 })
  if (!(await editor.locator('.rich-marker-red').count())) throw new Error('重载后标记不见')
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)
  if (overflow) throw new Error('375px 页面水平溢出')
  return { result: 'PASS', target, marker: stored.document.markers[0] }
}
