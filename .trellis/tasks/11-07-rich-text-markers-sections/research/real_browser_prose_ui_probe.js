// 假模型与专用 SQLite 的真实产品 UI 探针；由 real_browser_prose_api_smoke.js 创建隔离用户和作品后执行。
async (page) => {
  await page.setViewportSize({ width: 1280, height: 900 })
  const seeded = await page.evaluate(async () => {
    const token = sessionStorage.getItem('codex-probe-token')
    if (!token) throw new Error('探针令牌未就绪')
    const json = { 'content-type': 'application/json' }
    const api = async (path, init = {}) => {
      const response = await fetch(path, init)
      if (!response.ok) throw new Error(`${path}: ${response.status} ${await response.text()}`)
      return response
    }
    const projects = await (await api('/api/v1/projects')).json()
    const project = Array.isArray(projects) ? projects[0] : projects.items[0]
    const base = `/api/v1/projects/${project.id}`
    const chapter = await (await api(`${base}/chapters`, { method: 'POST', headers: json,
      body: JSON.stringify({ title: '真实编辑器格式确认', content: '前甲🙂尾' }) })).json()
    const chapterBase = `${base}/chapters/${chapter.id}`
    const scene = (await (await api(`${chapterBase}/scenes`)).json()).items[0]
    const endpoint = `${chapterBase}/scenes/${scene.id}/document`
    const data = await (await api(endpoint)).json()
    data.document.formatting.inline = [{ kind: 'bold', start: 1, end: 3 }]
    const marker = crypto.randomUUID()
    data.document.markers = [{ key: marker, section_key: null, status: 'active', color: 'blue', start: 1, end: 3 }]
    const saved = await (await api(endpoint, { method: 'PUT', headers: json, body: JSON.stringify({
      expected_content_revision: data.content_revision, document: data.document, add_section_keys: [], delete_section_keys: [],
      add_marker_keys: [marker], delete_marker_keys: [], mapped_section_keys: [], mapped_marker_keys: [],
    }) })).json()
    await api('/api/__codex_probe/reset', { method: 'POST', headers: { 'x-codex-probe-token': token } })
    await api('/api/__codex_probe/responses', { method: 'POST', headers: { ...json, 'x-codex-probe-token': token },
      body: JSON.stringify({ items: ['改好🙂'] }) })
    return { projectId: project.id, chapterId: chapter.id, sceneId: scene.id, saved }
  })
  await page.goto(`http://127.0.0.1:5181/projects/${seeded.projectId}/chapters/${seeded.chapterId}?scene_id=${seeded.sceneId}`)
  const editor = page.getByRole('textbox', { name: '当前场景正文' })
  await editor.waitFor({ timeout: 30000 })
  const strong = editor.locator('strong').first()
  await strong.waitFor()
  await strong.evaluate((element) => {
    const range = document.createRange()
    range.selectNodeContents(element)
    const selection = window.getSelection()
    selection.removeAllRanges()
    selection.addRange(range)
  })
  await page.getByRole('tab', { name: '局部改写' }).click()
  await page.getByText('已选中 2 字', { exact: false }).waitFor()
  await page.getByRole('button', { name: '改写选中内容' }).click()
  await page.getByText('与原文对比').waitFor({ timeout: 30000 })
  await page.getByRole('button', { name: /采\s*纳/ }).click()
  await page.getByText('替换及其接缝处无法可靠保留的格式可能丢失', { exact: false }).waitFor()
  const before = await page.evaluate(async (s) => (await fetch(`/api/v1/projects/${s.projectId}/chapters/${s.chapterId}/scenes/${s.sceneId}/document`)).json(), seeded)
  if (JSON.stringify(before) !== JSON.stringify(seeded.saved)) throw new Error('确认前已修改正文')
  await page.getByRole('button', { name: '核对格式并采纳' }).click()
  const confirmation = page.getByRole('dialog', { name: '确认格式变化' })
  await confirmation.getByRole('button', { name: /取\s*消/ }).click()
  const cancelled = await page.evaluate(async (s) => (await fetch(`/api/v1/projects/${s.projectId}/chapters/${s.chapterId}/scenes/${s.sceneId}/document`)).json(), seeded)
  if (JSON.stringify(cancelled) !== JSON.stringify(seeded.saved)) throw new Error('取消确认后正文发生变化')
  await page.getByRole('button', { name: '核对格式并采纳' }).click()
  const applied = page.waitForResponse((response) => response.url().includes('/rewrite/apply')
    && response.request().method() === 'POST' && response.status() === 200)
  await page.getByRole('dialog', { name: '确认格式变化' }).getByRole('button', { name: '确认格式变化并采纳' }).click()
  await applied
  const after = await page.evaluate(async (s) => {
    const document = await (await fetch(`/api/v1/projects/${s.projectId}/chapters/${s.chapterId}/scenes/${s.sceneId}/document`)).json()
    const token = sessionStorage.getItem('codex-probe-token')
    const calls = await (await fetch('/api/__codex_probe/calls', { headers: { 'x-codex-probe-token': token } })).json()
    return { document, calls }
  }, seeded)
  if (after.document.document.markers[0].status !== 'stale' || after.document.document.markers[0].excerpt !== '甲🙂') throw new Error(`标记失效记录不符：${JSON.stringify(after.document.document)}`)
  if (after.calls.calls.length !== 1 || !JSON.stringify(after.calls.calls[0].messages).includes('甲🙂')) throw new Error('模型出站与选区不匹配')
  await page.reload()
  await editor.waitFor()
  if (!(await editor.innerText()).includes('前改好🙂尾')) throw new Error('重开未显示采纳正文')
  await page.setViewportSize({ width: 375, height: 812 })
  const size = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }))
  if (size.width !== 375 || size.scroll !== 375) throw new Error(`移动端溢出：${JSON.stringify(size)}`)
  return { status: 'PASS', target: { projectId: seeded.projectId, chapterId: seeded.chapterId, sceneId: seeded.sceneId },
    checked: '真实编辑器 → FakeLLM → 格式确认/取消/采纳 → 标记失效 → 重载' }
}
