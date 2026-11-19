// G3 系列设定协作者 C-1 至 C-7（场景表 research/targets-consistency-collab.md C5）。
// 运行：QA_DIR=<dir> node scenarios/g3-series.cjs
// 会改动隔离库：新注册 g3_inv、新建共享条目、撤销 qa_series 的系列身份（C-7 最后一步，不可重复执行到同一库）。
'use strict'
const lib = require('../lib.cjs')

const body = (page) => page.locator('body').innerText()
const has = async (page, text) => (await body(page)).includes(text)
const count = (loc) => loc.count()
const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
/** AntD 两字按钮会插入空格：按逐字间允许空白匹配整段名称。 */
const B = (t) => ({ name: new RegExp('^(?:[a-z-]+\\s+)?\\s*' + [...t].map(esc).join('\\s*') + '\\s*$') })

async function register(env, username) {
  const res = await fetch(`${env.origin}/api/v1/auth/register`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password: lib.QA_PASSWORD }),
  })
  return res.status
}

async function status(api, method, url, data) {
  const res = await api.raw(method, url, data, [200, 201, 202, 204, 400, 401, 403, 404, 409, 422])
  return res
}

/** 返回状态码与错误码（不抛出）。 */
async function probe(ctx, method, url, data) {
  const res = await ctx.context.request.fetch(`/api/v1${url}`, { method, data })
  let code = ''
  try { const j = JSON.parse(await res.text()); code = j.code || j.detail?.code || (typeof j.detail === 'string' ? j.detail : '') } catch { /* 非 JSON */ }
  return `${res.status()}${code ? ` ${code}` : ''}`
}

