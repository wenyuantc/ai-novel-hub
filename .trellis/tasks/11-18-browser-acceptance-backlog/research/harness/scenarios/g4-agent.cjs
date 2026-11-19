// G4 智能体前端（E4i-h）：D-1 至 D-5，以及 D-extra（输入框的可访问角色）。
// D-3、D-4 由作者在独立浏览器上下文经接口改变编辑者权限 / 模型授权，编辑者页面不刷新。
// 运行：QA_DIR=<dir> node scenarios/g4-agent.cjs        （只跑部分：ONLY=D1,D3）
'use strict'
const lib = require('../lib.cjs')
const ag = require('./agent-common.cjs')

const PERM_CHANGED = '作品权限已变化，请重新读取对话。'
const only = (id) => !process.env.ONLY || process.env.ONLY.split(',').includes(id)

/** 页面上的错误类提示（权限变化 / 网络错误 / 任何 error Alert）。 */
async function errorNotices(page) {
  const alerts = await page.locator('.ant-alert-error, .ant-message-error').allInnerTexts()
  const perm = await page.getByText(PERM_CHANGED).count()
  return { alerts: alerts.map((t) => t.replace(/\s+/g, ' ').slice(0, 80)), perm }
}

/** 会话最近一轮的任务（取消息里最大的 turn_task_id，再从任务列表读状态）。 */
async function lastTurn(api, pid, cid) {
  const d = await api.get(`/projects/${pid}/agent/conversations/${cid}`)
  const ids = d.messages.map((m) => m.turn_task_id).filter(Boolean)
  if (!ids.length) return null
  const id = Math.max(...ids)
  const list = await api.get(`/tasks?project_id=${pid}&limit=50`)
  return (list.items || list).find((t) => t.id === id) || { id, status: '（列表中未找到）' }
}

async function member(api, pid, username) {
  return (await api.get(`/projects/${pid}/collaborators`)).find((m) => m.username === username)
}
async function patchMember(api, pid, username, body) {
  const m = await member(api, pid, username)
  return api.patch(`/projects/${pid}/collaborators/${m.user_id}`, { expected_revision: m.revision, ...body })
}

/** 连续发送：每条发送后立即读输入框（受理后应清空），并在中途切后台 / 切页。 */
async function sendSeries(env, page, api, pid, label) {
  const cid = await ag.openNewConversation(page, api, pid)
  const box = page.locator(ag.BOX)
  const log = []
  const msgs = ['你好，先看看这本书', '第 5 章的冲突点在哪里', '再列一下主要角色', '最后给点修改建议', '谢谢']
  for (let i = 0; i < msgs.length; i += 1) {
    await box.fill(msgs[i])
    await page.getByRole('button', { name: /发\s*送/ }).click()
    // 受理：输入框清空（最多等 3 秒）
    let cleared = false
    for (let k = 0; k < 30 && !cleared; k += 1) { cleared = (await box.inputValue()) === ''; if (!cleared) await lib.sleep(100) }
    let extra = ''
    if (i === 1) {
      // 切到后台再切回：同上下文开一个新标签页并前置，2 秒后切回
      const other = await page.context().newPage()
      await other.goto('/projects'); await other.bringToFront(); await lib.sleep(2000)
      await page.bringToFront(); await other.close(); extra = '（发送后切到其他标签页 2 秒再切回）'
    }
    if (i === 3) {
      // 发送后立即切页（作品内导航，智能体面板随布局保留）
      await page.locator(`a[href="/projects/${pid}/chapters"]`).first().click()
      extra = '（发送后立即切到「章节」页）'
    }
    await ag.waitTurn(api, pid, cid, { within: 60000 })
    await page.waitForTimeout(600)
    const notices = await errorNotices(page)
    log.push({ i: i + 1, cleared, extra, notices })
  }
  const detail = await api.get(`/projects/${pid}/agent/conversations/${cid}`)
  const users = detail.messages.filter((m) => m.role === 'user').length
  await lib.shot(env, page, `g4-${label}-series`)
  return { cid, log, users }
}

