// 真实 RichSceneEditor + Chromium，验证编辑权与 AI 权限分离及动态撤权。
async (page) => {
  await page.setViewportSize({ width: 375, height: 812 })
  // 探针期间将 rich_section_probe_shell.html 复制到 Vite public；结束后移除副本。
  await page.goto('http://127.0.0.1:5179/__codex_section_ai_probe_7x5P91.html')
  await page.evaluate(async () => {
    const source = await (await fetch('/src/components/scenes/RichSceneEditor.tsx')).text()
    const version = source.match(/react\.js\?v=([a-z\d]+)/)?.[1]
    if (!version) throw new Error('React 依赖版本不存在')
    const { default: React } = await import(`/node_modules/.vite/deps/react.js?v=${version}`)
    const { default: ReactDOM } = await import(`/node_modules/.vite/deps/react-dom_client.js?v=${version}`)
    const { App, ConfigProvider, theme } = await import(`/node_modules/.vite/deps/antd.js?v=${version}`)
    const { default: RichSceneEditor } = await import('/src/components/scenes/RichSceneEditor.tsx')
    const { projectAccess } = await import('/src/utils/projectAccess.ts')
    const themePath = source.match(/from "([^"]*\/theme\/themeStore\.ts[^"]*)"/)?.[1]
    if (!themePath) throw new Error('主题 store 路径不存在')
    const { useThemeStore } = await import(themePath)
    useThemeStore.setState({ mode: 'light' })
    const access = projectAccess({ id: 1, owner: { id: 2 } },
      { id: 1, is_super_admin: true, effective_permissions: [] }, 1)
    if (!access.canEdit || access.canUseAI) throw new Error('他人作品的人工编辑 / AI 权限不符')
    const formatting = () => ({ version: 1, boundaries: [], inline: [], blocks: [] })
    const documentData = { content: '正文🙂', formatting: formatting(), sections: [
      { key: '11111111-1111-4111-8111-111111111111', anchor: 2, order: 0, title: '作者备注',
        content: '备注文字', formatting: formatting(), include_in_ai: false },
    ], retired_section_keys: [], markers: [] }
    const probe = window.__sectionPermissionProbe = { calls: [], changes: [], access, themeStore: useThemeStore }
    function Harness() {
      const [value, setValue] = React.useState(documentData)
      const [allowed, setAllowed] = React.useState(false)
      const mode = useThemeStore((state) => state.mode)
      probe.setAllowed = setAllowed
      return React.createElement(ConfigProvider, { theme: { algorithm: mode === 'dark' ? theme.darkAlgorithm : theme.defaultAlgorithm } }, React.createElement(App, null,
        React.createElement(RichSceneEditor, { document: value, savedDocument: documentData, readOnly: false,
          onDocument: (next) => { probe.changes.push(next); setValue(next) }, onSelection: () => {},
          onSectionConvert: () => {}, onSectionRewrite: allowed ? (selected) => probe.calls.push(selected) : undefined })))
    }
    document.body.replaceChildren()
    const host = document.createElement('div'); document.body.append(host)
    ReactDOM.createRoot(host).render(React.createElement(Harness))
  })
  const editor = page.getByRole('textbox', { name: '当前场景正文' })
  await editor.waitFor()
  if (!await page.getByText('超限不会截断草稿', { exact: false }).isVisible()) throw new Error('编辑器缺少可见容量提示')
  const block = editor.locator('.rich-author-section').first()
  const rewrite = block.getByRole('button', { name: '选区 AI 编辑' })
  if (!await rewrite.isDisabled() || await block.getByRole('button', { name: '转为正文' }).isDisabled()
      || await page.getByRole('button', { name: '插入区块' }).isDisabled()) throw new Error('人工编辑 / AI 控件权限错误')
  await block.getByRole('textbox', { name: '区块名称' }).fill('人工编辑可用')
  await editor.locator('p').first().evaluate((element) => {
    const text = document.createTreeWalker(element, NodeFilter.SHOW_TEXT).nextNode()
    window.getSelection().setBaseAndExtent(text, 1, text, 1)
  })
  await page.getByRole('button', { name: '插入区块' }).click()
  if (await editor.locator('.rich-author-section').count() !== 2) throw new Error('禁用 AI 时无法插入普通区块')
  const original = editor.locator('.rich-author-section').last()
  const originalRewrite = original.getByRole('button', { name: '选区 AI 编辑' })
  await page.evaluate(() => window.__sectionPermissionProbe.setAllowed(true))
  await originalRewrite.waitFor({ state: 'visible' })
  await page.waitForFunction(() => [...document.querySelectorAll('button[aria-label="选区 AI 编辑"]')].every((button) => !button.disabled))
  await original.locator('.rich-author-section__content p').first().evaluate((element) => {
    const text = document.createTreeWalker(element, NodeFilter.SHOW_TEXT).nextNode()
    window.getSelection().setBaseAndExtent(text, 0, text, text.length)
  })
  await originalRewrite.click()
  await page.waitForFunction(() => window.__sectionPermissionProbe.calls.length === 1)
  await page.evaluate(() => window.__sectionPermissionProbe.setAllowed(false))
  await page.waitForFunction(() => [...document.querySelectorAll('button[aria-label="选区 AI 编辑"]')].every((button) => button.disabled))
  await originalRewrite.evaluate((button) => button.dispatchEvent(new MouseEvent('click', { bubbles: true })))
  if ((await page.evaluate(() => window.__sectionPermissionProbe.calls.length)) !== 1
      || await original.getByRole('textbox', { name: '区块名称' }).isDisabled()
      || await page.getByRole('button', { name: '插入区块' }).isDisabled()) throw new Error('撤销 AI 权限后操作边界错误')
  const markerColors = () => page.evaluate(() => {
    const wrapper = document.querySelector('.rich-scene-editor')
    const result = []
    for (const color of ['yellow', 'green', 'blue', 'purple', 'red']) {
      const sample = document.createElement('span')
      sample.className = `rich-marker rich-marker-${color}`
      sample.textContent = '标记'
      wrapper.append(sample)
      const style = getComputedStyle(sample)
      result.push({ background: style.backgroundColor, decoration: style.textDecorationStyle })
      sample.remove()
    }
    return result
  })
  const light = await markerColors()
  await page.evaluate(() => window.__sectionPermissionProbe.themeStore.setState({ mode: 'dark' }))
  await page.waitForFunction(() => document.querySelector('.rich-scene-editor').style.getPropertyValue('--rich-marker-yellow').includes('.30'))
  const dark = await markerColors()
  if (new Set(light.map((item) => item.background)).size !== 5 || new Set(dark.map((item) => item.background)).size !== 5
      || light.some((item) => item.decoration !== 'dotted') || dark.some((item) => item.decoration !== 'dotted')
      || light.every((item, index) => item.background === dark[index].background)) throw new Error(`标记明暗颜色或非颜色线索失效 ${JSON.stringify({ light, dark })}`)
  if (await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)) throw new Error('暗色主题下页面横向溢出')
  return 'PASS: 375px 区分他人作品编辑 / AI 能力、真实组件创建区块、动态授权撤销及五色明暗主题'
}