async function main() {
  const env = lib.loadEnv()
  const fx = env.fixtures
  const sid = fx.series.id
  const mainPid = fx.main_project.id
  const bookB = fx.book_b.id
  const adoptedId = fx.series.entry_adopted.id
  const unadoptedId = fx.series.entry_unadopted.id
  const r = lib.reporter('g3-series')
  const browser = await lib.launch()
  const pages = []
  try {
    // ---------- C-1 作者邀请（界面） ----------
    const invitee = `g3_inv_${Date.now() % 100000}`
    r.note('C-1 setup', `注册新账号 ${invitee}：${await register(env, invitee)}`)
    const author = await lib.contextAs(browser, env, 'qa_author')
    pages.push(author.page)
    const ap = author.page
    // 可重复执行：恢复 qa_series 的系列身份与未采用条目「临江城」
    const members = await author.api.get(`/series/${sid}/collaborators`)
    const qs = members.find((mm) => mm.username === 'qa_series')
    if (!qs || !['pending', 'accepted'].includes(qs.status)) {
      await author.api.post(`/series/${sid}/collaborators`, { username: 'qa_series' })
    }
    if (!qs || qs.status !== 'accepted') {
      const c = await browser.newContext({ baseURL: env.origin })
      await c.request.post('/api/v1/auth/login', { data: { username: 'qa_series', password: lib.QA_PASSWORD } })
      const a = lib.apiFor(c.request, 'qa_series')
      for (const i of await a.get('/collaboration/invitations')) {
        await a.post(`/collaboration/invitations/${i.scope}/${i.invitation_id}/accept`, { expected_revision: i.revision })
      }
      await c.close()
      r.note('setup', '已恢复 qa_series 的系列身份')
    }
    const allEntries = await author.api.get(`/series/${sid}/entries?page=1&page_size=100`)
    if (!allEntries.items.some((e) => e.kind === 'world' && e.data.title === '临江城')) {
      await author.api.post(`/series/${sid}/entries`, { expected_revision: allEntries.revision, kind: 'world',
        data: { title: '临江城', category: 'place', content: '江畔大城，尚未被任何作品采用。', aliases: [] } })
      r.note('setup', '已重建未采用条目「临江城」')
    }
    await ap.goto(`/series/${sid}`)
    const panel = ap.locator('section[aria-label="系列成员"]')
    await panel.waitFor({ timeout: 15000 })
    const heading = await panel.locator('h3').innerText()
    const roleSelects = await count(panel.getByLabel('邀请角色'))
    const aiChecks = await count(panel.getByText('允许使用作者模型配置'))
    await panel.getByLabel('邀请用户名').fill(invitee)
    await panel.getByRole('button', B('邀请')).click()
    const dlg = ap.locator('.ant-modal').filter({ hasText: '确认发送邀请' })
    await dlg.waitFor()
    const dlgText = (await dlg.innerText()).replace(/\s+/g, ' ')
    await dlg.getByRole('button', B('发送邀请')).click()
    const row = panel.locator('li.collaboration-member').filter({ hasText: invitee })
    await row.waitFor({ timeout: 10000 })
    const rowText = (await row.innerText()).replace(/\s+/g, ' ')
    await lib.shot(env, ap, 'g3-c1-invite')
    r.check('C-1', heading === '系列设定协作者' && roleSelects === 0 && aiChecks === 0
      && dlgText.includes(`邀请「${invitee}」成为系列设定协作者？`) && rowText.includes('设定协作者 · 待接受'),
    `面板标题「${heading}」；角色下拉 ${roleSelects} 个、模型授权复选框 ${aiChecks} 个；确认框「${dlgText.slice(0, 60)}」；成员行「${rowText}」`)

    // ---------- C-2 受邀人接受（界面） ----------
    const inv = await lib.loginViaPage(browser, env, invitee)
    pages.push(inv.page)
    const ip = inv.page
    await ip.goto('/projects')
    const invSec = ip.locator('section[aria-label="待处理邀请"]')
    await invSec.waitFor({ timeout: 15000 })
    await invSec.getByText('设定协作者').first().waitFor({ timeout: 15000 })
    const invText = (await invSec.innerText()).replace(/\s+/g, ' ')
    await lib.shot(env, ip, 'g3-c2-invitation')
    await invSec.getByRole('button', B('接受')).first().click()
    const acc = ip.locator('.ant-modal').filter({ hasText: '确认接受邀请' })
    await acc.getByRole('button', B('接受邀请')).click()
    await ip.getByText('邀请已接受。').first().waitFor({ timeout: 10000 }).catch(() => {})
    await ip.goto('/series')
    const item = ip.locator('.ant-list-item').filter({ hasText: '剑影系列' })
    await item.waitFor({ timeout: 15000 })
    const itemText = (await item.innerText()).replace(/\s+/g, ' ')
    await item.getByRole('link', { name: '剑影系列' }).click()
    await ip.getByText('系列共享设定').waitFor({ timeout: 15000 })
    const hint = await has(ip, '设定协作者可维护共享基础；作品访问权限由作者单独授予。')
    await lib.shot(env, ip, 'g3-c2-series-detail')
    const authorRow = (await panel.locator('li.collaboration-member').filter({ hasText: invitee }).innerText().catch(() => '')).replace(/\s+/g, ' ')
    r.check('C-2', invText.includes('系列 · 剑影系列') && invText.includes('设定协作者') && itemText.includes('设定协作者') && hint,
      `邀请卡「${invText.slice(0, 80)}」；系列列表项「${itemText}」；详情提示出现：${hint}`)
    r.note('C-2 author-row', `作者页未刷新时成员行：「${authorRow}」`)

    // ---------- C-3 设定协作者（无书籍权限）看到的页面 ----------
    const ser = await lib.contextAs(browser, env, 'qa_series')
    pages.push(ser.page)
    const sp = ser.page
    await sp.goto(`/series/${sid}`)
    await sp.getByText('系列共享设定').waitFor({ timeout: 15000 })
    await sp.waitForTimeout(800)
    const spText = await body(sp)
    const btn = async (name) => count(sp.getByRole('button', { name, exact: true }))
    const ownerButtons = { 重命名: await btn('重命名'), 加入作品: await btn('加入作品'), 退出系列: await btn('退出系列'), 删除空系列: await btn('删除空系列') }
    const memberPanel = await count(sp.locator('section[aria-label="系列成员"]'))
    const emptyBooks = spText.includes('没有可查看的成员作品')
    const leaksTitle = spText.includes('剑影长歌') || spText.includes('剑影续篇')
    await lib.shot(env, sp, 'g3-c3-series-collab')
    await sp.goto(`/projects/${mainPid}/overview`)
    await sp.getByText(/无法打开项目|没有读取此作品的权限|项目不存在/).first().waitFor({ timeout: 20000 }).catch(() => {})
    const directText = (await body(sp)).replace(/\s+/g, ' ')
    await lib.shot(env, sp, 'g3-c3-direct-book')
    const apiChecks = {
      'GET /projects/main': await probe(ser, 'GET', `/projects/${mainPid}`),
      'GET /projects/bookB/chapters': await probe(ser, 'GET', `/projects/${bookB}/chapters`),
      'PATCH /series(rename)': await probe(ser, 'PATCH', `/series/${sid}`, { name: 'x', expected_revision: 0 }),
      'POST /series/books': await probe(ser, 'POST', `/series/${sid}/books`, { project_id: mainPid, expected_revision: 0 }),
      'DELETE /series/books/bookB': await probe(ser, 'DELETE', `/series/${sid}/books/${bookB}?expected_revision=0`),
      'GET /series/collaborators': await probe(ser, 'GET', `/series/${sid}/collaborators`),
      'POST /series/collaborators': await probe(ser, 'POST', `/series/${sid}/collaborators`, { username: 'qa_viewer' }),
      'DELETE /series': await probe(ser, 'DELETE', `/series/${sid}?expected_revision=0`),
    }
    const apiDenied = Object.values(apiChecks).every((s) => /^(403|404)/.test(s))
    const directDenied = /无法打开项目|没有读取此作品的权限|不存在/.test(directText) && !directText.includes('第1回')
    r.check('C-3', Object.values(ownerButtons).every((n) => n === 0) && memberPanel === 0 && emptyBooks && !leaksTitle && directDenied && apiDenied,
      `作者按钮 ${JSON.stringify(ownerButtons)}；协作者面板 ${memberPanel}；「没有可查看的成员作品」${emptyBooks}；出现成员书标题 ${leaksTitle}；`
      + `直接访问主作品页面：「${directText.slice(-160)}」；接口 ${JSON.stringify(apiChecks)}`)

    // ---------- C-4 设定协作者新建共享角色、编辑共享基础 ----------
    // 先由作者给主作品中的「沈青」记一条本书状态，用来核对系列编辑不覆盖本书记录
    const mainChars = await author.api.get(`/projects/${mainPid}/characters?page_size=200`)
    const shenLocal = (mainChars.items || mainChars).find((c) => c.name === '沈青')
    const localDetail = await author.api.get(`/projects/${mainPid}/characters/${shenLocal.id}`)
    const stateText = '第3章后在临江城养伤（本书记录）'
    void localDetail
    await author.api.patch(`/projects/${mainPid}/characters/${shenLocal.id}`, { current_state: stateText })
      .catch((e) => r.note('C-4 setup', `写本书状态失败：${e.message.slice(0, 200)}`))
    await sp.goto(`/series/${sid}`)
    await sp.getByText('系列共享设定').waitFor({ timeout: 15000 })
    const newName = `顾长风${Date.now() % 1000}`
    await sp.getByRole('button', B('共享角色')).click()
    let m = sp.locator('.ant-modal').filter({ hasText: '新增共享角色' })
    await m.waitFor()
    await m.getByRole('textbox', { name: '姓名' }).fill(newName)
    await m.getByRole('textbox', { name: '简介' }).fill('系列协作者新建的共享角色')
    await m.getByRole('button', B('保存共享基础')).click()
    const createdOk = await sp.getByText('共享基础已保存').first().waitFor({ timeout: 10000 }).then(() => true, () => false)
    await sp.waitForTimeout(800)
    const newVisible = await has(sp, newName)
    // 编辑沈青共享简介
    const newProfile = `林远的同门师姐（设定协作者于 ${new Date().toISOString().slice(11, 19)} 修订）`
    const shenItem = sp.locator('.ant-list-item').filter({ hasText: '沈青' })
    await shenItem.getByRole('button', B('编辑共享基础')).click()
    m = sp.locator('.ant-modal').filter({ hasText: '编辑共享基础 · 沈青' })
    await m.waitFor()
    await m.getByRole('textbox', { name: '简介' }).fill(newProfile)
    await m.getByRole('button', B('保存共享基础')).click()
    const editOk = await sp.getByText('共享基础已保存').first().waitFor({ timeout: 10000 }).then(() => true, () => false)
    await lib.shot(env, sp, 'g3-c4-edited')
    await ap.goto(`/series/${sid}`)
    await ap.getByText('系列共享设定').waitFor({ timeout: 15000 })
    await ap.waitForTimeout(800)
    const authorSees = (await has(ap, newName)) && (await has(ap, newProfile))
    const after = await author.api.get(`/projects/${mainPid}/characters/${shenLocal.id}`)
    // 主作品角色页可见更新后的简介
    await ap.goto(`/projects/${mainPid}/characters`)
    await ap.getByText('沈青').first().waitFor({ timeout: 15000 })
    await ap.waitForTimeout(800)
    const charPageHasProfile = await has(ap, newProfile)
    await lib.shot(env, ap, 'g3-c4-author-book')
    r.check('C-4', createdOk && newVisible && editOk && authorSees && after.profile === newProfile && after.current_state === stateText,
      `新建「${newName}」保存提示 ${createdOk}、列表可见 ${newVisible}；编辑沈青保存 ${editOk}；作者系列页可见两处更新 ${authorSees}；`
      + `主作品本书投影 profile 已更新 ${after.profile === newProfile}；本书 current_state 保持「${after.current_state}」；主作品角色页可见新简介 ${charPageHasProfile}`)

    // ---------- C-5 已采用／未采用条目的删除 ----------
    await sp.goto(`/series/${sid}`)
    await sp.getByText('系列共享设定').waitFor({ timeout: 15000 })
    await sp.waitForTimeout(800)
    const shen = sp.locator('.ant-list-item').filter({ hasText: '沈青' })
    const shenDelDisabled = await shen.getByRole('button', B('删除')).isDisabled()
    const shenText = (await shen.innerText()).replace(/\s+/g, ' ')
    const shenHint = shenText.includes('已被作品采用，请系列作者处理删除。')
    const shenTag = shenText.includes('已有作品采用') && !/\d+ 本作品采用/.test(shenText)
    const lin = sp.locator('.ant-list-item').filter({ hasText: '临江城' })
    const linText = (await lin.innerText()).replace(/\s+/g, ' ')
    const linDelEnabled = await lin.getByRole('button', B('删除')).isEnabled()
    const apiDelAdopted = await probe(ser, 'DELETE', `/series/${sid}/entries/${adoptedId}?expected_revision=${(await ser.api.get(`/series/${sid}`)).revision}`)
    const apiImpactAdopted = await probe(ser, 'GET', `/series/${sid}/impact?action=delete&entry_id=${adoptedId}`)
    // 实际删除未采用条目（临江城）
    await lin.getByRole('button', B('删除')).click()
    m = sp.locator('.ant-modal').filter({ hasText: '删除共享设定 · 临江城' })
    await m.waitFor()
    await sp.waitForTimeout(1200)
    await m.locator('.ant-modal-footer').getByRole('button', B('删除共享设定')).click()
    const delOk = await sp.getByText('归属已更新').first().waitFor({ timeout: 10000 }).then(() => true, () => false)
    await sp.waitForTimeout(800)
    const linGone = !(await has(sp, '临江城'))
    await lib.shot(env, sp, 'g3-c5-delete')
    r.check('C-5', shenDelDisabled && shenHint && shenTag && linText.includes('尚未被作品采用') && linDelEnabled && delOk && linGone
      && /^409/.test(apiDelAdopted),
    `沈青：删除禁用 ${shenDelDisabled}、提示 ${shenHint}、标签不含本数 ${shenTag}（「${shenText.slice(0, 70)}」）；`
      + `临江城：「${linText.slice(0, 40)}」删除可用 ${linDelEnabled}、删除成功 ${delOk}、列表消失 ${linGone}；`
      + `接口直接删沈青 ${apiDelAdopted}，删除预检 ${apiImpactAdopted}`)

    // ---------- C-6 收回指定作品 ----------
    // 设定协作者兼续篇编辑者：新建一个未采用条目，收回到续篇
    const se = await lib.contextAs(browser, env, 'qa_series_editor')
    pages.push(se.page)
    const sep = se.page
    const moveName = `渡口石碑${Date.now() % 1000}`
    const created = await se.api.post(`/series/${sid}/entries`, {
      expected_revision: (await se.api.get(`/series/${sid}`)).revision, kind: 'world',
      data: { title: moveName, category: 'place', content: '续篇专属地标', aliases: [] },
    })
    const moveId = (created.item || created).id
    await sep.goto(`/series/${sid}`)
    await sep.getByText('系列共享设定').waitFor({ timeout: 15000 })
    await sep.waitForTimeout(800)
    const seBooks = (await body(sep)).includes('剑影续篇')
    const mvItem = sep.locator('.ant-list-item').filter({ hasText: moveName })
    await mvItem.getByRole('button', B('收回指定作品')).click()
    m = sep.locator('.ant-modal').filter({ hasText: `收回「${moveName}」` })
    await m.waitFor()
    await m.getByLabel('收回到哪部作品').first().click()
    await sep.waitForTimeout(800)
    const opts = await sep.locator('.ant-select-dropdown:visible .ant-select-item-option').allInnerTexts()
    await sep.locator('.ant-select-dropdown:visible .ant-select-item-option').filter({ hasText: '剑影续篇' }).click()
    await m.getByRole('button', B('检查影响')).click()
    const tm = sep.locator('.ant-modal').filter({ hasText: `收回指定作品 · ${moveName}` })
    await tm.waitFor()
    await sep.waitForTimeout(1200)
    await tm.locator('.ant-modal-footer').getByRole('button', B('收回指定作品')).click()
    const moveOk = await sep.getByText('归属已更新').first().waitFor({ timeout: 10000 }).then(() => true, () => false)
    const bWorld = await se.api.get(`/projects/${bookB}/world?page_size=200`).catch(() => null)
    const inBookB = JSON.stringify(bWorld || '').includes(moveName)
    await lib.shot(env, sep, 'g3-c6-moved')
    // 沈青只被主作品采用：系列编辑者没有主作品编辑权，接口应拒绝收回到续篇
    const seMoveShen = await probe(se, 'POST', `/series/${sid}/entries/${adoptedId}/move-to-book`, { project_id: bookB, expected_revision: (await se.api.get(`/series/${sid}`)).revision })
    // 仅系列权限者
    await sp.goto(`/series/${sid}`)
    await sp.getByText('系列共享设定').waitFor({ timeout: 15000 })
    await sp.waitForTimeout(800)
    const spMoveBtns = sp.getByRole('button', B('收回指定作品'))
    const spMoveDisabled = await spMoveBtns.first().isDisabled()
    let spOpts = []
    if (!spMoveDisabled) {
      await spMoveBtns.first().click()
      const m2 = sp.locator('.ant-modal').filter({ hasText: '收回「' })
      await m2.getByLabel('收回到哪部作品').first().click()
      await sp.waitForTimeout(800)
      spOpts = await sp.locator('.ant-select-dropdown:visible .ant-select-item-option').allInnerTexts()
      const emptyText = await sp.locator('.ant-select-dropdown:visible').innerText().catch(() => '')
      spOpts.push(`[下拉文本:${emptyText.replace(/\s+/g, ' ')}]`)
      await sp.keyboard.press('Escape')
      await m2.getByRole('button', B('取消')).click()
    }
    await lib.shot(env, sp, 'g3-c6-series-only')
    const tmp = await ser.api.post(`/series/${sid}/entries`, {
      expected_revision: (await ser.api.get(`/series/${sid}`)).revision, kind: 'world',
      data: { title: `临时条目${Date.now() % 1000}`, category: 'place', content: 'x', aliases: [] },
    })
    const tmpId = (tmp.item || tmp).id
    const serMove = await probe(ser, 'POST', `/series/${sid}/entries/${tmpId}/move-to-book`, { project_id: bookB, expected_revision: (await ser.api.get(`/series/${sid}`)).revision })
    const serImpact = await probe(ser, 'GET', `/series/${sid}/impact?action=move&entry_id=${tmpId}&project_id=${bookB}`)
    r.check('C-6', seBooks && opts.length === 1 && opts[0].includes('剑影续篇') && moveOk && inBookB && /^(403|404|409)/.test(seMoveShen)
      && /^(403|404|409 series_book_edit_required)/.test(serMove) && /^(403|404|409 series_book_edit_required)/.test(serImpact) && (spMoveDisabled || !spOpts.some((t) => t.includes('剑影'))),
    `兼编辑者：可选 ${JSON.stringify(opts)}，收回「${moveName}」成功 ${moveOk}，续篇世界观可见 ${inBookB}；接口把沈青收回续篇 ${seMoveShen}；`
      + `仅系列权限：收回按钮禁用 ${spMoveDisabled}，下拉 ${JSON.stringify(spOpts)}；接口收回 ${serMove}，预检 ${serImpact}`)

    // ---------- C-7 单书成员无系列身份；撤销系列协作者 ----------
    const vw = await lib.contextAs(browser, env, 'qa_viewer')
    pages.push(vw.page)
    const vp = vw.page
    await vp.goto('/projects')
    await vp.getByText('剑影长歌').first().waitFor({ timeout: 15000 })
    await vp.waitForTimeout(800)
    const shelfText = await body(vp)
    await vp.goto(`/projects/${mainPid}/overview`)
    await vp.getByText('剑影长歌').first().waitFor({ timeout: 15000 })
    await vp.waitForTimeout(1000)
    const ovText = await body(vp)
    await vp.goto(`/projects/${mainPid}/characters`)
    await vp.getByText('沈青').first().waitFor({ timeout: 15000 })
    await vp.waitForTimeout(800)
    const chText = await body(vp)
    await lib.shot(env, vp, 'g3-c7-viewer-chars')
    const vProject = await vw.api.get(`/projects/${mainPid}`)
    const vChars = await vw.api.get(`/projects/${mainPid}/characters?page_size=200`)
    const vShen = (vChars.items || vChars).find((c) => c.name === '沈青')
    const vSeriesList = await vw.api.get('/series')
    const vSeries = await probe(vw, 'GET', `/series/${sid}`)
    const vEntries = await probe(vw, 'GET', `/series/${sid}/entries`)
    const noSeries = !shelfText.includes('剑影系列') && !ovText.includes('剑影系列') && !chText.includes('剑影系列')
      && vProject.series == null && (vSeriesList || []).length === 0 && /^(403|404)/.test(vSeries) && /^(403|404)/.test(vEntries)
    r.note('C-7 viewer', `书架/概览/角色页出现「剑影系列」：${shelfText.includes('剑影系列')}/${ovText.includes('剑影系列')}/${chText.includes('剑影系列')}；`
      + `project.series=${JSON.stringify(vProject.series)}；沈青 origin=${JSON.stringify(vShen && vShen.origin)}；GET /series 数 ${(vSeriesList || []).length}；GET /series/1 ${vSeries}；entries ${vEntries}；`
      + `角色页含「系列」字样 ${chText.includes('系列')}`)
    // 撤销 qa_series：作者在界面撤销，qa_series 停在系列页不刷新
    await sp.goto(`/series/${sid}`)
    await sp.getByText('系列共享设定').waitFor({ timeout: 15000 })
    await sp.waitForTimeout(800)
    await ap.goto(`/series/${sid}`)
    const apPanel = ap.locator('section[aria-label="系列成员"]')
    await apPanel.waitFor({ timeout: 15000 })
    const qsRow = apPanel.locator('li.collaboration-member').filter({ hasText: /^qa_series\b/ }).filter({ hasNotText: 'qa_series_editor' })
    await qsRow.getByRole('button', B('撤销')).click()
    const rv = ap.locator('.ant-modal').filter({ hasText: '确认撤销权限' })
    await rv.getByRole('button', B('确认撤销')).click()
    const revokedOk = await ap.getByText('成员权限已撤销。').first().waitFor({ timeout: 10000 }).then(() => true, () => false)
    // 旧页面上继续操作：编辑共享基础
    const shen2 = sp.locator('.ant-list-item').filter({ hasText: '沈青' })
    await shen2.getByRole('button', B('编辑共享基础')).click()
    m = sp.locator('.ant-modal').filter({ hasText: '编辑共享基础 · 沈青' })
    let staleText = ''
    if (await m.isVisible().catch(() => false)) {
      await m.getByRole('textbox', { name: '简介' }).fill('撤销后尝试写入')
      await m.getByRole('button', B('保存共享基础')).click()
      await sp.waitForTimeout(2500)
    } else {
      await sp.waitForTimeout(1500)
    }
    staleText = (await body(sp)).replace(/\s+/g, ' ')
    await lib.shot(env, sp, 'g3-c7-after-revoke')
    const staleWriteApi = await probe(ser, 'PATCH', `/series/${sid}/entries/${adoptedId}`, { data: { profile: 'x' }, expected_revision: 999 })
    const errSeen = (staleText.match(/(系列不存在[^。]*|[^。 ]*重新读取[^。 ]*|[^。 ]*权限[^。]*已变化[^。]*|没有权限[^。]*)/) || [''])[0]
    await sp.reload()
    await sp.waitForTimeout(2500)
    const afterReload = (await body(sp)).replace(/\s+/g, ' ')
    await sp.goto('/series')
    await sp.waitForTimeout(2000)
    const listAfter = await body(sp)
    const shenNow = await author.api.get(`/series/${sid}/entries/${adoptedId}`)
    await lib.shot(env, sp, 'g3-c7-series-list-after')
    r.check('C-7', noSeries && revokedOk && !listAfter.includes('剑影系列') && shenNow.data.profile !== '撤销后尝试写入' && /^(403|404)/.test(staleWriteApi) && !!errSeen,
      `单书查看者无系列身份 ${noSeries}；撤销 ${revokedOk}；旧页面写入后可见错误「${errSeen}」（页面片段「${staleText.slice(0, 120)}」）；`
      + `刷新后「${afterReload.slice(0, 80)}」；系列列表仍含剑影系列 ${listAfter.includes('剑影系列')}；接口写入 ${staleWriteApi}；沈青简介未被改写 ${shenNow.data.profile !== '撤销后尝试写入'}`)

    const consoleErrors = pages.flatMap((p) => p._qaConsole || []).filter((t) => !t.includes('warning'))
    r.note('console', consoleErrors.length ? consoleErrors.slice(0, 8).join(' | ') : '无控制台错误')
  } finally {
    await browser.close()
  }
  process.exit(r.save(env) ? 1 : 0)
}

main().catch((err) => { console.error(err); process.exit(2) })
