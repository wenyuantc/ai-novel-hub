async (page) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('http://127.0.0.1:5179/')
  await page.evaluate(async () => {
    const source = await (await fetch('/src/components/scenes/MarkerTimeline.tsx')).text()
    const version = source.match(/antd\.js\?v=([a-z\d]+)/)?.[1]
    if (!version) throw new Error('Vite dependency version not found')
    const { default: React } = await import(`/node_modules/.vite/deps/react.js?v=${version}`)
    const { default: ReactDOM } = await import(`/node_modules/.vite/deps/react-dom_client.js?v=${version}`)
    const { App } = await import(`/node_modules/.vite/deps/antd.js?v=${version}`)
    const { useAuthStore } = await import('/src/stores/auth.ts')
    const { default: Timeline } = await import('/src/components/scenes/MarkerTimeline.tsx')
    const user = (id) => ({ id, username: `user${id}`, is_admin: false, is_super_admin: false,
      effective_permissions: [], roles: [], authorization_revision: id, created_at: '' })
    useAuthStore.setState({ user: user(1) })
    window.__markerProbe = { calls: [], jumps: [], relinks: [], deletes: [], switchUser: () => useAuthStore.setState({ user: user(2) }) }
    const previous = window.fetch
    const createPage = (offset, owner = 1) => ({ offset, limit: 20, total: 21, planning_revision: 1, snapshot: 'snapshot-A',
      items: Array.from({ length: offset ? 1 : 20 }, (_, i) => ({ key: `marker-${i}`, chapter_id: 1, chapter_no: 1,
        chapter_title: owner === 1 ? `原作者${'x'.repeat(180)}` : '新作者', scene_id: i + 1, scene_title: `场景${i}`,
        scene_active: i !== 0, section_key: i === 0 ? 'section' : null,
        section_title: i === 0 ? 'y'.repeat(200) : null, content_revision: 1,
        status: i === 0 ? 'stale' : 'active', color: 'yellow', start: i === 0 ? null : 0,
        end: i === 0 ? null : 1, last_start: i === 0 ? 0 : null, excerpt: i === 0 ? '旧片段' : '正文', excerpt_truncated: false,
        draft_excerpt: i === 0 ? '删除前的草稿末态' : null, draft_excerpt_truncated: i === 0, draft_last_start: i === 0 ? 4 : null })) })
    window.fetch = async (input, init) => {
      const url = new URL(String(input), location.href)
      if (!url.pathname.endsWith('/markers')) return previous(input, init)
      const offset = Number(url.searchParams.get('offset') || 0)
      const owner = useAuthStore.getState().user?.id
      window.__markerProbe.calls.push({ offset, snapshot: url.searchParams.get('snapshot'), owner })
      if (offset && window.__markerProbe.defer) return new Promise((resolve) => { window.__markerProbe.resolve = () => resolve(new Response(JSON.stringify(createPage(offset)), { headers: { 'content-type': 'application/json' } })) })
      if (offset && !window.__markerProbe.conflicted) {
        window.__markerProbe.conflicted = true
        return new Response(JSON.stringify({ detail: '目录快照已变化', code: 'marker_timeline_changed' }), { status: 409, headers: { 'content-type': 'application/json' } })
      }
      return new Response(JSON.stringify(createPage(offset, owner)), { headers: { 'content-type': 'application/json' } })
    }
    document.body.innerHTML = ''
    const node = document.createElement('div')
    document.body.appendChild(node)
    ReactDOM.createRoot(node).render(React.createElement(App, null, React.createElement(Timeline, {
      projectId: 1, editable: true,
      onJump: async (item) => { window.__markerProbe.jumps.push(item.key); return false },
      onRelink: async (item) => { window.__markerProbe.relinks.push(item.key); return true },
      onDelete: async (item) => { window.__markerProbe.deletes.push(item.key); return true },
    })))
  })
  await page.getByText('原作者', { exact: false }).first().waitFor()
  await page.getByText('服务端无法独立验证', { exact: false }).waitFor()
  await page.getByText('删除前的草稿末态').waitFor()
  await page.getByText('查看原有片段').click()
  await page.getByText('旧片段').waitFor()
  await page.getByText('原文片段过长，已截断。').waitFor()
  await page.getByRole('button', { name: /定\s*位/ }).first().focus()
  await page.keyboard.press('Enter')
  if ((await page.evaluate(() => window.__markerProbe.jumps)).length !== 1) throw new Error('jump callback not reached')
  if ((await page.evaluate(() => window.__markerProbe.calls)).length !== 1) throw new Error('cancelled jump reloaded or changed the page')
  const remove = page.getByRole('button', { name: '删除第 1 章场景0的标记' })
  await remove.click()
  await page.getByText('删除这条标记？').waitFor()
  await page.locator('.ant-popover-inner').getByRole('button', { name: /取\s*消/ }).click()
  if ((await page.evaluate(() => window.__markerProbe.deletes)).length) throw new Error('cancelled delete was executed')
  await remove.click()
  await page.locator('.ant-popover-inner').getByRole('button', { name: /删\s*除/ }).click()
  await page.waitForFunction(() => window.__markerProbe.deletes.length === 1 && window.__markerProbe.calls.length >= 2)
  await page.getByRole('button', { name: '下一页' }).click()
  await page.getByText('目录快照已变化').waitFor()
  if (await page.getByRole('button', { name: /定\s*位/ }).first().isEnabled()) throw new Error('stale page still allows jump')
  const pageCall = await page.evaluate(() => window.__markerProbe.calls.find((call) => call.offset === 20))
  if (pageCall?.snapshot !== 'snapshot-A') throw new Error('next page omitted snapshot')
  await page.getByRole('button', { name: '重新加载第一页' }).click()
  await page.getByRole('button', { name: '重新关联' }).waitFor()
  await page.getByRole('button', { name: '重新关联' }).click()
  await page.waitForFunction(() => window.__markerProbe.relinks.length === 1 && window.__markerProbe.calls.length >= 5)
  const size = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }))
  if (size.width !== 375 || size.scroll !== 375) throw new Error(`mobile overflow: ${JSON.stringify(size)}`)
  await page.evaluate(() => { window.__markerProbe.defer = true })
  await page.getByRole('button', { name: '下一页' }).click()
  await page.waitForFunction(() => typeof window.__markerProbe.resolve === 'function')
  await page.evaluate(() => window.__markerProbe.switchUser())
  await page.getByText('新作者', { exact: false }).first().waitFor()
  await page.evaluate(() => window.__markerProbe.resolve())
  await page.waitForTimeout(50)
  if (await page.getByText('原作者', { exact: false }).count()) throw new Error('old identity response leaked into new session')
  return 'PASS: Chromium 375px timeline snapshot conflict, guarded jump and delete, explicit relink and identity isolation'
}