async function main() {
  const env = lib.loadEnv()
  const r = lib.reporter('g4-agent')
  const browser = await lib.launch()
  const pid = env.fixtures.main_project.id
  try {
    const author = await lib.contextAs(browser, env, 'qa_author')
    await lib.clearScripts(env)
    await lib.setDelay(env, { first_byte: 0, per_chunk: 0 })
    // 保证编辑者处于初始状态（编辑者 + 作者模型授权）
    const m0 = await member(author.api, pid, 'qa_editor')
    if (m0.role !== 'editor' || !m0.allow_owner_ai) await patchMember(author.api, pid, 'qa_editor', { role: 'editor', allow_owner_ai: true })

    // ---------- D-1 编辑者 / D-2 作者：连续发送 ----------
    for (const [id, who] of [['D-1', 'qa_editor'], ['D-2', 'qa_author']]) {
      if (!only(id.replace('-', ''))) continue
      const ctx = who === 'qa_author' ? author : await lib.contextAs(browser, env, who)
      // 假模型首字节延迟 0.6 秒，使受理与流式回复在时间上可区分
      await lib.setDelay(env, { first_byte: 0.6, per_chunk: 0.05, kinds: ['agent'] })
      const res = await sendSeries(env, ctx.page, ctx.api, pid, id)
      await lib.setDelay(env, { first_byte: 0, per_chunk: 0 })
      const bad = res.log.filter((x) => !x.cleared || x.notices.perm || x.notices.alerts.length)
      r.check(id, bad.length === 0 && res.users === 5,
        `${who} 连发 5 条：${res.log.map((x) => `#${x.i}${x.extra}清空=${x.cleared}${x.notices.perm ? ' 权限变化提示!' : ''}${x.notices.alerts.length ? ` 错误=${x.notices.alerts.join('/')}` : ''}`).join('；')}；`
        + `服务端用户消息 ${res.users} 条`)
      if (who !== 'qa_author') await ctx.context.close()
    }

    // ---------- D-3：发送前被降为查看者（页面不刷新） ----------
    if (only('D3')) {
      const ed = await lib.contextAs(browser, env, 'qa_editor')
      const cid = await ag.openNewConversation(ed.page, ed.api, pid)
      const box = ed.page.locator(ag.BOX)
      const text = '把第 3 章标题改成「茶棚夜话」'
      const sends = []
      ed.page.on('response', async (res) => {
        if (res.request().method() === 'POST' && /\/agent\/conversations\/\d+\/messages$/.test(new URL(res.url()).pathname)) {
          sends.push(`${res.status()} ${(await res.text().catch(() => '')).slice(0, 120)}`)
        }
      })
      await box.fill(text)
      await patchMember(author.api, pid, 'qa_editor', { role: 'viewer' })
      const boxAtClick = await box.count()
      let clicked = true
      await ed.page.getByRole('button', { name: /发\s*送/ }).click({ timeout: 3000 }).catch(() => { clicked = false })
      await ed.page.waitForTimeout(300)
      const keptRightAfter = await box.count() ? await box.inputValue().catch(() => '（无）') : '（输入框已移除）'
      await ed.page.waitForTimeout(3000)
      const kept = await box.count() ? await box.inputValue().catch(() => '（输入框已移除）') : '（输入框已移除）'
      const notices = await errorNotices(ed.page)
      const readonly = await ed.page.getByText('此会话仅供查看').count()
      const sendBtn = await ed.page.getByRole('button', { name: /发\s*送/ }).count()
      const pageText = await ed.page.locator('body').innerText()
      const viewerBanner = pageText.includes('当前为查看者')
      const detail = await author.api.get(`/projects/${pid}/agent/conversations/${cid}`).catch((e) => ({ error: e.message.slice(0, 60) }))
      const userMsgs = detail.messages ? detail.messages.filter((m) => m.role === 'user').length : 'n/a（作者无权读编辑者会话）'
      await lib.shot(env, ed.page, 'g4-D-3')
      // 场景表预期「输入保留不清空」：输入区被移除时记为失败（见 verification-g7-g4.md D-3）
      r.check('D-3', kept === text && (notices.perm > 0 || notices.alerts.length > 0 || readonly > 0),
        `作者降级后编辑者点发送：输入框内容「${kept}」；权限变化提示 ${notices.perm}；错误条 ${JSON.stringify(notices.alerts)}；`
        + `「此会话仅供查看」${readonly}；发送按钮 ${sendBtn}；页头查看者提示 ${viewerBanner}；会话内用户消息 ${userMsgs}；`
        + `点击时输入框存在 ${boxAtClick}、点击成功 ${clicked}；发送请求 ${JSON.stringify(sends)}；点击后 0.3 秒输入框「${keptRightAfter}」`)
      await patchMember(author.api, pid, 'qa_editor', { role: 'editor', allow_owner_ai: true })
      await ed.context.close()
    }

    // ---------- D-4：待确认改名提案，作者关闭模型授权（页面不刷新） ----------
    if (only('D4')) {
      const ed = await lib.contextAs(browser, env, 'qa_editor')
      const cid = await ag.openNewConversation(ed.page, ed.api, pid)
      const ch3 = env.fixtures.main_project.chapters['3']
      await lib.clearScripts(env)
      await lib.pushScript(env, { match: 'tools', type: 'tool_calls', tool_calls: [
        { name: 'search_knowledge', arguments: { query: '茶棚' } },
        { name: 'update_chapter_meta', arguments: { id: ch3, title: '茶棚夜话' } }] })
      await ag.typeAndSend(ed.page, '查一下茶棚相关内容，再把第 3 章标题改成「茶棚夜话」')
      const paused = await ag.waitTurn(ed.api, pid, cid, { until: 'paused', within: 30000 }).catch((e) => ({ error: e.message }))
      await ed.page.waitForTimeout(800)
      const before = await ed.page.locator('summary').allInnerTexts()
      const proposalBefore = await ed.page.locator('.ant-card').filter({ hasText: '修改章节标题' }).first().innerText().catch(() => '')
      await lib.shot(env, ed.page, 'g4-D-4-paused')
      await patchMember(author.api, pid, 'qa_editor', { allow_owner_ai: false })
      // 不刷新，等权限快照轮询与进度流把作废带到界面
      let after = []
      let voided = 0
      for (let i = 0; i < 40; i += 1) {
        after = await ed.page.getByText(/修改章节标题 · /).allInnerTexts()
        voided = await ed.page.getByText('已作废').count()
        if (voided && after.some((t) => t.includes('已停止'))) break
        await ed.page.waitForTimeout(500)
      }
      const stepSummary = ed.page.locator('summary').filter({ hasText: /修改章节标题 · / }).first()
      let expanded = ''
      if (await stepSummary.count()) { await stepSummary.click(); expanded = await stepSummary.locator('xpath=..').locator('pre').innerText() }
      const processing = await ed.page.getByText(/ · 处理中/).count()
      await lib.shot(env, ed.page, 'g4-D-4-voided')
      r.check('D-4', voided > 0 && after.some((t) => t.includes('已停止')) && expanded.includes('本轮已结束，此步骤没有返回结果。') && processing === 0,
        `暂停时：轮次 ${paused.error ? paused.error : paused.active_task?.status}；步骤 ${JSON.stringify(before)}；提案卡片「${proposalBefore.replace(/\s+/g, ' ').slice(0, 60)}」；`
        + `作者关闭模型授权后（未刷新）：「已作废」${voided}；步骤 ${JSON.stringify(after)}；展开「${expanded}」；残留「处理中」${processing}`)
      await patchMember(author.api, pid, 'qa_editor', { allow_owner_ai: true })
      await ed.context.close()
    }

    // ---------- D-5：作者轮次——待确认、取消、模型失败 ----------
    if (only('D5')) {
      const { page, api } = author
      const ch4 = env.fixtures.main_project.chapters['4']
      let cid = await ag.openNewConversation(page, api, pid)
      await lib.clearScripts(env)
      await lib.pushScript(env, { match: 'tools', type: 'tool_calls', tool_calls: [
        { name: 'get_project_overview', arguments: {} },
        { name: 'update_chapter_meta', arguments: { id: ch4, title: '第四回·改' } }] })
      await ag.typeAndSend(page, '看看概况，再把第 4 章标题改一下')
      await ag.waitTurn(api, pid, cid, { until: 'paused', within: 30000 })
      await page.waitForTimeout(800)
      const pausedSteps = await page.locator('summary').allInnerTexts()
      await lib.shot(env, page, 'g4-D-5-paused')
      // 取消这一轮
      await page.getByRole('status').getByRole('button', { name: /停\s*止/ }).click()
      await page.waitForTimeout(500)
      await lib.shot(env, page, 'g4-D-5-stop-confirm')
      await page.locator('.ant-popconfirm:visible, .ant-popover:visible').last().locator('button.ant-btn-primary').click()
      await ag.waitTurn(api, pid, cid, { within: 30000 })
      await page.waitForTimeout(1500)
      const cancelledSteps = await page.locator('summary').allInnerTexts()
      const cancelledTask = await lastTurn(api, pid, cid)
      await lib.shot(env, page, 'g4-D-5-cancelled')

      // 失败：先确认一项提案（步骤因此有结果），确认后续跑时假模型 503
      cid = await ag.openNewConversation(page, api, pid)
      await lib.clearScripts(env)
      await lib.pushScript(env, [
        { match: 'tools', type: 'tool_calls', tool_calls: [{ name: 'get_project_overview', arguments: {} }, { name: 'update_chapter_meta', arguments: { id: ch4, title: '第四回·再改' } }] },
        { match: 'tools', type: 'error', status: 503, times: 10 }])
      await ag.typeAndSend(page, '看看概况，再改第 4 章标题')
      await ag.waitTurn(api, pid, cid, { until: 'paused', within: 30000 })
      await page.waitForTimeout(800)
      await page.locator('.ant-card').filter({ hasText: '修改章节标题' }).last().getByRole('button', { name: /^确\s*认$/ }).click()
      await ag.waitTurn(api, pid, cid, { within: 90000 })
      await lib.clearScripts(env)
      await page.waitForTimeout(1500)
      const failedSteps = await page.locator('summary').allInnerTexts()
      const failedTask = await lastTurn(api, pid, cid)
      const failNotice = await page.locator('.ant-alert-warning, .ant-alert-error').allInnerTexts()
      await lib.shot(env, page, 'g4-D-5-failed')

      const pausedOk = pausedSteps.some((t) => t === '读取项目概览 · 完成') && pausedSteps.some((t) => t === '修改章节标题 · 处理中')
      const cancelOk = cancelledTask?.status === 'cancelled' && cancelledSteps.some((t) => t === '修改章节标题 · 已停止') && !cancelledSteps.some((t) => t.includes('处理中'))
      const failOk = failedTask?.status === 'failed' && !failedSteps.some((t) => t.includes('处理中'))
      r.check('D-5', pausedOk && cancelOk && failOk,
        `待确认时步骤 ${JSON.stringify(pausedSteps)}；取消后轮次 ${cancelledTask?.status}，步骤 ${JSON.stringify(cancelledSteps)}；`
        + `确认后续跑 503：轮次 ${failedTask?.status}，步骤 ${JSON.stringify(failedSteps)}，提示 ${JSON.stringify(failNotice.map((t) => t.replace(/\s+/g, ' ').slice(0, 60)))}`)
    }

    // ---------- D-extra：输入框的可访问角色 ----------
    if (only('Dx')) {
      const { page, api } = author
      await ag.openNewConversation(page, api, pid)
      await page.waitForTimeout(1500)
      const tb = await page.getByRole('textbox', { name: '发给项目智能体的消息' }).count()
      const cb = await page.getByRole('combobox', { name: '发给项目智能体的消息' }).count()
      const attrs = await page.locator(ag.BOX).evaluate((e) => ['role', 'aria-autocomplete', 'aria-haspopup', 'aria-expanded', 'type', 'aria-multiline']
        .map((a) => `${a}=${e.getAttribute(a)}`).join(' '))
      const snap = await page.locator(ag.BOX).ariaSnapshot()
      r.note('D-extra', `getByRole(textbox)=${tb}，getByRole(combobox)=${cb}；属性 ${attrs}；ariaSnapshot「${snap}」`)
    }

    r.note('transient-404', `脚本轮询会话详情时遇到 404 ${ag.transient404.length} 次：${JSON.stringify(ag.transient404.slice(0, 5))}`)
    const errs = author.page._qaConsole.filter((x) => !x.includes('warning'))
    r.note('console', errs.length ? errs.slice(0, 5).join(' | ') : '无控制台错误')
  } finally {
    await lib.clearScripts(env).catch(() => {})
    await lib.setDelay(env, { first_byte: 0, per_chunk: 0 }).catch(() => {})
    await browser.close()
  }
  process.exit(r.save(env) ? 1 : 0)
}

main().catch((err) => { console.error(err); process.exit(2) })
