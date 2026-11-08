// 合成账号、专用 SQLite 和 FakeLLM；从真实编辑操作开始贯通完整作者工作流。
async (page) => {
  await page.setViewportSize({ width: 375, height: 812 })
  const seeded = await page.evaluate(async () => {
    const token = sessionStorage.getItem('codex-probe-token')
    const json = { 'content-type': 'application/json' }
    const api = async (path, init = {}) => {
      const response = await fetch(path, init)
      if (!response.ok) throw new Error(`${path}: ${response.status} ${await response.text()}`)
      return response
    }
    const project = await (await api('/api/v1/projects', { method: 'POST', headers: json,
      body: JSON.stringify({ title: '完整作者流程', genre: '科幻', premise: '合成测试', style: '' }) })).json()
    const base = `/api/v1/projects/${project.id}`
    const chapter = await (await api(`${base}/chapters`, { method: 'POST', headers: json,
      body: JSON.stringify({ title: '完整流程', content: '甲\r\n🙂乙' }) })).json()
    const scene = (await (await api(`${base}/chapters/${chapter.id}/scenes`)).json()).items[0]
    await api('/api/__codex_probe/reset', { method: 'POST', headers: { 'x-codex-probe-token': token } })
    await api('/api/__codex_probe/responses', { method: 'POST', headers: { ...json, 'x-codex-probe-token': token },
      body: JSON.stringify({ items: ['改好🙂'] }) })
    return { projectId: project.id, chapterId: chapter.id, sceneId: scene.id }
  })
  const endpoint = `/api/v1/projects/${seeded.projectId}/chapters/${seeded.chapterId}/scenes/${seeded.sceneId}/document`
  await page.goto(`http://127.0.0.1:5181/projects/${seeded.projectId}/chapters/${seeded.chapterId}?scene_id=${seeded.sceneId}`)
  const editor = page.getByRole('textbox', { name: '当前场景正文' })
  await editor.waitFor({ timeout: 30000 })
  // 正文标记 emoji；选区是 UTF-16，服务端坐标是 Unicode 码点。
  await editor.locator('p').nth(1).evaluate((element) => {
    const text = document.createTreeWalker(element, NodeFilter.SHOW_TEXT).nextNode()
    window.getSelection().setBaseAndExtent(text, 0, text, '🙂'.length)
  })
  await page.getByRole('button', { name: '标记选区' }).click()
  await page.getByRole('dialog', { name: '标记正文选区' }).getByRole('button', { name: /确\s*定/ }).click()
  // 在首段末尾插入区块；旧 CRLF 和其右侧正文不得改变。
  await editor.locator('p').first().evaluate((element) => {
    const text = document.createTreeWalker(element, NodeFilter.SHOW_TEXT).nextNode()
    window.getSelection().setBaseAndExtent(text, text.textContent.length, text, text.textContent.length)
  })
  await page.getByRole('button', { name: '插入区块' }).click()
  const section = editor.locator('.rich-author-section').first()
  await section.getByRole('textbox', { name: '区块名称' }).fill('单次块私密标题')
  if (await section.getByRole('checkbox', { name: '允许 AI 自动使用' }).isChecked()) throw new Error('新区块默认开启 AI')
  await section.locator('.rich-author-section__content p').first().click()
  await page.keyboard.insertText('隐藏前选中🙂隐藏后')
  // 在区块内保留前缀斜体，并对选区设置粗体及作者标记。
  const select = async (text) => section.locator('.rich-author-section__content p').first().evaluate((element, wanted) => {
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT)
    let node
    while ((node = walker.nextNode())) {
      const offset = node.textContent.indexOf(wanted)
      if (offset >= 0) {
        window.getSelection().setBaseAndExtent(node, offset, node, offset + wanted.length)
        return
      }
    }
    throw new Error(`无法选择 ${wanted}`)
  }, text)
  await select('隐藏前')
  await page.getByRole('button', { name: /斜\s*体/ }).click()
  await select('选中🙂')
  await page.getByRole('button', { name: /粗\s*体/ }).click()
  await page.getByRole('button', { name: '标记选区' }).click()
  await page.getByRole('dialog', { name: '标记区块选区' }).getByRole('button', { name: /确\s*定/ }).click()
  const save = page.waitForResponse((response) => response.url().endsWith('/document')
    && response.request().method() === 'PUT' && response.status() === 200)
  await page.getByRole('button', { name: /保\s*存/ }).first().click()
  await save
  const saved = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  if (saved.document.content !== '甲\r\n🙂乙' || saved.document.sections[0].content !== '隐藏前选中🙂隐藏后'
      || saved.document.sections[0].include_in_ai || saved.document.markers.length !== 2) throw new Error(`保存后的完整文档不符：${JSON.stringify(saved)}`)
  const key = saved.document.sections[0].key
  const blockMarker = saved.document.markers.find((item) => item.section_key === key).key
  const proseMarker = saved.document.markers.find((item) => item.section_key === null).key
  // 折叠后从时间线定位区块标记，应自动展开并精确选中。
  await section.getByRole('button', { name: '折叠区块' }).click()
  await page.getByRole('button', { name: '全书标记' }).click()
  const list = page.getByRole('list', { name: '全书正文标记' })
  await list.getByRole('listitem').filter({ hasText: '区块：单次块私密标题' }).getByRole('button', { name: /定位/ }).click()
  await page.waitForTimeout(400)
  if (!await section.locator('.rich-author-section__content').isVisible() || await page.evaluate(() => String(window.getSelection())) !== '选中🙂') throw new Error('时间线没有展开并定位区块标记')
  await section.getByRole('button', { name: '选区 AI 编辑' }).click()
  const dialog = page.getByRole('dialog', { name: '区块选区 AI 编辑' })
  await dialog.getByRole('button', { name: '继续并确认发送' }).click()
  await page.getByRole('dialog', { name: '确认本次发送' }).getByRole('button', { name: '确认并发送' }).click()
  await dialog.getByText('与原选区对比').waitFor({ timeout: 30000 })
  const calls = await page.evaluate(async () => {
    const token = sessionStorage.getItem('codex-probe-token')
    return (await (await fetch('/api/__codex_probe/calls', { headers: { 'x-codex-probe-token': token } })).json()).calls
  })
  const payload = JSON.stringify(calls)
  if (calls.length !== 1 || !payload.includes('选中🙂') || payload.includes('隐藏前') || payload.includes('隐藏后')
      || payload.includes('单次块私密标题')) throw new Error('单次选区模型出站范围错误')
  await dialog.getByRole('button', { name: '采纳选区' }).click()
  await dialog.getByText('替换及其接缝处无法可靠保留的格式可能丢失', { exact: false }).waitFor()
  await dialog.getByRole('button', { name: '核对格式并采纳' }).click()
  const applied = page.waitForResponse((response) => response.url().endsWith('/rewrite/apply')
    && response.request().method() === 'POST' && response.status() === 200)
  await page.getByRole('dialog', { name: '确认格式变化' }).getByRole('button', { name: '确认格式变化并采纳' }).click()
  await applied
  const rewritten = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  const stale = rewritten.document.markers.find((item) => item.key === blockMarker)
  if (rewritten.document.sections[0].content !== '隐藏前改好🙂隐藏后' || stale.status !== 'stale' || stale.excerpt !== '选中🙂') throw new Error('选区采纳或失效标记不符')
  await page.reload()
  await editor.waitFor()
  const backup = await page.evaluate(async (s) => {
    const base = `/api/v1/projects/${s.projectId}`
    const reader = await (await fetch(`${base}/reader/chapters/${s.chapterId}`)).text()
    const txt = await (await fetch(`${base}/export?format=txt`)).text()
    const md = await (await fetch(`${base}/export?format=md`)).text()
    if ([reader, txt, md].some((value) => value.includes('隐藏前'))) throw new Error('作者区块出现在阅读或常规导出')
    const content = await (await fetch(`${base}/backup?versions=1&snapshots=1`)).json()
    const form = new FormData()
    form.append('file', new Blob([JSON.stringify(content)], { type: 'application/json' }), 'backup.ainovel.json')
    const preview = await fetch('/api/v1/projects/import/preview', { method: 'POST', body: form })
    if (!preview.ok) throw new Error('备份预览失败')
    const imported = await fetch('/api/v1/projects/import', { method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ token: (await preview.json()).token, title: '完整流程恢复' }) })
    if (!imported.ok) throw new Error(`备份恢复失败 ${await imported.text()}`)
    const projectId = (await imported.json()).project.id
    const chapters = await (await fetch(`/api/v1/projects/${projectId}/chapters`)).json()
    const chapter = chapters.find((item) => item.title === '完整流程')
    const scenes = await (await fetch(`/api/v1/projects/${projectId}/chapters/${chapter.id}/scenes`)).json()
    return (await (await fetch(`/api/v1/projects/${projectId}/chapters/${chapter.id}/scenes/${scenes.items[0].id}/document`)).json()).document
  }, seeded)
  if (!backup.sections.some((item) => item.key === key && item.content === '隐藏前改好🙂隐藏后')
      || !backup.markers.some((item) => item.key === blockMarker && item.status === 'stale' && item.excerpt === '选中🙂')
      || !backup.markers.some((item) => item.key === proseMarker && item.status === 'active')) throw new Error('备份恢复丢失区块或标记身份')
  const source = editor.locator('.rich-author-section').first()
  await source.getByRole('button', { name: '转为正文' }).click()
  const convert = page.waitForResponse((response) => response.url().endsWith('/convert')
    && response.request().method() === 'POST' && response.status() === 200)
  await page.getByRole('dialog', { name: /转为正文/ }).getByRole('button', { name: '保存并转换' }).click()
  await convert
  const publicDoc = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  if (publicDoc.document.content !== '甲隐藏前改好🙂隐藏后\r\n🙂乙'
      || !publicDoc.document.formatting.inline.some((style) => style.kind === 'italic' && style.start === 1 && style.end === 4)
      || !publicDoc.document.markers.some((item) => item.key === proseMarker && item.status === 'active')) throw new Error('转换正文未保留文字、斜体或原正文标记')
  const size = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }))
  if (size.width !== 375 || size.scroll !== 375) throw new Error(`移动端溢出：${JSON.stringify(size)}`)
  return { status: 'PASS', target: seeded, checked: '编辑→正文/区块标记→保存→时间线定位→关闭块 AI→格式确认→失效记录→阅读导出→备份恢复→转正文' }
}
