async (page) => {
  const checks = []
  const assert = (name, actual, expected) => {
    if (JSON.stringify(actual) !== JSON.stringify(expected)) throw new Error(`${name}: ${JSON.stringify(actual)} != ${JSON.stringify(expected)}`)
    checks.push(name)
  }
  const text = () => page.evaluate(() => window.poc.state().text)
  const setup = (value, offset) => page.evaluate(({value, offset}) => {
    window.poc.load(value)
    const editor = window.poc.editor
    editor.commands.focus()
    if (offset !== undefined) {
      const options = window.poc.toPm(offset)
      if (!options.length) throw new Error(`unaddressable: ${offset}`)
      editor.commands.setTextSelection(options.at(-1))
    }
    window.poc.breakHistory()
  }, {value, offset})

  await setup('甲\r\n乙\n末', 3)
  await page.keyboard.press('Backspace')
  assert('backspace-preserves-next-line-ending', await text(), '甲乙\n末')
  await page.keyboard.press('Meta+z')
  assert('backspace-undo-preserves-all-endings', await text(), '甲\r\n乙\n末')
  await page.keyboard.press('Meta+Shift+z')
  assert('backspace-redo-preserves-next-line-ending', await text(), '甲乙\n末')

  await setup('甲\r\n乙\n末', 1)
  await page.keyboard.press('Delete')
  assert('delete-preserves-next-line-ending', await text(), '甲乙\n末')

  await setup('甲\r\n乙丙\n末', 4)
  await page.keyboard.press('Enter')
  assert('enter-uses-new-lf-only', await text(), '甲\r\n乙\n丙\n末')
  await page.keyboard.press('Meta+z')
  assert('enter-undo', await text(), '甲\r\n乙丙\n末')

  await setup('甲\r\n乙丙\n末', 4)
  await page.evaluate(() => window.poc.editor.commands.toggleHeading({level: 2}))
  assert('heading-preserves-text', await text(), '甲\r\n乙丙\n末')
  await page.evaluate(() => window.poc.editor.commands.toggleBulletList())
  assert('list-preserves-text', await text(), '甲\r\n乙丙\n末')
  await page.keyboard.press('Enter')
  assert('list-enter', await text(), '甲\r\n乙\n丙\n末')

  await setup('甲\r\n乙\n末', 1)
  await page.evaluate(() => {
    const dt = new DataTransfer()
    dt.setData('text/plain', '新甲\r\n新乙\n新丙')
    window.poc.editor.view.dom.dispatchEvent(new ClipboardEvent('paste', {bubbles:true, cancelable:true, clipboardData:dt}))
  })
  assert('paste-mixed-endings', await text(), '甲新甲\r\n新乙\n新丙\r\n乙\n末')

  await setup('甲\r\n乙丙\n末', 4)
  await page.evaluate(() => {window.poc.editor.commands.setTextSelection(5);window.poc.insertSection('x')})
  assert('section-insert-preserves-prose', await text(), '甲\r\n乙丙\n末')
  const sides = await page.evaluate(() => window.poc.toPm(4))
  assert('same-offset-two-section-sides', sides.length, 2)
  await page.evaluate((pos) => {window.poc.editor.commands.focus();window.poc.editor.commands.setTextSelection(pos)}, sides[1])
  await page.keyboard.press('Backspace')
  await page.keyboard.press('Backspace')
  assert('section-implicit-delete-prevented', await page.locator('.section').count(), 1)
  assert('section-implicit-delete-preserves-prose', await text(), '甲\r\n乙丙\n末')
  const before = await page.evaluate(() => window.poc.editor.getJSON())
  await page.evaluate(() => {
    window.poc.editor.commands.setTextSelection({from:4,to:14})
    const dt = new DataTransfer(); dt.setData('text/plain', '替换')
    window.poc.editor.view.dom.dispatchEvent(new ClipboardEvent('paste', {bubbles:true, cancelable:true, clipboardData:dt}))
  })
  assert('cross-section-paste-rejected', await page.evaluate(() => window.poc.editor.getJSON()), before)
  await page.getByRole('button', {name:'拖动区块'}).dragTo(page.getByText('末', {exact:true}))
  assert('section-drag-preserves-prose', await text(), '甲\r\n乙丙\n末')
  assert('section-drag-preserves-identity', await page.evaluate(() => window.poc.editor.getJSON().content.filter(n=>n.type==='section').map(n=>n.attrs.id)), ['x'])

  await setup('甲\r\n乙丙\n末', 4)
  await page.evaluate(() => {window.poc.mark(4,5,'m1');window.poc.breakHistory();window.poc.editor.commands.focus();window.poc.editor.commands.setTextSelection({from:4,to:5})})
  await page.keyboard.press('Backspace')
  assert('deleted-mark-stale', await page.evaluate(() => window.poc.editor.getJSON().attrs.markers), [{id:'m1',status:'stale'}])
  await page.keyboard.press('Meta+z')
  assert('mark-undo-restores-text', await text(), '甲\r\n乙丙\n末')
  assert('mark-undo-restores-active', await page.evaluate(() => window.poc.editor.getJSON().attrs.markers), [{id:'m1',status:'active'}])
  await page.keyboard.press('Meta+Shift+z')
  assert('mark-redo-text', await text(), '甲\r\n丙\n末')
  assert('mark-redo-stale', await page.evaluate(() => window.poc.editor.getJSON().attrs.markers), [{id:'m1',status:'stale'}])

  await setup('甲\r\n乙\n末', 4)
  await page.evaluate(() => {window.ime=[];const el=window.poc.editor.view.dom;el.addEventListener('compositionend', e=>window.ime.push(e.data), {once:true})})
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('Input.imeSetComposition', {text:'pin',selectionStart:3,selectionEnd:3})
  await cdp.send('Input.imeSetComposition', {text:'拼音',selectionStart:2,selectionEnd:2})
  await cdp.send('Input.insertText', {text:'拼音'})
  assert('ime-composition-events', await page.evaluate(() => window.ime), ['拼音'])
  assert('ime-preserves-neighbor-endings', await text(), '甲\r\n乙拼音\n末')
  await cdp.detach()

  await setup('安全')
  const bad = await page.evaluate(() => {try {window.poc.editor.commands.setContent({type:'doc',content:[{type:'unknown',content:[{type:'text',text:'丢失'}]}]},{errorOnInvalidContent:true});return false} catch {return true}})
  assert('invalid-schema-rejected', bad, true)
  assert('invalid-schema-keeps-original', await text(), '安全')
  return {passed: checks.length, checks}
}
