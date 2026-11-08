async (page) => {
  await page.setViewportSize({ width: 375, height: 812 })
  // 先将同目录 rich_section_probe_shell.html 复制到 frontend/public/__codex_section_ai_probe_7x5P91.html，
  // 该空白页仅注入 Vite React preamble；探针结束后移除这份临时副本。
  await page.goto('http://127.0.0.1:5179/__codex_section_ai_probe_7x5P91.html')
  await page.evaluate(async () => {
    const source = await (await fetch('/src/components/scenes/RichSectionRewriteModal.tsx')).text()
    const version = source.match(/react\.js\?v=([a-z\d]+)/)?.[1]
    if (!version) throw new Error('Vite dependency version not found')
    const { default: React } = await import(`/node_modules/.vite/deps/react.js?v=${version}`)
    const { useState } = React
    const { default: ReactDOM } = await import(`/node_modules/.vite/deps/react-dom_client.js?v=${version}`)
    const { App, ConfigProvider, theme } = await import(`/node_modules/.vite/deps/antd.js?v=${version}`)
    // Vite 热更新可能给模块追加 ?t=，探针必须引用组件实际使用的同一 store 实例。
    const imported = (path) => {
      const match = source.match(new RegExp(`from "([^"]*${path}[^\"]*)"`))
      if (!match) throw new Error(`Vite import not found: ${path}`)
      return match[1]
    }
    const { useAuthStore } = await import(imported('/stores/auth\\.ts'))
    const { useProjectStore } = await import(imported('/stores/project\\.ts'))
    const { invalidateRequestGeneration, setUnauthorizedHandler, setPermissionDeniedHandler } = await import(imported('/api/client\\.ts'))
    const { default: RewriteModal } = await import('/src/components/scenes/RichSectionRewriteModal.tsx')
    const user = (id) => ({ id, username: `user${id}`, is_admin: true, is_super_admin: true,
      effective_permissions: [], roles: [], authorization_revision: 1, created_at: '' })
    const target = { projectId: 1, chapterId: 2, sceneId: 3, sectionKey: '11111111-1111-4111-8111-111111111111',
      sectionTitle: '<img src=x onerror=alert(1)>秘密标题', includeInAi: false, contentRevision: 4, planningRevision: 5,
      start: 4, end: 7, selectedText: '选中🙂' }
    useAuthStore.setState({ user: user(1), status: 'authenticated' })
    useProjectStore.setState({ project: { id: 1, owner: { id: 1 } } })
    const probe = window.__sectionRewriteProbe = { target, calls: [], locks: [], applied: [], unauthorized: 0, denied: 0,
      delayed: false, delayedApply: false, conflict: true, projectStore: useProjectStore }
    setUnauthorizedHandler(() => { probe.unauthorized++ })
    setPermissionDeniedHandler(() => { probe.denied++ })
    probe.switchUser = () => {
      invalidateRequestGeneration()
      useAuthStore.setState({ user: user(2), status: 'authenticated' })
      useProjectStore.setState({ project: { id: 1, owner: { id: 2 } } })
    }
    probe.reauthenticate = () => invalidateRequestGeneration()
    probe.applyResponse = () => new Response(JSON.stringify({ content_revision: 5, planning_revision: 5, document: {
      content: '正式正文', formatting: { version: 1, boundaries: [], inline: [], blocks: [] },
      sections: [{ key: target.sectionKey, title: target.sectionTitle, content: '改好🙂', anchor: 0, order: 0,
        include_in_ai: false, formatting: { version: 1, boundaries: [], inline: [], blocks: [] } }],
      retired_section_keys: [], markers: [],
    } }), { headers: { 'content-type': 'application/json' } })
    const oldFetch = window.fetch.bind(window)
    window.fetch = async (input, init = {}) => {
      const url = new URL(String(input), location.href)
      if (!url.pathname.startsWith('/api/v1/projects/1/chapters/2/scenes/3/sections/')) return oldFetch(input, init)
      const body = JSON.parse(String(init.body))
      probe.calls.push({ url: url.pathname, body })
      if (url.pathname.endsWith('/apply')) {
        if (probe.conflict === 'format' && !body.confirm_format_loss) return new Response(JSON.stringify({ detail: '格式损失', code: 'rich_document_confirmation_required' }), { status: 409 })
        if (probe.conflict === 'unmapped') return new Response(JSON.stringify({ detail: '结构冲突', code: 'rich_document_unmapped' }), { status: 409 })
        if (probe.conflict === true) return new Response(JSON.stringify({ detail: '版本冲突', code: 'scene_content_conflict' }), { status: 409 })
        if (probe.delayedApply) return new Promise((resolve) => { probe.releaseApply = resolve })
        return probe.applyResponse()
      }
      if (probe.delayed) return new Promise((resolve) => { probe.release = resolve; probe.pendingSignal = init.signal })
      const done = { section_key: target.sectionKey, request_id: body.request_id, log_id: 7, chapter_id: 2,
        scene_id: 3, content_revision: 4, planning_revision: 5, start: 4, end: 7, original: '选中🙂', before: '',
        text: '<script>危险</script>改好🙂' }
      return new Response(`event: delta\ndata: ${JSON.stringify({ text: done.text })}\n\nevent: done\ndata: ${JSON.stringify(done)}\n\n`,
        { headers: { 'content-type': 'text/event-stream' } })
    }
    function Harness() {
      const [selected, setSelected] = useState(target)
      const [dark, setDark] = useState(false)
      probe.open = (value = target) => setSelected(value)
      probe.setDark = setDark
      return React.createElement(ConfigProvider, { theme: { algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm } },
        React.createElement(App, null, React.createElement(RewriteModal, { target: selected, aiConfigId: null,
          styleChoice: 'inherit', skillId: null, onClose: () => setSelected(null),
          onApplied: (value) => probe.applied.push(value), onLockChange: (value) => probe.locks.push(value) })))
    }
    document.body.replaceChildren()
    const root = document.createElement('div')
    document.body.append(root)
    ReactDOM.createRoot(root).render(React.createElement(Harness))
  })
  const main = page.getByRole('dialog', { name: '区块选区 AI 编辑' })
  await main.getByText('本区块已关闭 AI 自动使用', { exact: false }).waitFor()
  if (await main.locator('img').count()) throw new Error('section title was executed as HTML')
  await main.getByRole('button', { name: '继续并确认发送' }).click()
  let confirm = page.getByRole('dialog', { name: '确认本次发送' })
  await confirm.getByText('选中🙂').waitFor()
  await confirm.getByRole('button', { name: /取\s*消/ }).click()
  if ((await page.evaluate(() => window.__sectionRewriteProbe.calls)).length) throw new Error('cancelling confirmation sent a request')
  await main.getByRole('button', { name: '继续并确认发送' }).click()
  confirm = page.getByRole('dialog', { name: '确认本次发送' })
  await confirm.getByRole('button', { name: '确认并发送' }).click()
  await main.getByText('与原选区对比').waitFor()
  const first = await page.evaluate(() => window.__sectionRewriteProbe.calls[0])
  const actual = await page.evaluate(async () => Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode('选中🙂'))), (b) => b.toString(16).padStart(2, '0')).join(''))
  if (!first.body.confirmed || first.body.selected_hash !== actual) throw new Error(`selection hash mismatch: ${first.body.selected_hash}`)
  if (JSON.stringify(first.body).includes('秘密标题') || JSON.stringify(first.body).includes('选中🙂')) throw new Error('request body includes unneeded section title or text')
  if (await main.locator('script').count()) throw new Error('model output executed as HTML')
  await main.getByRole('button', { name: '重新生成' }).click()
  await page.getByRole('dialog', { name: '确认本次发送' }).getByRole('button', { name: /取\s*消/ }).click()
  if ((await page.evaluate(() => window.__sectionRewriteProbe.calls)).length !== 1) throw new Error('cancelled retry sent request')
  await main.getByRole('button', { name: '重新生成' }).click()
  await page.getByRole('dialog', { name: '确认本次发送' }).getByRole('button', { name: '确认并发送' }).click()
  await page.waitForFunction(() => window.__sectionRewriteProbe.calls.length === 2)
  await main.getByRole('button', { name: '采纳选区' }).waitFor({ state: 'visible' })
  const ids = await page.evaluate(() => window.__sectionRewriteProbe.calls.map((call) => call.body.request_id))
  if (ids[0] === ids[1]) throw new Error('re-confirmed retry reused an old UUID')
  await main.getByRole('button', { name: '采纳选区' }).click()
  await main.getByText('正文、规划或 AI 可见范围已变化', { exact: false }).waitFor()
  if (!await main.getByRole('button', { name: '采纳选区' }).isDisabled() || !await main.getByText('与原选区对比').isVisible()) throw new Error('409 did not preserve and freeze the preview')
  const size = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }))
  if (size.width !== 375 || size.scroll !== 375) throw new Error(`mobile overflow: ${JSON.stringify(size)}`)
  await main.getByRole('button', { name: '放弃并关闭' }).click()
  await page.evaluate(() => { window.__sectionRewriteProbe.conflict = 'format'; window.__sectionRewriteProbe.open() })
  await main.getByRole('button', { name: '继续并确认发送' }).click()
  await page.getByRole('dialog', { name: '确认本次发送' }).getByRole('button', { name: '确认并发送' }).click()
  await main.getByRole('button', { name: '采纳选区' }).click()
  await main.getByText('替换及其接缝处无法可靠保留的格式可能丢失', { exact: false }).waitFor()
  const count = await page.evaluate(() => window.__sectionRewriteProbe.calls.length)
  await main.getByRole('button', { name: '核对格式并采纳' }).click()
  let format = page.getByRole('dialog', { name: '确认格式变化' })
  await format.getByRole('button', { name: /取\s*消/ }).click()
  if ((await page.evaluate(() => window.__sectionRewriteProbe.calls.length)) !== count) throw new Error('cancelled format confirmation wrote data')
  await main.getByRole('button', { name: '核对格式并采纳' }).click()
  format = page.getByRole('dialog', { name: '确认格式变化' })
  await format.getByRole('button', { name: '确认格式变化并采纳' }).click()
  await page.waitForFunction(() => window.__sectionRewriteProbe.applied.length === 1)
  if (!(await page.evaluate(() => window.__sectionRewriteProbe.calls.at(-1).body.confirm_format_loss))) throw new Error('explicit format confirmation was not sent')
  await page.evaluate(() => { window.__sectionRewriteProbe.conflict = 'unmapped'; window.__sectionRewriteProbe.open() })
  await main.getByRole('button', { name: '继续并确认发送' }).click()
  await page.getByRole('dialog', { name: '确认本次发送' }).getByRole('button', { name: '确认并发送' }).click()
  await main.getByRole('button', { name: '采纳选区' }).click()
  await main.getByText('无法安全自动映射的结构', { exact: false }).waitFor()
  if (await main.getByRole('button', { name: '核对格式并采纳' }).count()) throw new Error('unmapped structure offered format-loss bypass')
  await main.getByRole('button', { name: '放弃并关闭' }).click()
  await page.evaluate(() => { window.__sectionRewriteProbe.conflict = false; window.__sectionRewriteProbe.setDark(true); window.__sectionRewriteProbe.open() })
  await main.getByRole('button', { name: '继续并确认发送' }).click()
  await page.getByRole('dialog', { name: '确认本次发送' }).getByRole('button', { name: '确认并发送' }).click()
  await main.getByRole('button', { name: '采纳选区' }).waitFor({ state: 'visible' })
  await main.getByRole('button', { name: '采纳选区' }).click()
  await page.waitForFunction(() => window.__sectionRewriteProbe.applied.length === 2)
  await page.evaluate(() => window.__sectionRewriteProbe.open({ ...window.__sectionRewriteProbe.target, includeInAi: true }))
  await main.getByText('本区块名称和其他内容也可能', { exact: false }).waitFor()
  await main.getByRole('button', { name: '放弃并关闭' }).click()
  await page.evaluate(() => window.__sectionRewriteProbe.open())
  await main.getByRole('button', { name: '继续并确认发送' }).click()
  const beforeSwitch = await page.evaluate(() => window.__sectionRewriteProbe.calls.length)
  await page.evaluate(() => window.__sectionRewriteProbe.open({ ...window.__sectionRewriteProbe.target,
    sectionKey: '22222222-2222-4222-8222-222222222222', sectionTitle: '新目标' }))
  await main.getByText('新目标', { exact: false }).waitFor()
  if (await page.getByRole('dialog', { name: '确认本次发送' }).count() || (await page.evaluate(() => window.__sectionRewriteProbe.calls.length)) !== beforeSwitch) throw new Error('target change retained an old confirmation')
  await main.getByRole('button', { name: '放弃并关闭' }).click()
  await page.evaluate(() => { window.__sectionRewriteProbe.delayed = true; window.__sectionRewriteProbe.open() })
  await main.getByRole('button', { name: '继续并确认发送' }).click()
  await page.getByRole('dialog', { name: '确认本次发送' }).getByRole('button', { name: '确认并发送' }).click()
  await page.waitForFunction(() => typeof window.__sectionRewriteProbe.release === 'function')
  await page.evaluate(() => {
    window.__sectionRewriteProbe.switchUser()
    window.__sectionRewriteProbe.release(new Response('{"detail":"过期"}', { status: 401 }))
  })
  await page.waitForTimeout(100)
  const guard = await page.evaluate(() => ({ unauthorized: window.__sectionRewriteProbe.unauthorized,
    denied: window.__sectionRewriteProbe.denied, applied: window.__sectionRewriteProbe.applied.length }))
  if (guard.unauthorized || guard.denied || guard.applied !== 2 || await main.getByText('与原选区对比').count()
      || await main.getByText('秘密标题', { exact: false }).count() || await main.getByText('选中🙂', { exact: false }).count()) {
    throw new Error(`old identity leaked into new session: ${JSON.stringify(guard)}`)
  }
  await page.evaluate(() => { window.__sectionRewriteProbe.delayed = false; window.__sectionRewriteProbe.delayedApply = true;
    window.__sectionRewriteProbe.open({ ...window.__sectionRewriteProbe.target, sectionTitle: '新身份的选区' }) })
  await main.getByRole('button', { name: '继续并确认发送' }).click()
  await page.getByRole('dialog', { name: '确认本次发送' }).getByRole('button', { name: '确认并发送' }).click()
  await main.getByRole('button', { name: '采纳选区' }).click()
  await page.waitForFunction(() => typeof window.__sectionRewriteProbe.releaseApply === 'function')
  await page.evaluate(() => {
    window.__sectionRewriteProbe.reauthenticate()
    window.__sectionRewriteProbe.releaseApply(window.__sectionRewriteProbe.applyResponse())
  })
  await page.waitForTimeout(100)
  if ((await page.evaluate(() => window.__sectionRewriteProbe.applied.length)) !== 2
      || await main.getByText('新身份的选区', { exact: false }).count()) throw new Error('old apply response survived a new session of the same user')
  // 管理员仍有人工编辑权时，作品所有权变化会撤销 AI 能力并中止在途请求。
  await page.evaluate(() => { window.__sectionRewriteProbe.delayedApply = false; window.__sectionRewriteProbe.delayed = true;
    window.__sectionRewriteProbe.release = null; window.__sectionRewriteProbe.open({ ...window.__sectionRewriteProbe.target, sectionTitle: '撤权前选区' }) })
  await main.getByRole('button', { name: '继续并确认发送' }).click()
  await page.getByRole('dialog', { name: '确认本次发送' }).getByRole('button', { name: '确认并发送' }).click()
  await page.waitForFunction(() => typeof window.__sectionRewriteProbe.release === 'function')
  await page.evaluate(() => window.__sectionRewriteProbe.projectStore.setState({ project: { id: 1, owner: { id: 1 } } }))
  await page.waitForFunction(() => window.__sectionRewriteProbe.pendingSignal.aborted)
  await page.evaluate(() => window.__sectionRewriteProbe.release(new Response('event: delta\ndata: {"text":"迟到内容"}\n\n',
    { headers: { 'content-type': 'text/event-stream' } })))
  await page.waitForTimeout(100)
  if ((await page.evaluate(() => window.__sectionRewriteProbe.applied.length)) !== 2
      || await main.getByText('撤权前选区', { exact: false }).count()
      || await main.getByText('迟到内容', { exact: false }).count()) throw new Error('AI 运行中撤权后旧预览或结果仍可见')
  return 'PASS: 375px disclosure, confirmations, retry/hash/UUID, target changes, SSE, format and structure conflict, theme, late identity/apply and in-flight AI revocation'
}
