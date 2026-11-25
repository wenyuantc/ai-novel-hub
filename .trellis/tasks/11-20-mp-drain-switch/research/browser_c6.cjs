// 由 process_c6.py 启动，使用隔离应用和临时账号验收真实 Chrome 的排空交互。
'use strict'
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright')

const detail = '系统正在维护，暂不受理新任务，请稍后再试'
const settingName = '停止受理新任务'
const settingsPath = '/api/v1/admin/system-settings'
const statePath = settingsPath + '/drain-status'
const editorDraft = '保留这一条润色要求和原有正文'
const batchDraft = '保留整批的额外写作要求'
const agentDraft = '开启排空时不能提交，但保留这段提问'

async function session(browser, origin, credentials, viewport) {
  const context = await browser.newContext({ baseURL: origin, locale: 'zh-CN', viewport })
  const login = await context.request.post('/api/v1/auth/login', { data: credentials })
  assert.equal(login.status(), 200)
  const page = await context.newPage()
  const pageErrors = []
  page.on('pageerror', (error) => pageErrors.push(error.message))
  return { context, page, pageErrors }
}

async function responseTo(page, method, matchPath, act) {
  const [response] = await Promise.all([
    page.waitForResponse((item) => item.request().method() === method && matchPath(new URL(item.url()).pathname)),
    act(),
  ])
  return response
}

async function expectMaintenance(page, matchPath, click) {
  const response = await responseTo(page, 'POST', matchPath, click)
  assert.equal(response.status(), 503)
  assert.deepEqual(await response.json(), { code: 'task_admission_closed', detail })
  await page.getByText(detail, { exact: true }).first().waitFor()
}

async function saveSwitch(page, enabled) {
  const toggle = page.getByRole('switch', { name: settingName })
  await toggle.click()
  assert.equal(await toggle.getAttribute('aria-checked'), String(enabled))
  const response = await responseTo(page, 'PATCH', (url) => url === settingsPath,
    () => page.getByRole('button', { name: '保存设置' }).click())
  assert.equal(response.status(), 200)
  const { items } = await response.json()
  assert.equal(items.find(({ key }) => key === 'task_admission_closed').value, enabled)
}

