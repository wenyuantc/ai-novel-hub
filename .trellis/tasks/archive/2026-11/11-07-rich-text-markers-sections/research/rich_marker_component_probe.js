// 实际 React 编辑器与 PM 插件，无 HTTP；用于证明正文/元数据事务一致。
async (page) => {
  await page.goto('http://127.0.0.1:5179/')
  await page.evaluate(async () => {
    const code = await (await fetch('/src/components/scenes/RichSceneEditor.tsx')).text()
    const version = code.match(/react\.js\?v=([a-z\d]+)/)?.[1]
    const { default: React } = await import(`/node_modules/.vite/deps/react.js?v=${version}`)
    const { default: ReactDOM } = await import(`/node_modules/.vite/deps/react-dom_client.js?v=${version}`)
    const { App } = await import(`/node_modules/.vite/deps/antd.js?v=${version}`)
    const { default: RichSceneEditor } = await import('/src/components/scenes/RichSceneEditor.tsx')
    const marker = { key: '12345678-1234-4123-8123-123456789def', section_key: null, status: 'active', start: 0, end: 4, color: 'blue', excerpt: '', excerpt_truncated: false, last_start: null }
    const doc = { content: '甲\r\n🙂乙\n尾', formatting: { version: 1, boundaries: [], inline: [], blocks: [] }, sections: [], retired_section_keys: [], markers: [marker] }
    window.__marked = { events: [], current: doc }
    function Wrapper() {
      const [value, setValue] = React.useState(doc)
      const ref = React.useRef(null)
      window.__markerHandle = ref
      return React.createElement(RichSceneEditor, { ref, document: value, readOnly: false, onDocument: (next, proof) => {
        window.__marked.current = next; window.__marked.events.push(proof); setValue(next)
      }, onSelection: () => {} })
    }
    const container = document.createElement('div'); document.body.appendChild(container)
    ReactDOM.createRoot(container).render(React.createElement(App, null, React.createElement(Wrapper)))
  })
  const editor = page.getByRole('textbox', { name: '当前场景正文' })
  await editor.waitFor()
  await editor.evaluate((root) => {
    const a = root.querySelectorAll('p')[0].querySelector('.rich-marker').firstChild
    const b = root.querySelectorAll('p')[1].querySelector('.rich-marker').firstChild
    root.focus(); window.getSelection().setBaseAndExtent(a, 0, b, 2)
  })
  await page.waitForTimeout(100)
  const selection = await page.evaluate(() => window.__markerHandle.current.selection())
  await page.keyboard.press('Backspace')
  await page.waitForTimeout(100)
  let result = await page.evaluate(() => window.__marked)
  if (result.current.content !== '乙\n尾' || result.current.markers[0].status !== 'stale' || result.current.markers[0].excerpt !== '甲\r\n🙂') throw new Error(`跨段删除错误 ${JSON.stringify(result)}`)
  if (!result.events.at(-1).mappedMarkerKeys.includes(result.current.markers[0].key)) throw new Error('没有事务映射证据')
  await page.getByRole('button', { name: /撤\s*销/ }).click()
  await page.waitForTimeout(80)
  result = await page.evaluate(() => window.__marked)
  if (result.current.content !== '甲\r\n🙂乙\n尾' || result.current.markers[0].status !== 'active') throw new Error('撤销未同步恢复标记和正文')
  await page.getByRole('button', { name: /重\s*做/ }).click()
  await page.waitForTimeout(80)
  result = await page.evaluate(() => window.__marked)
  if (result.current.content !== '乙\n尾' || result.current.markers[0].status !== 'stale') throw new Error('重做未同步恢复失效')
  return { result: 'PASS', selection, lastProof: result.events.at(-1), marker: result.current.markers[0] }
}
