// G3 管理员兼协作者 C-8 至 C-13（场景表 research/targets-consistency-collab.md C5）。
// 运行：QA_DIR=<dir> node scenarios/g3-admin.cjs
// 会改动隔离库：qa_admin_rw 的协作身份依次变为编辑者（有／无作者模型授权）、被撤销，再被邀请为系列设定协作者。
// 开头会把 qa_admin_rw 恢复为主作品查看者，可重复执行。
'use strict'
const lib = require('../lib.cjs')

const body = async (page) => (await page.locator('body').innerText()).replace(/\s+/g, ' ')
const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
/** AntD 两字按钮会插入空格；图标按钮名称带图标名前缀。 */
const B = (t) => ({ name: new RegExp('^(?:[a-z-]+\\s+)?\\s*' + [...t].map(esc).join('\\s*') + '\\s*$') })
const btnCount = (page, t) => page.getByRole('button', B(t)).count()

async function probe(ctx, method, url, data) {
  const res = await ctx.context.request.fetch(`/api/v1${url}`, { method, data })
  let code = ''
  try { const j = JSON.parse(await res.text()); code = j.code || j.detail?.code || (typeof j.detail === 'string' ? j.detail : '') } catch { /* 非 JSON */ }
  return `${res.status()}${code ? ` ${code}` : ''}`
}
const denied = (s) => /^(401|403|404|409)/.test(s)

async function waitText(page, re, ms = 15000) {
  return page.getByText(re).first().waitFor({ timeout: ms }).then(() => true, () => false)
}