async function exercise(directory) {
  const fixture = JSON.parse(fs.readFileSync(path.join(directory, 'browser-fixtures.json'), 'utf8'))
  const browser = await chromium.launch({ channel: 'chrome', headless: true })
  const desktop = { width: 1440, height: 940 }
  const mobile = { width: 375, height: 812 }
  const screenshotDirectory = process.env.C6_BROWSER_OUTPUT || directory
  const sessions = []
  console.log(`Chrome ${browser.version()}`)
  try {
    const admin = await session(browser, fixture.adminOrigin, fixture.admin, desktop)
    const reader = await session(browser, fixture.userOrigin, fixture.reader, mobile)
    const author = await session(browser, fixture.userOrigin, fixture.author, desktop)
    sessions.push(['admin', admin], ['reader', reader], ['author', author])

    await admin.page.goto('/admin/system-settings')
    await admin.page.getByRole('heading', { name: '任务排空状态' }).waitFor()
    await admin.page.getByText('正在受理新任务', { exact: true }).waitFor()
    await saveSwitch(admin.page, true)

    // 只读账号用另一进程，验证状态随后台更新且窄屏不横向溢出。
    assert.equal((await reader.context.request.get(settingsPath)).status(), 200)
    assert.equal((await reader.context.request.get(statePath)).status(), 200)
    await reader.page.goto('/admin/system-settings')
    const readonlyToggle = reader.page.getByRole('switch', { name: settingName })
    await readonlyToggle.waitFor()
    assert.equal(await readonlyToggle.getAttribute('aria-checked'), 'true')
    assert.equal(await readonlyToggle.isDisabled(), true)
    await reader.page.getByText('已排空', { exact: true }).waitFor()
    assert.equal(await reader.page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true)
    const status = reader.page.getByRole('heading', { name: '任务排空状态' })
    await status.scrollIntoViewIfNeeded()
    await reader.page.screenshot({ path: path.join(screenshotDirectory, 'c6-settings-mobile.png') })
    await admin.page.getByRole('heading', { name: '任务排空状态' }).scrollIntoViewIfNeeded()
    await admin.page.screenshot({ path: path.join(screenshotDirectory, 'c6-settings-desktop.png') })

    const project = `/projects/${fixture.project}`
    const { page } = author
    await page.goto(`${project}/chapters/${fixture.chapter}`)
    await page.getByRole('tab', { name: 'AI 助手' }).click()
    await page.getByRole('tab', { name: 'AI 润色' }).click()
    const polishInput = page.getByPlaceholder('例如：增强画面感，精简对话，统一人称')
    await polishInput.fill(editorDraft)
    await expectMaintenance(page, (url) => url.endsWith('/polish'),
      () => page.getByRole('button', { name: /润色当前场景|逐场景润色整章/ }).click())
    assert.equal(await polishInput.inputValue(), editorDraft)

    await page.goto(`${project}/chapters`)
    await page.getByRole('button', { name: '批量生成' }).click()
    const dialog = page.getByRole('dialog', { name: '批量生成章节' })
    const batch = dialog.getByPlaceholder('例如：保持紧凑节奏，多用对话推进')
    await batch.fill(batchDraft)
    await expectMaintenance(page, (url) => url.endsWith('/batch-generate'),
      () => dialog.getByRole('button', { name: '开始批量生成' }).click())
    assert.equal(await dialog.isVisible(), true)
    assert.equal(await batch.inputValue(), batchDraft)

    await page.goto(`${project}/overview`)
    const agent = page.getByRole('button', { name: '项目智能体' })
    await agent.waitFor()
    if ((await agent.getAttribute('aria-expanded')) !== 'true') await agent.click()
    await page.getByRole('button', { name: '开始新会话' }).click()
    const message = page.getByLabel('发给项目智能体的消息')
    await message.fill(agentDraft)
    const send = page.getByRole('button', { name: /发\s*送/ })
    await expectMaintenance(page, (url) => url.endsWith('/messages'), () => send.click())
    assert.equal(await message.inputValue(), agentDraft)

    // 同一个页面中的输入在恢复受理后可以原样再次发送。
    await saveSwitch(admin.page, false)
    await reader.page.getByText('正在受理新任务', { exact: true }).waitFor()
    await readonlyToggle.and(reader.page.locator('[aria-checked=false]')).waitFor()
    const accepted = await responseTo(page, 'POST', (url) => url.endsWith('/messages'), () => send.click())
    assert.equal(accepted.status(), 202)
    const firstReply = page.getByText('恢复之后，智能体收到保留的消息。', { exact: true })
    await firstReply.waitFor()
    assert.equal(await message.inputValue(), '')

    const created = await responseTo(page, 'POST', (url) => url.endsWith('/conversations'),
      () => page.getByRole('button', { name: '新建会话' }).click())
    assert.equal(created.status(), 201)
    await firstReply.waitFor({ state: 'hidden' })
    const autoExecute = page.getByRole('switch', { name: '自动执行小修改' })
    await autoExecute.waitFor()
    if (await autoExecute.getAttribute('aria-checked') === 'true') await autoExecute.click()
    await message.fill(fixture.reviewPrompt)
    const proposed = await responseTo(page, 'POST', (url) => url.endsWith('/messages'), () => send.click())
    assert.equal(proposed.status(), 202)
    const reject = page.getByRole('button', { name: /^拒\s*绝$/ })
    await reject.waitFor()
    await saveSwitch(admin.page, true)
    const rejected = await responseTo(page, 'POST', (url) => url.endsWith('/reject'), () => reject.click())
    assert.equal(rejected.status(), 200)
    const review = await rejected.json()
    assert.equal(review.status, 'rejected')
    const resume = page.getByRole('button', { name: '继续本轮' })
    await expectMaintenance(page, (url) => url.endsWith('/resume'), () => resume.click())
    await saveSwitch(admin.page, false)
    const resumed = await responseTo(page, 'POST', (url) => url.endsWith('/resume'), () => resume.click())
    assert.equal(resumed.status(), 202)
    await page.getByText('这个提案已经拒绝，轮次继续。', { exact: true }).waitFor()

    for (const view of [admin, reader, author]) assert.deepEqual(view.pageErrors, [])
    console.log('管理页、375px 只读页、章节润色、批量、智能体拒绝、恢复和手动续跑均通过。')
  } catch (error) {
    for (const [name, { page, pageErrors }] of sessions) {
      console.error(name, new URL(page.url()).pathname, pageErrors,
        (await page.locator('body').innerText().catch(() => '')).slice(-1600))
      await page.screenshot({ path: path.join(screenshotDirectory, `c6-${name}-failure.png`) }).catch(() => {})
    }
    throw error
  } finally {
    await browser.close()
  }
}

exercise(process.argv[2]).catch((error) => {
  console.error(error)
  process.exitCode = 1
})
