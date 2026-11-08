// 专用 SQLite / FakeLLM 产品 UI：仅使用本任务随机测试用户和合成哨兵。
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
    const project = await (await api('/api/v1/projects', { method: 'POST', headers: json,
      body: JSON.stringify({ title: '区块 AI 隔离作品', genre: '科幻', premise: '仅用于端到端测试', style: '' }) })).json()
    const base = `/api/v1/projects/${project.id}`
    const chapter = await (await api(`${base}/chapters`, { method: 'POST', headers: json,
      body: JSON.stringify({ title: '区块单次例外', content: '正式正文甲乙丙' }) })).json()
    const chapterBase = `${base}/chapters/${chapter.id}`
    const scene = (await (await api(`${chapterBase}/scenes`)).json()).items[0]
    const endpoint = `${chapterBase}/scenes/${scene.id}/document`
    const data = await (await api(endpoint)).json()
    const keys = [crypto.randomUUID(), crypto.randomUUID(), crypto.randomUUID()]
    const marker = crypto.randomUUID()
    const empty = { version: 1, boundaries: [], inline: [], blocks: [] }
    data.document.sections = [
      { key: keys[0], anchor: 1, order: 0, title: '关闭甲标题绝密', content: '关闭甲前缀选中🙂关闭甲后缀',
        include_in_ai: false, formatting: { ...empty, inline: [
          { kind: 'bold', start: 5, end: 8 }, { kind: 'italic', start: 0, end: 5 },
        ] } },
      { key: keys[1], anchor: 2, order: 0, title: '关闭乙标题绝密', content: '关闭乙正文绝密', include_in_ai: false, formatting: empty },
      { key: keys[2], anchor: 3, order: 0, title: '开启标题哨兵', content: '开启背景哨兵', include_in_ai: true, formatting: empty },
    ]
    data.document.markers = [{ key: marker, section_key: keys[0], status: 'active', color: 'purple', start: 5, end: 8 }]
    const saved = await (await api(endpoint, { method: 'PUT', headers: json, body: JSON.stringify({
      expected_content_revision: data.content_revision, document: data.document, add_section_keys: keys, delete_section_keys: [],
      add_marker_keys: [marker], delete_marker_keys: [], mapped_section_keys: [], mapped_marker_keys: [],
    }) })).json()
    await api('/api/__codex_probe/reset', { method: 'POST', headers: { 'x-codex-probe-token': token } })
    await api('/api/__codex_probe/responses', { method: 'POST', headers: { ...json, 'x-codex-probe-token': token },
      body: JSON.stringify({ items: ['改好🙂'] }) })
    return { projectId: project.id, chapterId: chapter.id, sceneId: scene.id, keys, marker, saved }
  })
  await page.goto(`http://127.0.0.1:5181/projects/${seeded.projectId}/chapters/${seeded.chapterId}?scene_id=${seeded.sceneId}`)
  const editor = page.getByRole('textbox', { name: '当前场景正文' })
  await editor.waitFor({ timeout: 30000 })
  // 先挂载普通改写面板，再打开区块预览；随后切换移动布局会卸载普通面板，不能解除区块锁。
  await page.getByRole('tab', { name: '局部改写' }).click()
  const section = editor.locator('.rich-author-section').first()
  const title = section.getByRole('textbox', { name: '区块名称' })
  if (await title.inputValue() !== '关闭甲标题绝密') throw new Error('目标区块未显示或顺序错误')
  if (await section.getByRole('checkbox', { name: '允许 AI 自动使用' }).isChecked()) throw new Error('目标区块默认开关错误')
  const selected = section.locator('strong').first()
  await selected.waitFor()
  await selected.evaluate((element) => {
    const range = document.createRange()
    range.selectNodeContents(element)
    const selection = window.getSelection()
    selection.removeAllRanges()
    selection.addRange(range)
  })
  await section.getByRole('button', { name: '选区 AI 编辑' }).click()
  const dialog = page.getByRole('dialog', { name: '区块选区 AI 编辑' })
  await dialog.getByText('本区块已关闭 AI 自动使用', { exact: false }).waitFor()
  await page.waitForFunction(() => document.querySelector('[aria-label="当前场景正文"]')?.getAttribute('contenteditable') === 'false')
  if (await page.getByRole('tab', { name: '局部改写' }).getAttribute('aria-disabled') !== 'true') throw new Error('区块预览期间普通改写标签仍可用')
  await page.setViewportSize({ width: 375, height: 812 })
  await page.waitForTimeout(100)
  if (await editor.getAttribute('contenteditable') !== 'false') throw new Error('普通改写面板卸载误解除了区块预览锁')
  await dialog.getByRole('button', { name: '继续并确认发送' }).click()
  let confirm = page.getByRole('dialog', { name: '确认本次发送' })
  await confirm.getByText('选中🙂').waitFor()
  await confirm.getByRole('button', { name: /取\s*消/ }).click()
  const callsBefore = await page.evaluate(async () => {
    const token = sessionStorage.getItem('codex-probe-token')
    return (await (await fetch('/api/__codex_probe/calls', { headers: { 'x-codex-probe-token': token } })).json()).calls
  })
  if (callsBefore.length) throw new Error('取消单次例外后仍调用了模型')
  await dialog.getByRole('button', { name: '继续并确认发送' }).click()
  confirm = page.getByRole('dialog', { name: '确认本次发送' })
  await confirm.getByRole('button', { name: '确认并发送' }).click()
  await dialog.getByText('与原选区对比').waitFor({ timeout: 30000 })
  const calls = await page.evaluate(async () => {
    const token = sessionStorage.getItem('codex-probe-token')
    return (await (await fetch('/api/__codex_probe/calls', { headers: { 'x-codex-probe-token': token } })).json()).calls
  })
  const messages = JSON.stringify(calls)
  if (calls.length !== 1 || !messages.includes('选中🙂') || !messages.includes('开启背景哨兵')
      || messages.includes('关闭甲前缀') || messages.includes('关闭甲后缀') || messages.includes('关闭甲标题绝密')
      || messages.includes('关闭乙标题绝密') || messages.includes('关闭乙正文绝密')) throw new Error('模型上下文违反区块单次例外范围')
  await dialog.getByRole('button', { name: '采纳选区' }).click()
  await dialog.getByText('替换及其接缝处无法可靠保留的格式可能丢失', { exact: false }).waitFor()
  const getDocument = async () => page.evaluate(async (s) => (await fetch(`/api/v1/projects/${s.projectId}/chapters/${s.chapterId}/scenes/${s.sceneId}/document`)).json(), seeded)
  if (JSON.stringify(await getDocument()) !== JSON.stringify(seeded.saved)) throw new Error('格式确认前已修改原稿')
  await dialog.getByRole('button', { name: '核对格式并采纳' }).click()
  await page.getByRole('dialog', { name: '确认格式变化' }).getByRole('button', { name: /取\s*消/ }).click()
  if (JSON.stringify(await getDocument()) !== JSON.stringify(seeded.saved)) throw new Error('取消格式确认仍修改了原稿')
  await dialog.getByRole('button', { name: '核对格式并采纳' }).click()
  const applied = page.waitForResponse((response) => response.url().includes('/sections/')
    && response.url().endsWith('/rewrite/apply') && response.request().method() === 'POST' && response.status() === 200)
  await page.getByRole('dialog', { name: '确认格式变化' }).getByRole('button', { name: '确认格式变化并采纳' }).click()
  await applied
  const after = await getDocument()
  const first = after.document.sections.find((item) => item.key === seeded.keys[0])
  const marker = after.document.markers.find((item) => item.key === seeded.marker)
  if (after.document.content !== '正式正文甲乙丙' || first.content !== '关闭甲前缀改好🙂关闭甲后缀' || first.include_in_ai
      || marker.status !== 'stale' || marker.excerpt !== '选中🙂') throw new Error(`区块采纳/标记不匹配：${JSON.stringify(after.document)}`)
  if (JSON.stringify(first.formatting.inline) !== JSON.stringify([{ kind: 'italic', start: 0, end: 5 }])) throw new Error('未改动的区块样式未保留')
  await page.reload()
  await editor.waitFor()
  if (!await editor.getByText('关闭甲前缀改好🙂关闭甲后缀', { exact: false }).count()) throw new Error('重载后区块内容丢失')
  const external = await page.evaluate(async (s) => {
    const base = `/api/v1/projects/${s.projectId}`
    const reader = await (await fetch(`${base}/reader/chapters/${s.chapterId}`)).text()
    const txt = await (await fetch(`${base}/export?format=txt`)).text()
    const md = await (await fetch(`${base}/export?format=md`)).text()
    const backup = await (await fetch(`${base}/backup?versions=1&snapshots=1`)).json()
    const backupText = JSON.stringify(backup)
    if ([reader, txt, md].some((value) => value.includes('关闭甲前缀') || value.includes('开启背景哨兵'))) throw new Error('阅读或普通导出泄漏作者区块')
    if (!backupText.includes('关闭甲前缀改好🙂关闭甲后缀') || !backupText.includes('关闭乙正文绝密')) throw new Error('完整备份缺失区块')
    const data = new FormData()
    data.append('file', new Blob([JSON.stringify(backup)], { type: 'application/json' }), 'backup.ainovel.json')
    const preview = await fetch('/api/v1/projects/import/preview', { method: 'POST', body: data })
    if (!preview.ok) throw new Error(`备份预览失败 ${preview.status} ${await preview.text()}`)
    const token = (await preview.json()).token
    const imported = await fetch('/api/v1/projects/import', { method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ token, title: '隔离备份恢复' }) })
    if (!imported.ok) throw new Error(`备份导入失败 ${imported.status} ${await imported.text()}`)
    const target = (await imported.json()).project
    const chapters = await (await fetch(`/api/v1/projects/${target.id}/chapters`)).json()
    const chapter = chapters.find((item) => item.title === '区块单次例外')
    const scenes = await (await fetch(`/api/v1/projects/${target.id}/chapters/${chapter.id}/scenes`)).json()
    const restored = await (await fetch(`/api/v1/projects/${target.id}/chapters/${chapter.id}/scenes/${scenes.items[0].id}/document`)).json()
    return { projectId: target.id, restored }
  }, seeded)
  const restored = external.restored.document
  if (!restored.sections.some((item) => item.key === seeded.keys[0] && item.content === '关闭甲前缀改好🙂关闭甲后缀' && !item.include_in_ai)
      || !restored.markers.some((item) => item.key === seeded.marker && item.status === 'stale' && item.excerpt === '选中🙂')) throw new Error('备份恢复的区块或标记身份丢失')
  const epubBefore = page.waitForEvent('download')
  await page.evaluate((pid) => {
    const link = document.createElement('a')
    link.href = `/api/v1/projects/${pid}/export?format=epub`
    link.download = 'before.epub'
    document.body.append(link)
    link.click()
    link.remove()
  }, seeded.projectId)
  await (await epubBefore).saveAs('/tmp/ainovel-e2e-browser.nUQQsd/before-convert.epub')
  const targetSection = editor.locator('.rich-author-section').first()
  await targetSection.getByRole('button', { name: '转为正文' }).click()
  const conversion = page.getByRole('dialog', { name: '将“关闭甲标题绝密”转为正文？' })
  const converted = page.waitForResponse((response) => response.url().includes('/document/sections/')
    && response.url().endsWith('/convert') && response.request().method() === 'POST' && response.status() === 200)
  await conversion.getByRole('button', { name: '保存并转换' }).click()
  await converted
  const publicResult = await page.evaluate(async (s) => {
    const base = `/api/v1/projects/${s.projectId}`
    const document = await (await fetch(`${base}/chapters/${s.chapterId}/scenes/${s.sceneId}/document`)).json()
    const chapter = await (await fetch(`${base}/chapters/${s.chapterId}`)).json()
    const reader = await (await fetch(`${base}/reader/chapters/${s.chapterId}`)).text()
    const txt = await (await fetch(`${base}/export?format=txt`)).text()
    const md = await (await fetch(`${base}/export?format=md`)).text()
    return { document, chapter, reader, txt, md }
  }, seeded)
  const publicText = '正关闭甲前缀改好🙂关闭甲后缀式正文甲乙丙'
  if (publicResult.document.document.content !== publicText || publicResult.chapter.content !== publicText
      || publicResult.chapter.word_count !== Array.from(publicText).length
      || publicResult.document.document.sections.some((item) => item.key === seeded.keys[0])) throw new Error('转换后的正文、字数或区块身份错误')
  if (!publicResult.reader.includes('关闭甲前缀改好🙂关闭甲后缀') || !publicResult.txt.includes('关闭甲前缀改好🙂关闭甲后缀')
      || !publicResult.md.includes('<em>关闭甲前缀</em>') || publicResult.md.includes('关闭乙正文绝密')) throw new Error('转换后的阅读或导出不符')
  const epubAfter = page.waitForEvent('download')
  await page.evaluate((pid) => {
    const link = document.createElement('a')
    link.href = `/api/v1/projects/${pid}/export?format=epub`
    link.download = 'after.epub'
    document.body.append(link)
    link.click()
    link.remove()
  }, seeded.projectId)
  await (await epubAfter).saveAs('/tmp/ainovel-e2e-browser.nUQQsd/after-convert.epub')
  const size = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }))
  if (size.width !== 375 || size.scroll !== 375) throw new Error(`移动端溢出：${JSON.stringify(size)}`)
  return { status: 'PASS', target: { projectId: seeded.projectId, chapterId: seeded.chapterId, sceneId: seeded.sceneId },
    importedProjectId: external.projectId, checked: '关闭区块选区确认/取消 → 实际 FakeLLM 出站隔离 → 格式确认 → 标记失效 → 阅读/导出隔离 → 备份恢复 → 转正文及样式' }
}