async function main() {
  const env = lib.loadEnv()
  const fx = env.fixtures
  const pid = fx.main_project.id
  const sid = fx.series.id
  const ch14 = fx.main_project.chapters['14']
  const adminId = fx.users.qa_admin_rw
  const r = lib.reporter('g3-admin')
  const browser = await lib.launch()
  const pages = []
  try {
    const author = await lib.contextAs(browser, env, 'qa_author')
    // ---- 恢复：qa_admin_rw 为主作品已接受的查看者（无作者模型授权）、非系列协作者 ----
    const memberOf = async () => (await author.api.get(`/projects/${pid}/collaborators`)).find((m) => m.username === 'qa_admin_rw')
    const acceptAll = async (ctx) => {
      for (const i of await ctx.api.get('/collaboration/invitations')) {
        await ctx.api.post(`/collaboration/invitations/${i.scope}/${i.invitation_id}/accept`, { expected_revision: i.revision })
      }
    }
    let admin = await lib.contextAs(browser, env, 'qa_admin_rw')
    pages.push(admin.page)
    let mem = await memberOf()
    if (!mem || !['pending', 'accepted'].includes(mem.status)) {
      await author.api.post(`/projects/${pid}/collaborators`, { username: 'qa_admin_rw', role: 'viewer', allow_owner_ai: false })
      await acceptAll(admin)
      mem = await memberOf()
    } else if (mem.role !== 'viewer' || mem.allow_owner_ai) {
      mem = await author.api.patch(`/projects/${pid}/collaborators/${adminId}`, { expected_revision: mem.revision, role: 'viewer', allow_owner_ai: false })
    }
    const sMembers = await author.api.get(`/series/${sid}/collaborators`)
    const sm = sMembers.find((m) => m.username === 'qa_admin_rw' && ['pending', 'accepted'].includes(m.status))
    if (sm) await author.api.raw('DELETE', `/series/${sid}/collaborators/${adminId}?expected_revision=${sm.revision}`)
    const ownCfg = (await admin.api.get('/ai-configs'))
    for (const c of (ownCfg.items || ownCfg)) await admin.api.del(`/ai-configs/${c.id}`)
    r.note('setup', `qa_admin_rw：主作品 ${mem.role}/${mem.status}，allow_owner_ai=${mem.allow_owner_ai}；本人模型配置已清空`)

    const ap = admin.page
    // ---------- C-8 管理员兼查看者（读写管理角色） ----------
    await ap.goto('/projects')
    await waitText(ap, '剑影长歌')
    await ap.waitForTimeout(800)
    const card = ap.locator('article, li, .library-card, a').filter({ hasText: '剑影长歌' }).first()
    const cardText = (await card.innerText().catch(() => '')).replace(/\s+/g, ' ')
    await lib.shot(env, ap, 'g3-c8-shelf')
    await ap.goto(`/projects/${pid}/overview`)
    await ap.getByLabel('一句话设定').waitFor({ timeout: 15000 })
    await ap.waitForTimeout(1000)
    let t = await body(ap)
    const notice = t.includes('管理员正在管理')
    const viewerAlert = t.includes('当前为查看者，不能修改此作品') || t.includes('当前为只读内容管理')
    const premise = `少年剑客林远的江湖路（管理员兼查看者修订 ${Date.now() % 100000}）`
    await ap.getByLabel('一句话设定').fill(premise)
    await ap.getByRole('button', B('保存')).first().click()
    const savedInfo = await waitText(ap, '已保存', 10000)
    const proj = await author.api.get(`/projects/${pid}`)
    // 正文
    await ap.goto(`/projects/${pid}/chapters/${ch14}`)
    const editable = ap.locator('[contenteditable="true"]').first()
    let chapterSaved = false
    let chapterNote = ''
    if (await editable.waitFor({ timeout: 15000 }).then(() => true, () => false)) {
      await editable.click()
      await ap.keyboard.press('Meta+End')
      await ap.keyboard.type('（管理员补记）')
      await ap.keyboard.press('Meta+s')
      for (let i = 0; i < 20 && !chapterSaved; i += 1) {
        await lib.sleep(500)
        const c = await author.api.get(`/projects/${pid}/chapters/${ch14}`)
        chapterSaved = c.content.includes('管理员补记')
      }
    } else chapterNote = '未找到可编辑正文区域'
    await lib.shot(env, ap, 'g3-c8-chapter')
    const backLink = ap.getByRole('link', { name: /全部作品|作品管理/ }).first()
    const backText = (await backLink.innerText().catch(() => '')).trim()
    const backHref = await backLink.getAttribute('href').catch(() => '')
    r.check('C-8', cardText.includes('剑影长歌') && !notice && !viewerAlert && savedInfo && proj.premise === premise && chapterSaved && backHref === '/projects',
      `书架卡片「${cardText.slice(0, 60)}」；代管提示 ${notice}；只读提示 ${viewerAlert}；作品资料保存提示 ${savedInfo}、服务端 premise 已更新 ${proj.premise === premise}；`
      + `第 14 章正文保存 ${chapterSaved}${chapterNote}；返回入口「${backText}」→ ${backHref}`)

    // ---------- C-9 AI 与作者专属入口 ----------
    await ap.goto(`/projects/${pid}/overview`)
    await ap.getByLabel('一句话设定').waitFor({ timeout: 15000 })
    await ap.waitForTimeout(800)
    const ov = {
      创作向导: await btnCount(ap, '创作向导'), 备份项目: await btnCount(ap, '备份项目'), 协作者: await btnCount(ap, '协作者'),
      项目智能体: await btnCount(ap, '项目智能体'),
    }
    await ap.getByRole('button', B('项目智能体')).click()
    // 无会话时显示空态说明；已有本人会话（例如上一轮作为编辑者建立的）时显示「此会话仅供查看」
    const roMsg = await waitText(ap, /当前为查看者，只能查看本人已有的智能体会话，不能新建会话或发送消息。|此会话仅供查看/, 10000)
    const newConvDisabled = await ap.getByRole('button', { name: '新建会话' }).isDisabled().catch(() => null)
    const startBtn = await btnCount(ap, '开始新会话')
    await lib.shot(env, ap, 'g3-c9-agent-readonly')
    await ap.getByRole('button', B('项目智能体')).click()
    await ap.goto(`/projects/${pid}/chapters`)
    await waitText(ap, '第14回')
    const batch = await btnCount(ap, '批量生成')
    await ap.goto(`/projects/${pid}/chapters/${ch14}`)
    await ap.locator('[contenteditable="true"]').first().waitFor({ timeout: 15000 }).catch(() => {})
    await ap.waitForTimeout(800)
    const aiTab = (await ap.getByRole('tab', { name: /AI 助手/ }).count()) + (await ap.getByText('AI 助手', { exact: true }).count())
    await lib.shot(env, ap, 'g3-c9-editor')
    const api9 = {
      generate: await probe(admin, 'POST', `/chapters/${ch14}/generate`, { target_words: 300 }),
      newConversation: await probe(admin, 'POST', `/projects/${pid}/agent/conversations`, {}),
      collaborators: await probe(admin, 'GET', `/projects/${pid}/collaborators`),
      backup: await probe(admin, 'GET', `/projects/${pid}/backup`),
    }
    r.check('C-9', ov.创作向导 === 0 && ov.备份项目 === 0 && ov.协作者 === 0 && ov.项目智能体 === 1 && roMsg && newConvDisabled === true && startBtn === 0
      && batch === 0 && aiTab === 0 && denied(api9.generate) && denied(api9.newConversation) && denied(api9.collaborators),
    `概览按钮 ${JSON.stringify(ov)}；智能体只读说明 ${roMsg}、新建会话禁用 ${newConvDisabled}、开始新会话 ${startBtn}；批量生成 ${batch}；AI 助手标签 ${aiTab}；接口 ${JSON.stringify(api9)}`)

    // ---------- C-10 仅 projects.read 的管理员兼查看者 ----------
    const ro = await lib.contextAs(browser, env, 'qa_admin_ro')
    pages.push(ro.page)
    await ro.page.goto(`/projects/${pid}/overview`)
    await ro.page.getByLabel('一句话设定').waitFor({ timeout: 15000 })
    await ro.page.waitForTimeout(800)
    const roText = await body(ro.page)
    const roSaveDisabled = await ro.page.getByRole('button', B('保存')).first().isDisabled()
    const roPremiseDisabled = await ro.page.getByLabel('一句话设定').isDisabled()
    await lib.shot(env, ro.page, 'g3-c10-ro')
    const roPatch = await probe(ro, 'PATCH', `/projects/${pid}`, { premise: 'x' })
    r.check('C-10', roText.includes('当前为查看者，不能修改此作品') && !roText.includes('管理员正在管理') && roSaveDisabled && denied(roPatch),
      `提示「当前为查看者，不能修改此作品」${roText.includes('当前为查看者，不能修改此作品')}；代管提示 ${roText.includes('管理员正在管理')}；保存禁用 ${roSaveDisabled}、设定输入禁用 ${roPremiseDisabled}；接口 PATCH 作品 ${roPatch}`)

    // ---------- C-11 管理员被改为编辑者：有／无作者模型授权 ----------
    // 作者在界面「协作者」抽屉修改权限
    const authorPage = author.page
    pages.push(authorPage)
    const setRole = async (allow) => {
      await authorPage.goto(`/projects/${pid}/overview`)
      await authorPage.getByRole('button', B('协作者')).click()
      const panel = authorPage.locator('section[aria-label="作品成员"]')
      await panel.waitFor({ timeout: 10000 })
      const row = panel.locator('li.collaboration-member').filter({ hasText: 'qa_admin_rw' })
      await row.getByRole('button', B('修改权限')).click()
      const m = authorPage.locator('.ant-modal').filter({ hasText: '确认修改权限' })
      await m.waitFor()
      await m.getByLabel('成员角色').first().click()
      await authorPage.locator('.ant-select-dropdown:visible .ant-select-item-option').filter({ hasText: '编辑者' }).click()
      const cb = m.getByRole('checkbox', { name: '允许使用作者模型配置' })
      if ((await cb.isChecked()) !== allow) await cb.click()
      await m.getByRole('button', B('保存权限')).click()
      const ok = await waitText(authorPage, '成员权限已更新。', 10000)
      await authorPage.keyboard.press('Escape')
      return ok
    }
    const set1 = await setRole(true)
    await ap.goto(`/projects/${pid}/chapters/${ch14}`)
    await ap.locator('[contenteditable="true"]').first().waitFor({ timeout: 15000 }).catch(() => {})
    await ap.waitForTimeout(1200)
    const aiTabOn = (await ap.getByRole('tab', { name: /AI 助手/ }).count()) > 0
    if (aiTabOn) await ap.getByRole('tab', { name: /AI 助手/ }).first().click()
    const feeHint = await waitText(ap, '未配置本人文本模型，将使用作者授权的默认文本模型', 8000)
    await lib.shot(env, ap, 'g3-c11-owner-ai')
    const gen = await admin.api.post(`/chapters/${ch14}/generate`, { target_words: 300 })
    const genDone = await admin.api.waitTask(gen)
    const myTasks = await admin.api.get(`/tasks?project_id=${pid}&limit=20`)
    const mine = (myTasks.items || myTasks).some((x) => x.id === gen.id)
    const authorTasks = await author.api.get(`/tasks?project_id=${pid}&limit=50`)
    const inAuthor = (authorTasks.items || authorTasks).some((x) => x.id === gen.id)
    // 智能体
    await lib.clearScripts(env)
    await ap.goto(`/projects/${pid}/overview`)
    await ap.getByRole('button', B('项目智能体')).click()
    let agentOk = false
    const newBtn = ap.getByRole('button', { name: '新建会话' })
    await newBtn.waitFor({ timeout: 10000 }).catch(() => {})
    await ap.waitForTimeout(800)
    if (await newBtn.isEnabled().catch(() => false)) {
      await newBtn.click()
      const box = ap.locator('textarea[aria-label="发给项目智能体的消息"]')
      await box.waitFor({ timeout: 15000 })
      await box.fill('管理员兼编辑者的智能体测试')
      await ap.getByRole('button', B('发送')).click()
      agentOk = await waitText(ap, /我已了解当前作品的情况/, 30000)
    }
    const convs = await admin.api.get(`/projects/${pid}/agent/conversations`)
    const authorConvs = await author.api.get(`/projects/${pid}/agent/conversations`)
    await lib.shot(env, ap, 'g3-c11-agent')
    await ap.getByRole('button', B('项目智能体')).click().catch(() => {})
    r.note('C-11a', `作者授权：改权限 ${set1}；AI 助手标签 ${aiTabOn}；费用提示 ${feeHint}；生成任务 ${genDone.status}、出现在本人任务列表 ${mine}、出现在作者任务列表 ${inAuthor}；`
      + `智能体会话：本人 ${convs.length} 个、作者可见 ${authorConvs.filter((c) => c.title === convs[0]?.title).length}；界面出现回复 ${agentOk}`)
    // 无作者模型授权
    const set2 = await setRole(false)
    await ap.goto(`/projects/${pid}/chapters/${ch14}`)
    await ap.locator('[contenteditable="true"]').first().waitFor({ timeout: 15000 }).catch(() => {})
    await ap.waitForTimeout(1200)
    if (await ap.getByRole('tab', { name: /AI 助手/ }).count()) await ap.getByRole('tab', { name: /AI 助手/ }).first().click()
    const noModel = await waitText(ap, '尚未配置文本模型', 8000)
    const feeGone = !(await body(ap)).includes('将使用作者授权的默认文本模型')
    await lib.shot(env, ap, 'g3-c11-no-owner-ai')
    const gen2 = await probe(admin, 'POST', `/chapters/${ch14}/generate`, { target_words: 300 })
    // 本人配置后可用本人模型
    await admin.api.post('/ai-configs', { name: '管理员本人假模型', provider: 'openai_compat', base_url: env.state.model_base_url, api_key: 'local-fake', model: 'fake-model', is_default: true })
    const gen3 = await admin.api.post(`/chapters/${ch14}/generate`, { target_words: 300 })
    const gen3Done = await admin.api.waitTask(gen3)
    r.check('C-11', set1 && aiTabOn && feeHint && genDone.status === 'succeeded' && mine && !inAuthor && convs.length >= 1 && agentOk && set2 && noModel && feeGone
      && /^(400|409|422)/.test(gen2) && gen3Done.status === 'succeeded',
    `有授权：AI 助手 ${aiTabOn}、费用提示 ${feeHint}、生成 ${genDone.status}（本人任务 ${mine}、作者列表不含 ${!inAuthor}）、智能体会话 ${convs.length} 个、界面回复 ${agentOk}；`
      + `无授权且无本人模型：「尚未配置文本模型」${noModel}、费用提示消失 ${feeGone}、接口生成 ${gen2}；配置本人模型后生成 ${gen3Done.status}`)

    // ---------- C-12 撤销协作身份（对方页面不刷新） ----------
    await ap.goto(`/projects/${pid}/overview`)
    await ap.getByLabel('一句话设定').waitFor({ timeout: 15000 })
    await ap.waitForTimeout(800)
    const before12 = { 创作向导: await btnCount(ap, '创作向导'), 项目智能体: await btnCount(ap, '项目智能体') }
    const cur = await memberOf()
    await author.api.raw('DELETE', `/projects/${pid}/collaborators/${adminId}?expected_revision=${cur.revision}`)
    // 不刷新：先直接在旧页面保存作品资料
    const premise2 = `撤销后旧页面保存 ${Date.now() % 100000}`
    await ap.getByLabel('一句话设定').fill(premise2)
    await ap.getByRole('button', B('保存')).first().click()
    await ap.waitForTimeout(2500)
    const afterSaveText = await body(ap)
    const p2 = await author.api.get(`/projects/${pid}`)
    // 等待前台权限轮询（15 秒）
    await ap.waitForTimeout(17000)
    const polled = await body(ap)
    const after12 = { 创作向导: await btnCount(ap, '创作向导'), 项目智能体: await btnCount(ap, '项目智能体'), 导出: await ap.getByRole('button', { name: /导出/ }).count() }
    await lib.shot(env, ap, 'g3-c12-after-revoke-polled')
    const genAfter = await probe(admin, 'POST', `/chapters/${ch14}/generate`, { target_words: 300 })
    const tasksAfter = await admin.api.get(`/tasks?project_id=${pid}&limit=50`)
    const oldTaskVisible = (tasksAfter.items || tasksAfter).some((x) => x.id === gen.id)
    const oldOutput = await probe(admin, 'GET', `/tasks/${gen.id}/output?task_key=${encodeURIComponent(gen.task_key)}`)
    const convAfter = await probe(admin, 'GET', `/projects/${pid}/agent/conversations`)
    const exportAfter = await probe(admin, 'GET', `/projects/${pid}/export?format=epub`)
    await ap.goto('/projects')
    await ap.waitForTimeout(2500)
    const shelfAfter = await body(ap)
    await ap.goto('/admin/projects')
    await waitText(ap, '剑影长歌')
    await ap.getByRole('link', { name: '剑影长歌' }).first().click().catch(() => {})
    await ap.waitForTimeout(2500)
    if (!/\/projects\/\d+/.test(ap.url())) await ap.goto(`/projects/${pid}/overview`)
    await ap.getByLabel('一句话设定').waitFor({ timeout: 15000 })
    await ap.waitForTimeout(1000)
    const mgmtText = await body(ap)
    const mgmt = { 代管提示: mgmtText.includes('管理员正在管理「qa_author」的项目'), 创作向导: await btnCount(ap, '创作向导'), 项目智能体: await btnCount(ap, '项目智能体'), 导出: await ap.getByRole('button', { name: /导出/ }).count() }
    const premise3 = `撤销后管理员人工编辑 ${Date.now() % 100000}`
    await ap.getByLabel('一句话设定').fill(premise3)
    await ap.getByRole('button', B('保存')).first().click()
    const saved3 = await waitText(ap, '已保存', 10000)
    const p3 = await author.api.get(`/projects/${pid}`)
    await lib.shot(env, ap, 'g3-c12-admin-manage')
    r.check('C-12', before12.创作向导 === 1 && !/剑影长歌/.test(shelfAfter.replace(/作品系列.*$/, '')) && mgmt.代管提示 && mgmt.创作向导 === 0 && mgmt.项目智能体 === 0 && mgmt.导出 === 0
      && after12.创作向导 === 0 && after12.项目智能体 === 0 && saved3 && p3.premise === premise3 && denied(genAfter) && !oldTaskVisible && denied(convAfter) && denied(exportAfter),
    `撤销前 ${JSON.stringify(before12)}；旧页面保存后 premise 写入 ${p2.premise === premise2}（页面含「已保存」${afterSaveText.includes('已保存')}）；轮询后 ${JSON.stringify(after12)}，页面含代管提示 ${polled.includes('管理员正在管理')}；`
      + `书架仍有剑影长歌 ${shelfAfter.includes('剑影长歌')}；从作品管理进入 ${JSON.stringify(mgmt)}，人工保存 ${saved3 && p3.premise === premise3}；`
      + `接口：生成 ${genAfter}、旧任务在列表 ${oldTaskVisible}、旧任务输出 ${oldOutput}、智能体会话 ${convAfter}、导出 ${exportAfter}`)

    // ---------- C-13 管理员兼系列设定协作者：对无书籍编辑身份的成员书做归属操作 ----------
    await author.api.post(`/series/${sid}/collaborators`, { username: 'qa_admin_rw' })
    await acceptAll(admin)
    const rev = (await admin.api.get(`/series/${sid}`)).revision
    const entries = await admin.api.get(`/series/${sid}/entries?page=1&page_size=100`)
    const unadopted = entries.items.find((e) => !e.has_uses)
    const shen = entries.items.find((e) => e.kind === 'character' && e.data.name === '沈青')
    const linyuan = (await author.api.get(`/projects/${pid}/characters?page_size=200`)).find((c) => c.name === '林远')
    const api13 = {
      useOnMain: unadopted ? await probe(admin, 'POST', `/series/${sid}/entries/${unadopted.id}/uses`, { project_id: pid, expected_revision: rev }) : '无未采用条目',
      unuseShen: await probe(admin, 'DELETE', `/series/${sid}/entries/${shen.id}/uses/${pid}?expected_revision=${rev}`),
      moveShen: await probe(admin, 'POST', `/series/${sid}/entries/${shen.id}/move-to-book`, { project_id: pid, expected_revision: rev }),
      promoteImpact: await probe(admin, 'GET', `/series/${sid}/impact?action=promote&project_id=${pid}&kind=character&entry_id=${linyuan.id}`),
    }
    await ap.goto(`/series/${sid}`)
    await waitText(ap, '系列共享设定')
    await ap.waitForTimeout(1000)
    const moveBtn = ap.locator('.ant-list-item').filter({ hasText: '沈青' }).getByRole('button', B('收回指定作品'))
    const moveDisabled = await moveBtn.isDisabled()
    let opts = []
    if (!moveDisabled) {
      await moveBtn.click()
      const m = ap.locator('.ant-modal').filter({ hasText: '收回「沈青」' })
      await m.getByLabel('收回到哪部作品').first().click()
      await ap.waitForTimeout(1200)
      opts = await ap.locator('.ant-select-dropdown:visible .ant-select-item-option').allInnerTexts()
      opts.push(`[${(await ap.locator('.ant-select-dropdown:visible').innerText().catch(() => '')).replace(/\s+/g, ' ')}]`)
    }
    await lib.shot(env, ap, 'g3-c13-series-move')
    // 主作品内「系列」相关入口（角色页提升）
    r.check('C-13', Object.values(api13).every((s) => denied(s) || s === '无未采用条目') && (moveDisabled || !opts.some((o) => o.includes('剑影长歌'))),
      `接口 ${JSON.stringify(api13)}；系列页收回按钮禁用 ${moveDisabled}，可选目标 ${JSON.stringify(opts)}`)

    const consoleErrors = pages.flatMap((p) => p._qaConsole || []).filter((x) => !x.includes('warning'))
    r.note('console', consoleErrors.length ? consoleErrors.slice(0, 8).join(' | ') : '无控制台错误')
  } finally {
    await browser.close()
  }
  process.exit(r.save(env) ? 1 : 0)
}

main().catch((err) => { console.error(err); process.exit(2) })
