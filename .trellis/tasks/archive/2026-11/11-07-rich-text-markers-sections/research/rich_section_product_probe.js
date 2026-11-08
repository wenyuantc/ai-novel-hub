// 独立 SQLite + 实际 Chromium；不使用用户现有作品或模型请求。
async (page) => {
  await page.setViewportSize({ width: 375, height: 900 })
  await page.goto('http://127.0.0.1:8767/')
  const target = await page.evaluate(async () => {
    const call = async (url, method, body) => {
      const response = await fetch(`/api/v1${url}`, { method, headers: { 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
      if (!response.ok) throw new Error(await response.text())
      return response.json()
    }
    await call('/auth/register', 'POST', { username: `sect${crypto.randomUUID().slice(0, 8)}`, password: 'secret123' })
    const project = await call('/projects', 'POST', { title: '区块产品隔离测试', genre: '科幻', premise: '测试', style: '' })
    const chapter = await call(`/projects/${project.id}/chapters`, 'POST', { title: '正文', content: '甲\r\n🙂乙' })
    const scene = (await call(`/projects/${project.id}/chapters/${chapter.id}/scenes`, 'GET')).items[0]
    return { pid: project.id, cid: chapter.id, sid: scene.id }
  })
  const endpoint = `/api/v1/projects/${target.pid}/chapters/${target.cid}/scenes/${target.sid}/document`
  await page.goto(`http://127.0.0.1:8767/projects/${target.pid}/chapters/${target.cid}?scene_id=${target.sid}`)
  const editor = page.getByRole('textbox', { name: '当前场景正文' })
  await editor.waitFor({ timeout: 20000 })
  await editor.evaluate((root) => {
    root.focus(); const text = root.querySelector('p').firstChild
    window.getSelection().setBaseAndExtent(text, 1, text, 1)
  })
  await page.waitForTimeout(100)
  await page.getByRole('button', { name: '插入区块' }).click()
  let sections = editor.locator('section.rich-author-section')
  if (await sections.count() !== 1) throw new Error('未能插入首个区块')
  const first = sections.nth(0)
  if (await first.getByRole('checkbox', { name: '允许 AI 自动使用' }).isChecked()) throw new Error('新区块未默认关闭 AI')
  await first.getByRole('textbox', { name: '区块名称' }).fill('备选一')
  await first.locator('.rich-author-section__content p').first().click()
  await page.keyboard.insertText('备注🙂')
  await first.getByRole('checkbox', { name: '允许 AI 自动使用' }).check()
  await first.locator('.rich-author-section__content').evaluate((root) => {
    const text = root.querySelector('p').firstChild
    window.getSelection().setBaseAndExtent(text, 0, text, text.length)
  })
  await page.waitForTimeout(100)
  await page.getByRole('button', { name: '标记选区' }).click()
  const markerDialog = page.getByRole('dialog', { name: '标记区块选区' })
  await markerDialog.waitFor()
  await markerDialog.getByRole('button', { name: /确\s*定/ }).click()
  await editor.evaluate((root) => {
    const firstSection = root.querySelector('section.rich-author-section')
    const next = firstSection.nextElementSibling
    const selection = window.getSelection(); root.focus()
    selection.setBaseAndExtent(next, 0, next, 0)
  })
  await page.waitForTimeout(100)
  await page.getByRole('button', { name: '插入区块' }).click()
  sections = editor.locator('section.rich-author-section')
  if (await sections.count() !== 2) throw new Error('未能在同码点插入第二个区块')
  await sections.nth(1).getByRole('textbox', { name: '区块名称' }).fill('备选二')
  await sections.nth(1).locator('.rich-author-section__content p').first().click()
  await page.keyboard.insertText('第二段')
  let responsePromise = page.waitForResponse((response) => response.url().endsWith('/document') && response.request().method() === 'PUT')
  await page.getByRole('button', { name: /保存/ }).first().click()
  let response = await responsePromise
  let saved = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  if (response.status() !== 200 || saved.document.content !== '甲\r\n🙂乙' || saved.document.sections.length !== 2
    || saved.document.sections.some((part) => part.anchor !== 1)
    || saved.document.sections[0].content !== '备注🙂' || !saved.document.sections[0].include_in_ai
    || saved.document.sections[1].content !== '第二段' || saved.document.sections[1].include_in_ai
    || saved.document.markers[0]?.section_key !== saved.document.sections[0].key) {
    throw new Error(`区块保存未完整保留 ${JSON.stringify({ status: response.status(), saved })}`)
  }
  await page.reload()
  await editor.waitFor({ timeout: 20000 })
  sections = editor.locator('section.rich-author-section')
  if (await sections.count() !== 2 || await sections.nth(0).getByRole('textbox', { name: '区块名称' }).inputValue() !== '备选一') throw new Error('刷新后区块丢失')
  await sections.nth(0).getByRole('button', { name: '折叠区块' }).click()
  await page.getByRole('button', { name: '全书标记' }).click()
  const list = page.getByRole('list', { name: '全书正文标记' })
  await list.getByRole('button', { name: /定位/ }).click()
  await page.waitForTimeout(500)
  if (await sections.nth(0).getByRole('button', { name: '展开区块' }).count()) throw new Error('区块标记定位没有先展开')
  if (await page.evaluate(() => String(window.getSelection())) !== '备注🙂') throw new Error('区块标记未定位到原文')
  await sections.nth(1).getByRole('button', { name: '区块上移' }).click()
  responsePromise = page.waitForResponse((result) => result.url().endsWith('/document') && result.request().method() === 'PUT')
  await page.getByRole('button', { name: /保存/ }).first().click()
  response = await responsePromise
  saved = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  if (response.status() !== 200 || saved.document.sections.map((part) => part.title).join(',') !== '备选二,备选一'
    || saved.document.markers[0].section_key !== saved.document.sections[1].key) throw new Error(`键盘替代排序丢身份 ${JSON.stringify(saved)}`)
  sections = editor.locator('section.rich-author-section')
  await page.waitForFunction(() => [...document.querySelectorAll('button[aria-label="拖动区块"]')].every((button) => !button.disabled))
  await editor.evaluate((root) => {
    window.__sectionDragChanges = []
    const capture = () => window.__sectionDragChanges.push({ names: [...root.querySelectorAll('section.rich-author-section input[aria-label="区块名称"]')].map((item) => item.value),
      messages: [...document.querySelectorAll('.ant-message')].map((item) => item.innerText) })
    new MutationObserver(capture).observe(root, { childList: true, subtree: true, attributes: true })
    for (const name of ['mousedown', 'mouseup', 'dragstart', 'dragover', 'drop', 'dragend']) root.addEventListener(name, (event) => {
      window.__sectionDragChanges.push({ event: name, target: event.target?.outerHTML?.slice(0, 150),
        x: event.clientX, y: event.clientY, connected: root.isConnected, types: [...(event.dataTransfer?.types ?? [])] })
    }, true)
    capture()
  })
  const dragHandle = sections.nth(0).getByRole('button', { name: '拖动区块' })
  await sections.nth(1).scrollIntoViewIfNeeded(); await dragHandle.scrollIntoViewIfNeeded()
  const targetBox = await sections.nth(1).boundingBox()
  await dragHandle.dragTo(sections.nth(1), {
    targetPosition: { x: Math.min(60, targetBox.width - 5), y: targetBox.height - 5 },
  })
  if (await sections.nth(0).getByRole('textbox', { name: '区块名称' }).inputValue() !== '备选一') {
    throw new Error(`真实拖动未移动区块 ${JSON.stringify(await page.evaluate(() => window.__sectionDragChanges))}`)
  }
  responsePromise = page.waitForResponse((result) => result.url().endsWith('/document') && result.request().method() === 'PUT')
  await page.getByRole('button', { name: /保存/ }).first().click()
  response = await responsePromise
  saved = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  if (response.status() !== 200 || saved.document.sections.map((part) => part.title).join(',') !== '备选一,备选二'
    || saved.document.markers[0].section_key !== saved.document.sections[0].key) throw new Error(`拖动后保存丢身份 ${JSON.stringify(saved)}`)
  await sections.nth(1).locator('.rich-author-section__content').evaluate((root) => {
    const text = root.querySelector('p').firstChild
    window.getSelection().setBaseAndExtent(text, 0, text, text.length)
  })
  await page.waitForTimeout(100)
  await page.getByRole('button', { name: /粗\s*体/ }).click()
  await sections.nth(1).getByRole('button', { name: '转为正文' }).click()
  const conversion = page.getByRole('dialog', { name: /转为正文/ })
  await conversion.waitFor()
  if (!(await conversion.getByText(/清空当前编辑器撤销历史/).count())) throw new Error('转换前未说明撤销历史和版本恢复')
  const convertedResponse = page.waitForResponse((result) => result.url().endsWith('/convert') && result.request().method() === 'POST')
  await conversion.getByRole('button', { name: '保存并转换' }).click()
  const converted = await convertedResponse
  await page.waitForTimeout(300)
  saved = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  if (converted.status() !== 200 || saved.document.content !== '甲第二段\r\n🙂乙'
    || saved.document.sections.length !== 1 || saved.document.sections[0].title !== '备选一'
    || !saved.document.formatting.inline.some((style) => style.kind === 'bold' && style.start === 1 && style.end === 4)) {
    throw new Error(`区块转换正文或格式丢失 ${JSON.stringify({ status: converted.status(), saved })}`)
  }
  const history = await page.evaluate(async ({ pid, cid }) => {
    const base = `/api/v1/projects/${pid}/chapters/${cid}/versions`
    const versions = await (await fetch(base)).json()
    return Promise.all(versions.map(async (version) => (await fetch(`${base}/${version.id}`)).json()))
  }, target)
  const beforeConversion = history.flatMap((version) => version.scene_snapshot?.scenes ?? [])
    .map((scene) => scene.document).find((document) => document?.sections?.some((part) => part.title === '备选二'
      && part.content === '第二段' && part.formatting.inline.some((style) => style.kind === 'bold' && style.start === 0 && style.end === 3))
      && document.sections.some((part) => part.title === '备选一') && document.markers.some((marker) => marker.status === 'active'))
  if (!beforeConversion) throw new Error('转换前的区块、格式和标记未完整进入章节版本')
  sections = editor.locator('section.rich-author-section')
  await sections.first().getByRole('button', { name: '删除区块' }).click()
  const deletion = page.getByRole('dialog', { name: /删除区块/ })
  await deletion.getByRole('button', { name: '确认删除' }).click()
  responsePromise = page.waitForResponse((result) => result.url().endsWith('/document') && result.request().method() === 'PUT')
  await page.getByRole('button', { name: /保存/ }).first().click()
  response = await responsePromise
  saved = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  if (response.status() !== 200 || saved.document.sections.length || saved.document.markers[0].status !== 'stale'
    || saved.document.markers[0].excerpt !== '备注🙂') throw new Error(`删除区块丢失标记 ${JSON.stringify(saved)}`)
  await page.getByRole('button', { name: /撤\s*销/ }).click()
  responsePromise = page.waitForResponse((result) => result.url().endsWith('/document') && result.request().method() === 'PUT')
  await page.getByRole('button', { name: /保存/ }).first().click()
  response = await responsePromise
  saved = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  if (response.status() !== 200 || saved.document.sections[0]?.title !== '备选一' || saved.document.markers[0].status !== 'active') {
    throw new Error(`跨保存撤销删除未复活区块与标记 ${JSON.stringify(saved)}`)
  }
  await page.getByRole('button', { name: /重\s*做/ }).click()
  responsePromise = page.waitForResponse((result) => result.url().endsWith('/document') && result.request().method() === 'PUT')
  await page.getByRole('button', { name: /保存/ }).first().click()
  response = await responsePromise
  saved = await page.evaluate(async (url) => (await fetch(url)).json(), endpoint)
  if (response.status() !== 200 || saved.document.sections.length || saved.document.markers[0].status !== 'stale') {
    throw new Error(`跨保存重做删除未保留标记 ${JSON.stringify(saved)}`)
  }
  return { result: 'PASS', saved }
}
