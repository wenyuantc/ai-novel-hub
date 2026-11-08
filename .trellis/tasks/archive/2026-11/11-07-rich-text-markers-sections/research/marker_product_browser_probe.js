// 隔离 SQLite + 实际应用与真实浏览器；不使用或修改用户作品。
async (page) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('http://127.0.0.1:8766/')
  const seeded = await page.evaluate(async () => {
    const call = async (path, method = 'GET', body) => {
      const response = await fetch(`/api/v1${path}`, { method, headers: { 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
      if (!response.ok) throw new Error(`${path} ${response.status} ${await response.text()}`)
      return response.json()
    }
    await call('/auth/register', 'POST', { username: `mark${crypto.randomUUID().slice(0, 8)}`, password: 'secret123' })
    const project = await call('/projects', 'POST', { title: '隔离标记测试', genre: '科幻', premise: '测试', style: '' })
    const first = await call(`/projects/${project.id}/chapters`, 'POST', { title: '第一章', content: '甲\r\n🙂乙' })
    const second = await call(`/projects/${project.id}/chapters`, 'POST', { title: '第二章', content: '他章🙂文字' })
    const scenes = []
    for (const chapter of [first, second]) {
      const scene = (await call(`/projects/${project.id}/chapters/${chapter.id}/scenes`)).items[0]
      const url = `/projects/${project.id}/chapters/${chapter.id}/scenes/${scene.id}/document`
      const doc = await call(url)
      const key = chapter.id === first.id ? '4fa11111-1111-4111-8111-111111111111' : '4fa22222-2222-4222-8222-222222222222'
      doc.document.markers.push(chapter.id === first.id
        ? { key, section_key: null, status: 'stale', start: null, end: null, color: 'blue', excerpt: '旧片段', excerpt_truncated: false, last_start: 0 }
        : { key, section_key: null, status: 'active', start: 2, end: 3, color: 'green', excerpt: '', excerpt_truncated: false, last_start: null })
      await call(url, 'PUT', { expected_content_revision: doc.content_revision, document: doc.document, add_section_keys: [], delete_section_keys: [], add_marker_keys: [key], delete_marker_keys: [], mapped_section_keys: [], mapped_marker_keys: [] })
      scenes.push(scene.id)
    }
    return { projectId: project.id, chapters: [first.id, second.id], scenes }
  })
  await page.goto(`http://127.0.0.1:8766/projects/${seeded.projectId}/chapters/${seeded.chapters[0]}?scene_id=${seeded.scenes[0]}`)
  await page.getByRole('button', { name: '全书标记' }).waitFor({ timeout: 20000 })
  let input = page.getByRole('textbox', { name: '当前场景正文' })
  await input.evaluate((el) => { el.focus(); el.setSelectionRange(2, 4); el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true })) })
  await page.getByRole('button', { name: '全书标记' }).click()
  await page.getByRole('button', { name: /重新关联第 1 章/ }).click()
  await page.getByText('标记已重新关联至所选正文').waitFor({ timeout: 10000 })
  const linked = await page.evaluate(async (pid) => (await fetch(`/api/v1/projects/${pid}/markers`)).json(), seeded.projectId)
  if (linked.items[0].start !== 3 || linked.items[0].end !== 4 || linked.items[0].excerpt !== '🙂') throw new Error('CRLF / emoji 选区映射错误')
  await page.getByRole('button', { name: /定位第 2 章/ }).click()
  await page.waitForURL(new RegExp(`/chapters/${seeded.chapters[1]}`))
  input = page.getByRole('textbox', { name: '当前场景正文' })
  await input.waitFor()
  await page.waitForTimeout(750)
  const across = await input.evaluate((el) => ({ start: el.selectionStart, end: el.selectionEnd, focused: document.activeElement === el }))
  if (across.start !== 2 || across.end !== 4 || !across.focused) throw new Error('跨章定位未正确聚焦')
  await page.getByRole('button', { name: '全书标记' }).click()
  const deletion = page.getByRole('button', { name: /删除第 2 章/ })
  await deletion.click()
  const popup = page.getByRole('tooltip').filter({ hasText: '删除这条标记' })
  await popup.getByRole('button', { name: /取\s*消/ }).click()
  const before = await page.evaluate(async (pid) => (await fetch(`/api/v1/projects/${pid}/markers`)).json(), seeded.projectId)
  if (before.total !== 2) throw new Error('取消删除仍发生写入')
  await deletion.click()
  await popup.getByRole('button', { name: /删\s*除/ }).click()
  await page.getByText('标记已删除，正文未改变').waitFor({ timeout: 10000 })
  const after = await page.evaluate(async (pid) => (await fetch(`/api/v1/projects/${pid}/markers`)).json(), seeded.projectId)
  if (after.total !== 1) throw new Error('确认删除后目录错误')
  await page.getByRole('button', { name: /定位第 1 章/ }).click()
  await page.waitForURL(new RegExp(`/chapters/${seeded.chapters[0]}`))
  input = page.getByRole('textbox', { name: '当前场景正文' })
  await input.waitFor()
  await page.getByRole('button', { name: '全书标记' }).click()
  await page.getByRole('button', { name: /定位第 1 章/ }).click()
  await page.waitForTimeout(750)
  const same = await input.evaluate((el) => ({ start: el.selectionStart, end: el.selectionEnd, focused: document.activeElement === el }))
  if (same.start !== 2 || same.end !== 4 || !same.focused) throw new Error('同章定位未正确聚焦')
  return { result: 'PASS', seeded, linked: [linked.items[0].start, linked.items[0].end], across, same, deletion: [before.total, after.total] }
}
