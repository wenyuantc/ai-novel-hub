async (page) => {
  await page.goto('http://127.0.0.1:5179/')
  await page.evaluate(async () => {
    const code = await (await fetch('/src/components/scenes/RichSceneEditor.tsx')).text()
    const version = code.match(/react\.js\?v=([a-z\d]+)/)?.[1]
    const { default: React } = await import(`/node_modules/.vite/deps/react.js?v=${version}`)
    const { default: ReactDOM } = await import(`/node_modules/.vite/deps/react-dom_client.js?v=${version}`)
    const { App } = await import(`/node_modules/.vite/deps/antd.js?v=${version}`)
    const { default: RichSceneEditor } = await import('/src/components/scenes/RichSceneEditor.tsx')
    const doc = { content: '甲\r\n乙\n末', formatting: { version: 1, boundaries: [], inline: [], blocks: [] }, sections: [], retired_section_keys: [], markers: [] }
    window.__imeTrace = { events: [], current: doc }
    function Wrapper() {
      const [value, setValue] = React.useState(doc)
      return React.createElement(RichSceneEditor, { document: value, savedDocument: doc, readOnly: false,
        onCompositionChange: (value) => window.__imeTrace.events.push(['composition', value]),
        onDocument: (next, evidence) => { window.__imeTrace.events.push(['document', next.content, evidence]); window.__imeTrace.current = next; setValue(next) }, onSelection: () => {} })
    }
    const container = document.createElement('div'); document.body.appendChild(container)
    ReactDOM.createRoot(container).render(React.createElement(App, null, React.createElement(Wrapper)))
  })
  const editor = page.getByRole('textbox', { name: '当前场景正文' })
  await editor.waitFor({ timeout: 20000 }); await editor.focus()
  await editor.evaluate((root) => { const text = root.querySelectorAll('p')[1].firstChild; window.getSelection().setBaseAndExtent(text, text.length, text, text.length); root.addEventListener('compositionend', e => window.__imeTrace.events.push(['nativeEnd',e.data])) })
  await page.waitForTimeout(100)
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('Input.imeSetComposition', { text: 'pin', selectionStart: 3, selectionEnd: 3 })
  await page.waitForTimeout(100)
  await cdp.send('Input.imeSetComposition', { text: '拼音', selectionStart: 2, selectionEnd: 2 })
  await cdp.send('Input.insertText', { text: '拼音' })
  await page.waitForTimeout(300)
  const result = await page.evaluate(() => ({...window.__imeTrace, dom:document.querySelector('.ProseMirror').innerHTML,doc:document.querySelector('.ProseMirror').pmViewDesc.node.toJSON()}))
  await cdp.detach()
  return result
}
