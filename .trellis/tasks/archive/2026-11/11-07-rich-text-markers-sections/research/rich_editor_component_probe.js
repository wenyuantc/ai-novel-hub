// 独立组件和真实 Chromium，不代表产品端到端或区块/标记完成。
async (page) => {
  await page.goto('http://127.0.0.1:5179/')
  await page.evaluate(async () => {
    const code = await (await fetch('/src/components/scenes/RichSceneEditor.tsx')).text()
    const version = code.match(/react\.js\?v=([a-z\d]+)/)?.[1]
    if (!version) throw new Error('Vite React dependency version not found')
    const { default: React } = await import(`/node_modules/.vite/deps/react.js?v=${version}`)
    const { default: ReactDOM } = await import(`/node_modules/.vite/deps/react-dom_client.js?v=${version}`)
    const { App } = await import(`/node_modules/.vite/deps/antd.js?v=${version}`)
    const { default: RichSceneEditor } = await import('/src/components/scenes/RichSceneEditor.tsx')
    const original = '甲🙂\r\n\n乙é\t  \r第三行  \n'
    const doc = { content: original, formatting: { version: 1, boundaries: [], inline: [], blocks: [] }, sections: [], retired_section_keys: [], markers: [] }
    window.__editorProbe = { original, current: doc }
    function Wrapper() {
      const [value, setValue] = React.useState(doc)
      window.__editorProbe.reset = (content) => {
        const next = { ...doc, content, formatting: { version: 1, boundaries: [], inline: [], blocks: [] } }
        window.__editorProbe.current = next; setValue(next)
      }
      return React.createElement(RichSceneEditor, { document: value, readOnly: false,
        onDocument: (next) => { window.__editorProbe.current = next; setValue(next) }, onSelection: () => {} })
    }
    const container = document.createElement('div')
    document.body.appendChild(container)
    ReactDOM.createRoot(container).render(React.createElement(App, null, React.createElement(Wrapper)))
  })
  const editor = page.getByRole('textbox', { name: '当前场景正文' })
  await editor.waitFor({ timeout: 20000 })
  await editor.evaluate((root) => {
    const text = root.querySelector('p').firstChild
    const selection = window.getSelection()
    selection.setBaseAndExtent(text, text.length, text, text.length)
  })
  await page.keyboard.type('X')
  await page.waitForTimeout(100)
  let state = await page.evaluate(() => window.__editorProbe)
  if (state.current.content !== '甲🙂X\r\n\n乙é\t  \r第三行  \n') throw new Error(`插入后原换行丢失 ${JSON.stringify(state.current.content)}`)
  await page.getByRole('button', { name: /撤\s*销/ }).click()
  await page.waitForTimeout(100)
  state = await page.evaluate(() => window.__editorProbe)
  if (state.current.content !== state.original) throw new Error(`撤销未恢复原文 ${JSON.stringify(state.current.content)}`)
  await page.getByRole('button', { name: /重\s*做/ }).click()
  await page.waitForTimeout(100)
  state = await page.evaluate(() => window.__editorProbe)
  if (state.current.content !== '甲🙂X\r\n\n乙é\t  \r第三行  \n') throw new Error('重做未恢复修改')
  const reset = async (text) => { await page.evaluate((value) => window.__editorProbe.reset(value), text); await page.waitForTimeout(80) }
  const cursor = async (paragraph, offset) => editor.evaluate((root, at) => {
    const text = root.querySelectorAll('p')[at.paragraph].firstChild
    const selection = window.getSelection()
    selection.setBaseAndExtent(text, at.offset, text, at.offset)
  }, { paragraph, offset })
  const expectText = async (expected, label) => {
    await page.waitForTimeout(80)
    const actual = await page.evaluate(() => window.__editorProbe.current.content)
    if (actual !== expected) throw new Error(`${label}: ${JSON.stringify(actual)} != ${JSON.stringify(expected)}`)
  }
  await reset('甲\r\n乙\n末'); await cursor(1, 0); await page.keyboard.press('Backspace')
  await expectText('甲乙\n末', 'Backspace 合并')
  await reset('甲\r\n乙\n末'); await cursor(0, 1); await page.keyboard.press('Delete')
  await expectText('甲乙\n末', 'Delete 合并')
  await reset('甲乙\r\n丙'); await cursor(0, 1); await page.keyboard.press('Enter')
  await expectText('甲\n乙\r\n丙', 'Enter 新分隔')
  await reset('甲乙\r\n丙'); await cursor(0, 1)
  await editor.evaluate((root) => {
    const data = new DataTransfer(); data.setData('text/plain', 'x\r\ny\nz')
    root.dispatchEvent(new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: data }))
  })
  await expectText('甲x\r\ny\nz乙\r\n丙', '混合换行粘贴')
  await reset('甲🙂乙\r\n末')
  await editor.evaluate((root) => {
    const text = root.querySelector('p').firstChild
    window.getSelection().setBaseAndExtent(text, 1, text, 3)
  })
  await page.getByRole('button', { name: /粗\s*体/ }).click()
  await page.waitForTimeout(80)
  state = await page.evaluate(() => window.__editorProbe)
  if (JSON.stringify(state.current.formatting.inline) !== JSON.stringify([{ kind: 'bold', start: 1, end: 2 }])) throw new Error('emoji 粗体坐标错误')
  await reset('甲\r\n乙\n末'); await cursor(0, 0)
  await page.getByRole('button', { name: /标\s*题/ }).click()
  await expectText('甲\r\n乙\n末', '标题转换正文')
  state = await page.evaluate(() => window.__editorProbe)
  if (state.current.formatting.blocks[0]?.kind !== 'heading') throw new Error('标题转换失败')
  await page.getByRole('button', { name: '无序列表' }).click()
  await expectText('甲\r\n乙\n末', '列表转换正文')
  state = await page.evaluate(() => window.__editorProbe)
  if (state.current.formatting.blocks[0]?.kind !== 'bullet_list') throw new Error('列表转换失败')
  await editor.focus(); await page.keyboard.press('Tab')
  if (await editor.evaluate((root) => document.activeElement === root)) throw new Error('Tab 焦点被困在编辑器')
  await editor.focus(); await page.keyboard.press('Shift+Tab')
  if (await editor.evaluate((root) => document.activeElement === root)) throw new Error('Shift+Tab 焦点被困在编辑器')
  await reset('甲乙')
  await editor.evaluate((root) => {
    const text = root.querySelector('p').firstChild
    window.getSelection().setBaseAndExtent(text, 0, text, text.length)
    const data = new DataTransfer(); data.setData('text/html', '<img src=x onerror=alert(1)>')
    root.dispatchEvent(new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: data }))
  })
  await expectText('甲乙', '无纯文本粘贴不得删除选区')
  await reset('甲乙'); await cursor(0, 1)
  await editor.evaluate((root) => {
    const data = new DataTransfer()
    data.setData('text/plain', '<img src=x onerror=alert(1)>')
    data.setData('text/html', '<img src=x onerror=alert(1)>')
    root.dispatchEvent(new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: data }))
  })
  await expectText('甲<img src=x onerror=alert(1)>乙', 'HTML 粘贴')
  if (await editor.locator('img').count()) throw new Error('粘贴执行了用户 HTML')
  await reset('甲\r\n乙\n末'); await cursor(1, 1)
  await editor.evaluate((root) => { window.__imeProbe = []; root.addEventListener('compositionend', (event) => window.__imeProbe.push(event.data), { once: true }) })
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('Input.imeSetComposition', { text: 'pin', selectionStart: 3, selectionEnd: 3 })
  await cdp.send('Input.imeSetComposition', { text: '拼音', selectionStart: 2, selectionEnd: 2 })
  await cdp.send('Input.insertText', { text: '拼音' })
  await expectText('甲\r\n乙拼音\n末', 'IME 模拟提交')
  if (JSON.stringify(await page.evaluate(() => window.__imeProbe)) !== JSON.stringify(['拼音'])) throw new Error('未观察到组合完成事件')
  await cdp.detach()
  return { result: 'PASS', operations: ['插入', '撤销', '重做', 'Backspace', 'Delete', 'Enter', '混合换行粘贴', 'emoji 粗体', '标题/列表转换', '键盘焦点', '无纯文本粘贴保护', 'HTML 纯文本粘贴', 'CDP 模拟中文组合'] }
}
