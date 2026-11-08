// 独立 React/Chromium + 可控 HTTP：提案延迟响应不得跨账号/会话代次，采纳结果不确定不得重试。
async (page) => {
  await page.setViewportSize({ width: 375, height: 900 })
  await page.goto('http://127.0.0.1:5179/')
  await page.waitForTimeout(500)
  await page.evaluate(async () => {
    const code = await (await fetch('/src/components/scenes/RichReviewModal.tsx')).text()
    const version = code.match(/antd\.js\?v=([a-z\d]+)/)?.[1]
    if (!version) throw new Error('Vite dependency version not found')
    const { default: React } = await import(`/node_modules/.vite/deps/react.js?v=${version}`)
    const { default: ReactDOM } = await import(`/node_modules/.vite/deps/react-dom_client.js?v=${version}`)
    const { App } = await import(`/node_modules/.vite/deps/antd.js?v=${version}`)
    const { default: Modal } = await import('/src/components/scenes/RichReviewModal.tsx')
    const authPath = code.match(/from "([^\"]*\/stores\/auth\.ts[^\"]*)"/)?.[1]
    const clientPath = code.match(/from "([^\"]*\/api\/client\.ts[^\"]*)"/)?.[1]
    if (!authPath || !clientPath) throw new Error('无法绑定组件实际使用的会话模块')
    const { useAuthStore } = await import(authPath)
    const { invalidateRequestGeneration } = await import(clientPath)
    const identity = { task_key: '11a11111-1111-4111-8111-111111111111', run_key: '22a22222-2222-4222-8222-222222222222', token: '33a33333-3333-4333-8333-333333333333', chapter_id: 2, phase: 'awaiting_review' }
    const task = { id: 1, project_id: 1, chapter_id: 2, rich_review: identity, status: 'paused', kind: 'chapter.generate' }
    const format = { version: 1, boundaries: [], inline: [], blocks: [] }
    const preview = { reason: 'rich_document_review', ...identity, status: 'pending', task_status: 'paused', content_revision: 1, scenes: [{
      scene_id: 3, original: { content: '旧稿', formatting: format, sections: [{ key: '645d44c0-cc16-4949-973b-542022da8421',
        anchor: 1, order: 0, title: '绝密区块标题', content: '绝密作者内容', formatting: format, include_in_ai: false }], retired_section_keys: [], markers: [] },
      candidate: '新稿', proposed_formatting: format, proposed_markers: [], proposed_sections: [{ key: '645d44c0-cc16-4949-973b-542022da8421', anchor: 1, order: 0 }], unresolved_sections: [], format_loss: false,
    }] }
    const holder = document.createElement('div'); document.body.appendChild(holder)
    const root = ReactDOM.createRoot(holder); const oldFetch = window.fetch
    const state = { mode: '', sequence: 0, accepted: false, calls: 0, closed: 0, changed: 0, release: null, waiting: false }
    const json = (value) => new Response(JSON.stringify(value), { headers: { 'content-type': 'application/json' } })
    window.fetch = async (input, init) => {
      const url = String(input)
      if (url.includes('/rich-proposals/') && init?.method === 'POST') {
        state.calls++
        if (state.mode === 'accept-late') return await new Promise((resolve) => {
          state.waiting = true; state.release = () => { state.waiting = false; resolve(json({ status: 'applied', task_status: 'succeeded', chapter_id: 2, content_revision: 2 })) }
        })
        if (state.mode === 'accept-fail') throw new TypeError('模拟网络中断')
        state.accepted = true
        return json({ status: 'applied', task_status: 'succeeded', chapter_id: 2, content_revision: 2 })
      }
      if (url.includes('/rich-proposals/')) {
        if (state.mode === 'preview-late') return await new Promise((resolve) => {
          state.waiting = true; state.release = () => { state.waiting = false; resolve(json(preview)) }
        })
        return json({ ...preview, status: state.accepted ? 'applied' : 'pending' })
      }
      if (url.endsWith('/tasks/1')) {
        if (state.mode === 'refresh-fail') throw new TypeError('模拟任务读取失败')
        const nextIdentity = state.mode === 'reused-id' ? { ...identity, task_key: '其他任务身份' } : identity
        return json({ ...task, rich_review: nextIdentity, status: 'succeeded' })
      }
      return oldFetch(input, init)
    }
    window.reviewIdentityProbe = {
      state,
      mount(mode) {
        state.mode = mode; state.sequence++; state.accepted = false; state.calls = 0; state.closed = 0; state.changed = 0; state.waiting = false; state.release = null
        useAuthStore.setState({ status: 'authenticated', user: { id: 11, authorization_revision: 1, effective_permissions: [] } })
        root.render(React.createElement(App, null, React.createElement(Modal, { key: state.sequence, task,
          onClose: () => { state.closed++ }, onChanged: () => { state.changed++ } })))
      },
      changeActor() { useAuthStore.setState({ status: 'authenticated', user: { id: 22, authorization_revision: 1, effective_permissions: [] } }) },
      changeSession() { invalidateRequestGeneration() },
      release() { state.release?.() },
    }
  })
  const setup = async (mode) => { await page.evaluate((value) => window.reviewIdentityProbe.mount(value), mode); await page.getByText('核对 AI 候选稿').waitFor() }
  const state = () => page.evaluate(() => ({ ...window.reviewIdentityProbe.state, release: !!window.reviewIdentityProbe.state.release }))
  await setup('preview-late')
  await page.waitForFunction(() => window.reviewIdentityProbe.state.waiting)
  await page.evaluate(() => { window.reviewIdentityProbe.changeActor(); window.reviewIdentityProbe.release() })
  await page.waitForTimeout(200)
  if (await page.getByText('绝密区块标题').count() || (await state()).closed !== 1) throw new Error('旧账号迟到的预览泄漏或未关闭')
  await setup('accept-late')
  await page.getByText('绝密区块标题').waitFor()
  await page.getByRole('button', { name: '确认并采纳整章' }).click()
  await page.waitForFunction(() => window.reviewIdentityProbe.state.waiting)
  await page.evaluate(() => { window.reviewIdentityProbe.changeSession(); window.reviewIdentityProbe.release() })
  await page.waitForTimeout(200)
  if (await page.getByText('绝密区块标题').count() || (await state()).changed !== 0 || (await state()).closed !== 1) throw new Error('旧会话迟到采纳触发回调')
  for (const mode of ['refresh-fail', 'accept-fail', 'reused-id']) {
    await setup(mode); await page.getByText('绝密区块标题').waitFor()
    await page.getByRole('button', { name: '确认并采纳整章' }).click()
    await page.getByText(mode === 'accept-fail' ? /无法确认本次采纳结果/ : /候选已采纳，但无法核对/).waitFor()
    if (!(await page.getByRole('button', { name: '确认并采纳整章' }).isDisabled()) || (await state()).calls !== 1 || (await state()).changed !== 0) throw new Error(`不确定结果未冻结 ${mode}`)
  }
  await setup('success'); await page.getByText('绝密区块标题').waitFor()
  await page.getByRole('button', { name: '确认并采纳整章' }).click()
  await page.waitForFunction(() => window.reviewIdentityProbe.state.changed === 1)
  return { result: 'PASS', cases: ['旧账号迟到预览', '同账号会话代次迟到采纳', '成功后任务刷新失败', '请求结果不确定', '任务 ID 复用', '身份复验成功'] }
}
