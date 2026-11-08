// 真实产品页 + 临时 SQLite / FakeLLM，验证整场景候选暂停及人工核对。
async (page) => {
  await page.setViewportSize({ width: 1280, height: 900 })
  const seeded = await page.evaluate(async () => {
    const token = sessionStorage.getItem('codex-probe-token')
    const json = { 'content-type': 'application/json' }
    const api = async (path, init = {}) => {
      const response = await fetch(path, init)
      if (!response.ok) throw new Error(`${path}: ${response.status} ${await response.text()}`)
      return response
    }
    const project = await (await api('/api/v1/projects', { method: 'POST', headers: json,
      body: JSON.stringify({ title: '候选核对测试', genre: '科幻', premise: '合成测试', style: '' }) })).json()
    const base = `/api/v1/projects/${project.id}`
    const chapter = await (await api(`${base}/chapters`, { method: 'POST', headers: json,
      body: JSON.stringify({ title: '候选核对', content: '原稿🙂甲乙' }) })).json()
    const chapterBase = `${base}/chapters/${chapter.id}`
    const scene = (await (await api(`${chapterBase}/scenes`)).json()).items[0]
    const endpoint = `${chapterBase}/scenes/${scene.id}/document`
    const data = await (await api(endpoint)).json()
    const key = crypto.randomUUID()
    data.document.formatting.inline = [{ kind: 'bold', start: 1, end: 4 }]
    data.document.sections = [{ key, anchor: 2, order: 0, title: '待定位作者备注', content: '绝密作者区块',
      include_in_ai: false, formatting: { version: 1, boundaries: [], inline: [], blocks: [] } }]
    const saved = await (await api(endpoint, { method: 'PUT', headers: json, body: JSON.stringify({
      expected_content_revision: data.content_revision, document: data.document, add_section_keys: [key], delete_section_keys: [],
      add_marker_keys: [], delete_marker_keys: [], mapped_section_keys: [], mapped_marker_keys: [],
    }) })).json()
    await api('/api/__codex_probe/reset', { method: 'POST', headers: { 'x-codex-probe-token': token } })
    await api('/api/__codex_probe/responses', { method: 'POST', headers: { ...json, 'x-codex-probe-token': token },
      body: JSON.stringify({ items: ['完全不同的候选稿'] }) })
    return { projectId: project.id, chapterId: chapter.id, sceneId: scene.id, key, saved }
  })
  await page.goto(`http://127.0.0.1:5181/projects/${seeded.projectId}/chapters/${seeded.chapterId}?scene_id=${seeded.sceneId}`)
  const editor = page.getByRole('textbox', { name: '当前场景正文' })
  await editor.waitFor({ timeout: 30000 })
  await page.getByRole('tab', { name: 'AI 润色' }).click()
  const created = page.waitForResponse((response) => response.url().endsWith('/polish')
    && response.request().method() === 'POST' && response.status() === 202)
  await page.getByRole('button', { name: '润色当前场景' }).click()
  const taskId = (await (await created).json()).id
  let task
  for (let attempt = 0; attempt < 100; attempt++) {
    task = await page.evaluate(async (id) => (await fetch(`/api/v1/tasks/${id}`)).json(), taskId)
    if (task.status === 'paused') break
    if (['failed', 'cancelled', 'interrupted'].includes(task.status)) throw new Error(`候选任务异常：${JSON.stringify(task)}`)
    await page.waitForTimeout(200)
  }
  if (task.status !== 'paused' || task.rich_review?.phase !== 'awaiting_review') throw new Error(`任务未等待人工核对：${JSON.stringify(task)}`)
  const documentUrl = `/api/v1/projects/${seeded.projectId}/chapters/${seeded.chapterId}/scenes/${seeded.sceneId}/document`
  const before = await page.evaluate(async (url) => (await fetch(url)).json(), documentUrl)
  if (JSON.stringify(before) !== JSON.stringify(seeded.saved)) throw new Error('待核对时原章已被修改')
  const calls = await page.evaluate(async () => {
    const token = sessionStorage.getItem('codex-probe-token')
    return (await (await fetch('/api/__codex_probe/calls', { headers: { 'x-codex-probe-token': token } })).json()).calls
  })
  if (calls.length !== 1 || JSON.stringify(calls).includes('绝密作者区块') || JSON.stringify(calls).includes('待定位作者备注')) throw new Error('整场景模型上下文泄漏关闭区块')
  await page.getByRole('button', { name: /后台任务/ }).click()
  const drawer = page.getByRole('dialog', { name: '后台任务' })
  const reviewButton = drawer.getByRole('button', { name: '核对候选' }).first()
  await reviewButton.waitFor({ timeout: 30000 })
  await reviewButton.click()
  let dialog = page.getByRole('dialog', { name: '核对 AI 候选稿' })
  await dialog.getByText('完全不同的候选稿', { exact: false }).first().waitFor()
  if (!await dialog.getByRole('button', { name: '确认并采纳整章' }).isDisabled()) throw new Error('未核对位置就允许采纳')
  await dialog.getByRole('button', { name: '稍后核对' }).click()
  if (JSON.stringify(await page.evaluate(async (url) => (await fetch(url)).json(), documentUrl)) !== JSON.stringify(before)) throw new Error('稍后核对修改了原稿')
  await reviewButton.click()
  dialog = page.getByRole('dialog', { name: '核对 AI 候选稿' })
  await dialog.getByText('完全不同的候选稿', { exact: false }).first().waitFor()
  await dialog.getByRole('spinbutton', { name: '待定位作者备注的新位置' }).fill('3')
  await dialog.getByRole('checkbox', { name: '我已核对候选，确认接受所提示的格式损失' }).check()
  const accepted = page.waitForResponse((response) => response.url().includes('/rich-proposals/')
    && response.url().endsWith('/accept') && response.request().method() === 'POST' && response.status() === 200)
  await dialog.getByRole('button', { name: '确认并采纳整章' }).click()
  await accepted
  const after = await page.evaluate(async (url) => (await fetch(url)).json(), documentUrl)
  if (after.document.content !== '完全不同的候选稿' || after.document.sections[0].key !== seeded.key
      || after.document.sections[0].anchor !== 3 || after.document.sections[0].content !== '绝密作者区块') throw new Error(`候选采纳不符：${JSON.stringify(after)}`)
  const finalCalls = await page.evaluate(async () => {
    const token = sessionStorage.getItem('codex-probe-token')
    return (await (await fetch('/api/__codex_probe/calls', { headers: { 'x-codex-probe-token': token } })).json()).calls.length
  })
  if (finalCalls !== 1) throw new Error('确认核对重复调用了模型')
  return { status: 'PASS', taskId, checked: '真实场景生成暂停 → 稍后核对不写 → 显式锚点和格式确认 → 原子采纳且模型一次' }
}
