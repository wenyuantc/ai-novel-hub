async (page) => {
  await page.setViewportSize({ width: 375, height: 812 })
  // 测试前将 rich_section_probe_shell.html 复制为 frontend/public/__codex_section_ai_probe_7x5P91.html。
  await page.goto('http://127.0.0.1:5179/__codex_section_ai_probe_7x5P91.html')
  await page.evaluate(async () => {
    const source = await (await fetch('/src/components/chapter/RewritePanel.tsx')).text()
    const version = source.match(/react\.js\?v=([a-z\d]+)/)?.[1]
    if (!version) throw new Error('Vite dependency version not found')
    const { default: React } = await import(`/node_modules/.vite/deps/react.js?v=${version}`)
    const { useState } = React
    const { default: ReactDOM } = await import(`/node_modules/.vite/deps/react-dom_client.js?v=${version}`)
    const { App, ConfigProvider, theme } = await import(`/node_modules/.vite/deps/antd.js?v=${version}`)
    const imported = (path) => {
      const match = source.match(new RegExp(`from "([^"]*${path}[^\"]*)"`))
      if (!match) throw new Error(`Vite import not found: ${path}`)
      return match[1]
    }
    const { useAuthStore } = await import(imported('/stores/auth\\.ts'))
    const { useProjectStore } = await import(imported('/stores/project\\.ts'))
    const { invalidateRequestGeneration, setUnauthorizedHandler } = await import(imported('/api/client\\.ts'))
    const { default: RewritePanel } = await import('/src/components/chapter/RewritePanel.tsx')
    const user = (id) => ({ id, username: `user${id}`, is_admin: false, is_super_admin: false,
      effective_permissions: [], roles: [], authorization_revision: 1, created_at: '' })
    useAuthStore.setState({ user: user(1), status: 'authenticated' })
    useProjectStore.setState({ project: { id: 1, owner: { id: 1 } } })
    const probe = window.__proseRewriteProbe = { calls: [], applied: [], locks: [], unauthorized: 0,
      mode: 'format', versions: { content_revision: 4, planning_revision: 5 }, deferSave: false }
    setUnauthorizedHandler(() => { probe.unauthorized++ })
    probe.switchUser = () => {
      invalidateRequestGeneration()
      useAuthStore.setState({ user: user(2), status: 'authenticated' })
      useProjectStore.setState({ project: { id: 1, owner: { id: 2 } } })
    }
    probe.reauthenticate = () => invalidateRequestGeneration()
    const previous = window.fetch.bind(window)
    window.fetch = async (input, init = {}) => {
      const url = new URL(String(input), location.href)
      if (!url.pathname.startsWith('/api/v1/projects/1/chapters/2/scenes/3/rewrite')) return previous(input, init)
      const body = JSON.parse(String(init.body))
      probe.calls.push({ path: url.pathname, body })
      if (url.pathname.endsWith('/apply')) {
        if (probe.mode === 'format' && !body.confirm_format_loss) return new Response(JSON.stringify({ code: 'rich_document_confirmation_required', detail: '格式损失' }), { status: 409 })
        if (probe.mode === 'unmapped') return new Response(JSON.stringify({ code: 'rich_document_unmapped', detail: '结构不可映射' }), { status: 409 })
        if (probe.mode === 'delayApply') return new Promise((resolve) => { probe.releaseApply = resolve })
        return new Response(JSON.stringify({ id: 3, content_revision: 5, planning_revision: 5, content: '前新尾' }), { headers: { 'content-type': 'application/json' } })
      }
      if (probe.mode === 'delayStream') return new Promise((resolve) => { probe.releaseStream = resolve })
      const result = { chapter_id: 2, scene_id: 3, content_revision: 4, planning_revision: 5, log_id: 7,
        start: body.start, end: body.end, original: probe.mode === 'mismatch' ? '错误原文' : '甲🙂',
        before: '前', text: '<script>结果不能执行</script>新' }
      return new Response(`event: delta\ndata: ${JSON.stringify({ text: result.text })}\n\nevent: done\ndata: ${JSON.stringify(result)}\n\n`,
        { headers: { 'content-type': 'text/event-stream' } })
    }
    function Harness() {
      const [content, setContent] = useState('前甲🙂尾')
      probe.setContent = setContent
      return React.createElement(ConfigProvider, { theme: { algorithm: theme.darkAlgorithm } }, React.createElement(App, null,
        React.createElement(RewritePanel, { pid: 1, chapterId: 2, sceneId: 3, content,
          selection: { start: 1, end: 4 }, aiConfigId: null, styleChoice: 'inherit', skillId: null, disabled: false,
          getSceneVersions: () => probe.versions,
          ensureSaved: () => probe.deferSave ? new Promise((resolve) => { probe.releaseSave = resolve }) : Promise.resolve(true),
          onLockChange: (value) => probe.locks.push(value), onSceneApplied: (value) => probe.applied.push(value), onApplied: () => {} })))
    }
    document.body.replaceChildren()
    const root = document.createElement('div')
    root.style.padding = '8px'
    document.body.append(root)
    ReactDOM.createRoot(root).render(React.createElement(Harness))
  })
  const start = page.getByRole('button', { name: '改写选中内容' })
  await start.click()
  await page.getByText('与原文对比').waitFor()
  const first = await page.evaluate(() => window.__proseRewriteProbe.calls[0])
  if (first.body.start !== 1 || first.body.end !== 3 || first.body.expected_content_revision !== 4 || first.body.expected_planning_revision !== 5) throw new Error('scene selection/versions were not bound')
  if (await page.locator('ins script, del script, pre script').count()) throw new Error('model output was rendered as executable HTML')
  await page.getByRole('button', { name: /采\s*纳/ }).click()
  await page.getByText('替换及其接缝处无法可靠保留的格式可能丢失', { exact: false }).waitFor()
  const beforeCancel = await page.evaluate(() => window.__proseRewriteProbe.calls.length)
  await page.getByRole('button', { name: '核对格式并采纳' }).click()
  await page.getByRole('dialog', { name: '确认格式变化' }).getByRole('button', { name: /取\s*消/ }).click()
  if ((await page.evaluate(() => window.__proseRewriteProbe.calls.length)) !== beforeCancel) throw new Error('cancelled format confirmation wrote data')
  await page.getByRole('button', { name: '核对格式并采纳' }).click()
  await page.getByRole('dialog', { name: '确认格式变化' }).getByRole('button', { name: '确认格式变化并采纳' }).click()
  await page.waitForFunction(() => window.__proseRewriteProbe.applied.length === 1)
  const apply = await page.evaluate(() => window.__proseRewriteProbe.calls.at(-1).body)
  if (apply.confirm_format_loss !== true || apply.expected_content_revision !== 4 || apply.expected_planning_revision !== 5 || apply.original !== '甲🙂') throw new Error('confirmed apply was not bound to the original preview')
  await page.evaluate(() => { window.__proseRewriteProbe.mode = 'unmapped' })
  await start.click()
  await page.getByRole('button', { name: /采\s*纳/ }).click()
  await page.getByText('无法安全自动映射', { exact: false }).waitFor()
  if (!await page.getByRole('button', { name: /采\s*纳/ }).isDisabled() || await page.getByRole('button', { name: '核对格式并采纳' }).count()) throw new Error('unmapped structure could be bypassed')
  await page.getByRole('button', { name: /放\s*弃/ }).click()
  await page.evaluate(() => { window.__proseRewriteProbe.mode = 'mismatch' })
  await start.click()
  await page.getByText('改写结果与已选择的目标', { exact: false }).waitFor()
  if (await page.getByRole('button', { name: /采\s*纳/ }).count()) throw new Error('mismatched preview was accepted')
  await page.evaluate(() => { window.__proseRewriteProbe.mode = 'normal' })
  await start.click()
  await page.getByText('与原文对比').waitFor()
  const beforeRegenerate = await page.evaluate(() => window.__proseRewriteProbe.calls.length)
  await page.evaluate(() => { window.__proseRewriteProbe.versions = { content_revision: 5, planning_revision: 5 } })
  await page.getByRole('button', { name: '重新生成' }).click()
  await page.getByText('正文或规划版本已变化', { exact: false }).waitFor()
  if ((await page.evaluate(() => window.__proseRewriteProbe.calls.length)) !== beforeRegenerate) throw new Error('regenerate silently used newer revisions')
  await page.getByRole('button', { name: /放\s*弃/ }).click()
  await page.evaluate(() => { window.__proseRewriteProbe.versions = { content_revision: 4, planning_revision: 5 }; window.__proseRewriteProbe.deferSave = true })
  await start.click()
  await page.waitForFunction(() => typeof window.__proseRewriteProbe.releaseSave === 'function')
  const beforeSaveRace = await page.evaluate(() => window.__proseRewriteProbe.calls.length)
  await page.evaluate(() => window.__proseRewriteProbe.setContent('前乙🙂尾'))
  await page.waitForTimeout(40)
  await page.evaluate(() => window.__proseRewriteProbe.releaseSave(true))
  await page.getByText('保存期间正文或选区已变化', { exact: false }).waitFor()
  if ((await page.evaluate(() => window.__proseRewriteProbe.calls.length)) !== beforeSaveRace) throw new Error('changed selection was sent after save')
  await page.evaluate(() => { window.__proseRewriteProbe.deferSave = false; window.__proseRewriteProbe.setContent('前甲🙂尾'); window.__proseRewriteProbe.mode = 'delayStream' })
  await page.waitForTimeout(40)
  await start.click()
  await page.waitForFunction(() => typeof window.__proseRewriteProbe.releaseStream === 'function')
  await page.evaluate(() => {
    window.__proseRewriteProbe.switchUser()
    window.__proseRewriteProbe.releaseStream(new Response('{"detail":"过期"}', { status: 401 }))
  })
  await page.waitForTimeout(100)
  if ((await page.evaluate(() => window.__proseRewriteProbe.unauthorized)) || await page.getByText('与原文对比').count()) throw new Error('late old session output leaked')
  await page.evaluate(() => { window.__proseRewriteProbe.mode = 'delayApply' })
  await start.click()
  await page.getByRole('button', { name: /采\s*纳/ }).click()
  await page.waitForFunction(() => typeof window.__proseRewriteProbe.releaseApply === 'function')
  await page.evaluate(() => {
    window.__proseRewriteProbe.reauthenticate()
    window.__proseRewriteProbe.releaseApply(new Response(JSON.stringify({ id: 3, content_revision: 5, planning_revision: 5, content: '前新尾' }), { headers: { 'content-type': 'application/json' } }))
  })
  await page.waitForTimeout(100)
  if ((await page.evaluate(() => window.__proseRewriteProbe.applied.length)) !== 1 || await page.getByText('与原文对比').count()) throw new Error('late apply reached a new session')
  const size = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }))
  if (size.width !== 375 || size.scroll !== 375) throw new Error(`mobile overflow: ${JSON.stringify(size)}`)
  return 'PASS: 375px prose rewrite, exact preview binding, format consent, unmapped, stale versions, save race and late identity/apply'
}
