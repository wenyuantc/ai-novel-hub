// G1 修改影响追踪 A-1 至 A-12（场景表：research/targets-consistency-collab.md A4）。
// 前置：seed.py → g12-prep.cjs（全书检查已跑）→ g2-consistency-fixes.cjs（只读取第 5、12 章，不改正文）。
// 本脚本会改第 5、12、14 章并新建第 16 章，只能对一套新环境顺序执行一次。
// 运行：QA_DIR=<dir> node scenarios/g1-impact.cjs
'use strict'
const lib = require('../lib.cjs')
const { PID_OF, CH, patchChapter, focusedText, waitText, visible, waitMessage } = require('./g12-common.cjs')

const CH5_INTACT = '山谷一战，林远力竭。林远的佩剑完好无损，他把佩剑收好。他只得赤手下山。'
const CH5_BROKEN = '山谷一战，林远力竭。林远的佩剑在山谷中被巨石砸断，彻底毁了。他只得赤手下山。'
const CH12_QUOTE = '林远拔出佩剑迎敌，剑光如雪。'

const impactUrl = (pid, cid) => `/projects/${pid}/consistency?tab=impact${cid ? `&chapter=${cid}` : ''}`
const drawerOf = (page) => page.locator('.ant-drawer-content').filter({ hasText: '评估对后续章节的影响' })

async function openDrawer(page, pid, cid) {
  await page.goto(impactUrl(pid, cid))
  const drawer = drawerOf(page)
  await drawer.getByText('可能过期的派生内容（').waitFor({ timeout: 20000 })
  await lib.sleep(300)
  return drawer
}

async function rowTags(page, cid) {
  return page.evaluate((id) => {
    const btn = document.querySelector(`[data-impact-chapter="${id}"]`)
    const item = btn?.closest('.ant-list-item')
    return item ? Array.from(item.querySelectorAll('.ant-tag, .ant-typography')).map((n) => n.textContent.trim()).filter(Boolean) : null
  }, cid)
}

async function assess(page, drawer, { button = /评估影响|重新评估/ } = {}) {
  await drawer.getByRole('button', { name: button }).click()
  await page.locator('.ant-popover').getByRole('button', { name: '开始评估' }).click()
}

async function main() {
  const env = lib.loadEnv()
  const r = lib.reporter('g1-impact')
  const pid = PID_OF(env)
  const c5 = CH(env, 5), c12 = CH(env, 12), c14 = CH(env, 14)
  const browser = await lib.launch()
  try {
    const author = await lib.contextAs(browser, env, 'qa_author', 'desktop')
    const page = author.page
    const api = author.api
    page.on('dialog', (d) => void d.accept())
    // G1_FROM=A-11：从某一场景接着跑；G1_ONLY=A-4：只跑列出的场景（前面的场景已在同一环境执行过）
    const order = Array.from({ length: 12 }, (_, i) => `A-${i + 1}`)
    const go = (id) => (process.env.G1_ONLY ? process.env.G1_ONLY.split(',').includes(id)
      : order.indexOf(id) >= order.indexOf(process.env.G1_FROM || 'A-1'))

    if (go('A-1')) {
    // ---------- A-1 ----------
    await page.goto(impactUrl(pid))
    const empty = await waitText(page, '目前没有派生内容过期的章节，也没有评估记录')
    await lib.shot(env, page, 'g1-A1-empty')
    r.check('A-1', empty, `修改影响页签空状态文案可见：${empty}`)

    }
    if (go('A-2')) {
    // ---------- A-2：在编辑器改第 5 章并保存 ----------
    await page.goto(`/projects/${pid}/chapters/${c5}`)
    const body = page.getByRole('textbox', { name: '当前场景正文', exact: true })
    await body.waitFor({ timeout: 20000 })
    await body.click()
    await page.keyboard.press('ControlOrMeta+a')
    await page.keyboard.insertText(CH5_INTACT)
    await page.keyboard.press('ControlOrMeta+s')
    const savedMsg = await waitMessage(page, '已保存', 15000)
    const hint = page.getByText(/本章改动后有 \d+ 项派生内容可能过期/).first()
    const hintShown = await visible(hint, 15000)
    const hintText = hintShown ? await hint.textContent() : ''
    await lib.shot(env, page, 'g1-A2-editor-hint')
    // 1280 桌面视口下页头标题区（章号、状态、字数、过期提示）是否真的可见、可点
    const titleBox = await page.evaluate(() => { const t = document.querySelector('.ant-card-head-title'); const r = t?.getBoundingClientRect(); return r ? Math.round(r.width) : -1 })
    let clickable1280 = true
    try { await hint.click({ trial: true, timeout: 3000 }) } catch { clickable1280 = false }
    // 为完成同一验证意图：在 1440 宽（工具栏不进页头）的新上下文里点击提示
    const wide = await lib.contextAs(browser, env, 'qa_author', { width: 1440, height: 900 })
    await wide.page.goto(`/projects/${pid}/chapters/${c5}`)
    const hintWide = wide.page.getByText(/本章改动后有 \d+ 项派生内容可能过期/).first()
    let deep = ''
    let drawerTitle = ''
    if (await visible(hintWide, 15000)) {
      await hintWide.click()
      await drawerOf(wide.page).getByText('可能过期的派生内容（').waitFor({ timeout: 20000 })
      deep = new URL(wide.page.url()).search
      drawerTitle = (await wide.page.locator('.ant-drawer-title').first().textContent()) || ''
      await lib.shot(env, wide.page, 'g1-A2-1440-drawer')
    }
    await wide.context.close()
    const saved5 = await api.get(`/projects/${pid}/chapters/${c5}`)
    r.check('A-2', clickable1280 && titleBox > 0 && savedMsg && hintShown && /tab=impact/.test(deep) && deep.includes(`chapter=${c5}`) && drawerTitle.includes('第 5 章')
      && saved5.content === CH5_INTACT,
    `编辑器保存提示 ${savedMsg}；页头提示在 DOM 中「${hintText}」，1280 下页头标题区宽 ${titleBox}px、可点击 ${clickable1280}；1440 下点击后地址 ${deep}，抽屉标题「${drawerTitle}」；服务端正文已更新 ${saved5.content === CH5_INTACT}`)

    }
    if (go('A-3')) {
    // ---------- A-3：列表与抽屉第一级 ----------
    await page.goto(impactUrl(pid))
    await page.locator(`[data-impact-chapter="${c5}"]`).waitFor({ timeout: 20000 })
    const rows = await page.locator('[data-impact-chapter]').count()
    const tags5 = await rowTags(page, c5)
    const d3 = await openDrawer(page, pid, c5)
    const cards = {
      summary: await visible(d3.locator('.ant-card-head-title').filter({ hasText: /^章摘要$/ })),
      memories: await visible(d3.locator('.ant-card-head-title').filter({ hasText: /^剧情记忆（\d+ 条）$/ })),
      progressions: await visible(d3.locator('.ant-card-head-title').filter({ hasText: /^设定变化（\d+ 条）$/ })),
    }
    const staleTitle = await d3.getByText(/可能过期的派生内容（\d+ 项）/).textContent()
    await lib.shot(env, page, 'g1-A3-drawer')
    const tagStr = (tags5 || []).join('｜')
    r.check('A-3', rows === 1 && /章摘要已过期/.test(tagStr) && /剧情记忆 \d+ 条已过期/.test(tagStr) && /设定变化 \d+ 条待确认/.test(tagStr)
      && tagStr.includes('事实台账已过期') && cards.summary && cards.memories && cards.progressions,
    `列表 ${rows} 行；第 5 章标签「${tagStr}」；抽屉「${staleTitle}」，章摘要/剧情记忆/设定变化卡片 ${JSON.stringify(cards)}`)

    }
    if (go('A-4')) {
    // ---------- A-4：三个跳转 ----------
    const jumps = []
    let d4 = await openDrawer(page, pid, c5)
    await d4.getByRole('link', { name: '打开本章分析' }).click()
    const analysisOk = await waitText(page, '章节分析', 20000)
    jumps.push(`打开本章分析 → ${new URL(page.url()).pathname}${new URL(page.url()).search}，分析抽屉 ${analysisOk}`)
    await lib.shot(env, page, 'g1-A4-analysis')
    d4 = await openDrawer(page, pid, c5)
    await d4.getByRole('link', { name: '到剧情记忆页查看' }).click()
    let memOk = true
    try { await page.waitForURL(/\/memories/, { timeout: 15000 }) } catch { memOk = false }
    memOk = memOk && await waitText(page, '剧情记忆（', 20000)
    jumps.push(`到剧情记忆页查看 → ${new URL(page.url()).pathname}，记忆列表 ${memOk}`)
    d4 = await openDrawer(page, pid, c5)
    await d4.getByRole('link', { name: '查看设定变化' }).first().click()
    const progOk = await waitText(page, '字段与剧情变化', 20000)
    const progUrl = new URL(page.url())
    jumps.push(`查看设定变化 → ${progUrl.pathname}${progUrl.search}，「字段与剧情变化」可见 ${progOk}`)
    await lib.shot(env, page, 'g1-A4-progression')
    r.check('A-4', analysisOk && progUrl.pathname.endsWith('/characters') && memOk && progOk
      && jumps[0].includes('panel=analysis') && jumps[1].includes('/memories'), jumps.join(' | '))

    }
    if (go('A-5')) {
    // ---------- A-5：已确认无需更新 ----------
    await page.goto(impactUrl(pid))
    await page.locator(`[data-impact-chapter="${c5}"]`).waitFor({ timeout: 20000 })
    const before5 = (await rowTags(page, c5)).join('｜')
    let d5 = await openDrawer(page, pid, c5)
    const box = d5.getByRole('checkbox', { name: '已确认无需更新' }).first()
    await box.check()
    const dirtyNote = await visible(d5.getByText('有尚未保存的确认'))
    await d5.getByRole('button', { name: '保存确认' }).click()
    const ok5 = await waitMessage(page, '已保存')
    await page.reload()
    d5 = drawerOf(page)
    await d5.getByText('可能过期的派生内容（').waitFor({ timeout: 20000 })
    const kept = await d5.getByRole('checkbox', { name: '已确认无需更新' }).first().isChecked()
    await page.goto(impactUrl(pid))
    await page.locator(`[data-impact-chapter="${c5}"]`).waitFor({ timeout: 20000 })
    const after5 = (await rowTags(page, c5)).join('｜')
    const cnt = (s) => Number((s.match(/设定变化 (\d+) 条待确认/) || [0, 0])[1])
    r.check('A-5', dirtyNote && ok5 && kept && cnt(after5) < cnt(before5),
      `勾选后「有尚未保存的确认」${dirtyNote}；保存提示 ${ok5}；刷新后勾选保持 ${kept}；列表「设定变化 N 条待确认」${cnt(before5)} → ${cnt(after5)}（行：${after5}）`)

    }
    if (go('A-6')) {
    // ---------- A-6：评估影响 ----------
    await lib.setDelay(env, { first_byte: 3, per_chunk: 0, kinds: ['extract'] })
    const d6 = await openDrawer(page, pid, c5)
    await assess(page, d6)
    const started = await waitMessage(page, '已开始评估', 10000)
    const progress = await visible(page.getByText('修改影响评估进行中'), 5000)
    const runningTag = await visible(page.locator('.ant-list-item .ant-tag').filter({ hasText: /^评估中$/ }), 5000)
    await lib.shot(env, page, 'g1-A6-running')
    const finished = await waitMessage(page, '修改影响评估已完成', 60000)
    await lib.setDelay(env, { first_byte: 0, per_chunk: 0, kinds: null })
    await lib.sleep(800)
    const changeTitle = await d6.getByText(/事实变化（\d+ 处）/).textContent().catch(() => '')
    const affectedTitle = await d6.getByText(/可能受影响的后续章节（/).textContent().catch(() => '')
    const card12 = d6.locator('.ant-card').filter({ has: page.locator('.ant-card-head-title', { hasText: '第 12 章' }) })
    const quote12 = await visible(card12.locator('blockquote').filter({ hasText: CH12_QUOTE }))
    const changeText = await d6.locator('section[aria-label="事实变化"]').innerText().catch(() => '')
    await lib.shot(env, page, 'g1-A6-result')
    r.check('A-6', started && progress && finished && /事实变化（[1-9]/.test(changeTitle) && quote12,
      `「已开始评估」${started}；进度「修改影响评估进行中」${progress}；列表「评估中」Tag ${runningTag}；完成提示 ${finished}；`
      + `「${changeTitle}」（${changeText.replace(/\s+/g, ' ').slice(0, 80)}）；「${affectedTitle}」；第 12 章卡片引句 ${quote12}`)

    }
    if (go('A-7')) {
    // ---------- A-7：受影响卡片定位 ----------
    const loc7 = []
    for (const link of ['在阅读器中定位', '在编辑器中定位']) {
      const d7 = await openDrawer(page, pid, c5)
      const card = d7.locator('.ant-card').filter({ has: page.locator('.ant-card-head-title', { hasText: '第 12 章' }) })
      await card.getByRole('link', { name: link }).click()
      await page.locator('[data-mention-focus]').first().waitFor({ timeout: 20000 })
      await lib.sleep(300)
      const text = await focusedText(page)
      loc7.push({ link, path: new URL(page.url()).pathname, text })
      await lib.shot(env, page, `g1-A7-${link === '在阅读器中定位' ? 'reader' : 'editor'}`)
    }
    r.check('A-7', loc7.every((x) => x.text === CH12_QUOTE && x.path.endsWith(`/${c12}`)),
      loc7.map((x) => `${x.link} → ${x.path}，高亮「${x.text}」`).join(' | '))

    }
    if (go('A-8')) {
    // ---------- A-8：复核状态保存，潜在冲突不受影响 ----------
    const issuesBefore = (await api.get(`/projects/${pid}/consistency/issues`)).items.map((i) => `${i.id}:${i.status}`).join(',')
    let d8 = await openDrawer(page, pid, c5)
    let card8 = d8.locator('.ant-card').filter({ has: page.locator('.ant-card-head-title', { hasText: '第 12 章' }) })
    await card8.getByRole('radio', { name: '已复核' }).check()
    await d8.getByRole('button', { name: '保存确认' }).click()
    const ok8 = await waitMessage(page, '已保存')
    await page.reload()
    d8 = drawerOf(page)
    await d8.getByText('可能过期的派生内容（').waitFor({ timeout: 20000 })
    card8 = d8.locator('.ant-card').filter({ has: page.locator('.ant-card-head-title', { hasText: '第 12 章' }) })
    const tag8 = await card8.locator('.ant-card-extra .ant-tag').textContent()
    const title8 = await d8.getByText(/可能受影响的后续章节（/).textContent()
    await page.goto(`/projects/${pid}/consistency`)
    await page.getByText('潜在冲突清单').waitFor({ timeout: 15000 })
    const issuesAfter = (await api.get(`/projects/${pid}/consistency/issues`)).items.map((i) => `${i.id}:${i.status}`).join(',')
    const swordTags = (await page.locator('.ant-list-item').filter({ hasText: '佩剑' }).locator('.ant-tag').allTextContents()).join('｜')
    r.check('A-8', ok8 && tag8 === '已复核' && /待复核 0 项/.test(title8) && issuesBefore === issuesAfter,
      `保存提示 ${ok8}；刷新后第 12 章状态「${tag8}」，标题「${title8}」；潜在冲突状态前 ${issuesBefore} / 后 ${issuesAfter}；佩剑问题标签「${swordTags}」`)

    }
    if (go('A-9')) {
    // ---------- A-9：被列章节与本章在评估后修改 ----------
    await patchChapter(api, pid, c12, '敌骑逼近城门。林远拔出佩剑迎敌，剑光如霜。守军士气大振。')
    let d9 = await openDrawer(page, pid, c5)
    const changedNote = await visible(d9.getByText('该章在评估后已修改，下面的原文与位置可能已失效'), 8000)
    await patchChapter(api, pid, c5, CH5_BROKEN)
    d9 = await openDrawer(page, pid, c5)
    const outdated = await visible(d9.getByText('评估后正文又有修改'), 8000)
    const reBtn = await visible(d9.getByRole('button', { name: '重新评估' }), 5000)
    await lib.shot(env, page, 'g1-A9-outdated')
    r.check('A-9', changedNote && outdated && reBtn,
      `接口把第 12 章「剑光如雪」改为「剑光如霜」后，第 12 章卡片警示 ${changedNote}；再把第 5 章改回「佩剑被砸断」后，「评估后正文又有修改」${outdated}，按钮「重新评估」${reBtn}`)

    }
    if (go('A-10')) {
    // ---------- A-10：只改错别字后评估（第 12 章） ----------
    const d10 = await openDrawer(page, pid, c12)
    await assess(page, d10)
    const fin10 = await waitMessage(page, '修改影响评估已完成', 60000)
    await lib.sleep(800)
    const noChange = await visible(d10.getByText('未发现剧情事实变化'), 8000)
    const noList = (await d10.locator('section[aria-label="可能受影响的后续章节"]').count()) === 0
    await lib.shot(env, page, 'g1-A10-no-change')
    r.check('A-10', fin10 && noChange && noList,
      `第 12 章（仅「雪→霜」一字之改）评估完成 ${fin10}；「未发现剧情事实变化」${noChange}；不列后续章节 ${noList}`)

    }
    if (go('A-11')) {
    // ---------- A-11：后续章节台账过期 → 无法判断 ----------
    await patchChapter(api, pid, c14, `第14章。${'林远渡江南下，船行一日，江面平静。'}`)
    // 让第 5 章相对当前台账多出一条事实（老周身亡），保证评估有变化、从而计算无法判断的章节
    await patchChapter(api, pid, c5, `${CH5_BROKEN}渡口的老周已身亡。`)
    const d11 = await openDrawer(page, pid, c5)
    await assess(page, d11, { button: '重新评估' })
    const fin11 = await waitMessage(page, '修改影响评估已完成', 60000)
    await lib.sleep(800)
    const unc = d11.getByText(/有 \d+ 章尚未抽取或已过期，无法判断/)
    const uncOk = await visible(unc, 8000)
    const uncText = uncOk ? await d11.locator('.ant-alert').filter({ hasText: '无法判断' }).innerText() : ''
    let modalOk = false
    let modalInfo = ''
    if (uncOk) {
      await d11.getByRole('button', { name: '对这些章节运行一致性检查' }).click()
      const modal = page.getByRole('dialog', { name: '发起一致性检查' })
      modalOk = await visible(modal, 8000)
      const range = await modal.getByRole('radio', { name: '起止章节' }).isChecked()
      const selects = (await modal.locator('.ant-select-selection-item').allTextContents()).join(' → ')
      modalInfo = `范围选「起止章节」${range}，预填 ${selects}`
      modalOk = modalOk && range && selects.includes('第 14 章')
      await lib.shot(env, page, 'g1-A11-modal')
      await page.keyboard.press('Escape')
      await modal.waitFor({ state: 'hidden', timeout: 5000 }).catch(() => {})
    }
    r.check('A-11', fin11 && uncOk && uncText.includes('第 14 章') && modalOk,
      `接口改第 14 章使其台账过期后重新评估第 5 章：完成 ${fin11}；提示「${uncText.replace(/\s+/g, ' ').slice(0, 120)}」；弹窗 ${modalInfo}`)

    }
    if (go('A-12')) {
    // ---------- A-12：未检查过的章节；查看者；编辑者 ----------
    const existing16 = (await api.get(`/projects/${pid}/chapters`)).find?.((c) => c.chapter_no === 16)
    const ch16 = existing16 || await api.post(`/projects/${pid}/chapters`, { title: '第16回', chapter_no: 16, content: '新章初稿。林远到了临江城。' })
    await patchChapter(api, pid, ch16.id, '新章初稿。林远到了临江城，城门口挂着灯笼。')
    const d12 = await openDrawer(page, pid, ch16.id)
    const noBase = await visible(d12.getByText('本章还没有改动前的事实台账，请先运行一次一致性检查；之后再修改本章才能评估影响。'), 8000)
    const runBtn = await visible(d12.getByRole('button', { name: '运行一致性检查' }), 5000)
    const notAssessed = await visible(d12.getByText('尚未评估。'), 5000)
    const assessDisabled = await d12.getByRole('button', { name: '评估影响' }).isDisabled()
    await lib.shot(env, page, 'g1-A12-no-baseline')

    const viewer = await lib.contextAs(browser, env, 'qa_viewer', 'desktop')
    const dv = await openDrawer(viewer.page, pid, c5)
    const v = {
      checkbox: await dv.getByRole('checkbox').count(),
      radio: await dv.getByRole('radio').count(),
      assessBtn: await dv.getByRole('button', { name: /评估影响|重新评估/ }).count(),
      saveBtn: await dv.getByRole('button', { name: '保存确认' }).count(),
      noPerm: await visible(dv.getByText('当前权限不能发起评估。')),
      readOnly: await visible(dv.getByText('当前为只读权限，不能修改确认状态。')),
    }
    await lib.shot(env, viewer.page, 'g1-A12-viewer')

    const editor = await lib.contextAs(browser, env, 'qa_editor', 'desktop')
    const ep = editor.page
    // 作者再改第 5 章（佩剑完好），编辑者发起评估，然后在结果里把第 12 章设为「无需处理」并保存
    await patchChapter(api, pid, c5, CH5_INTACT)
    let de = await openDrawer(ep, pid, c5)
    await assess(ep, de, { button: '重新评估' })
    const eStarted = await waitMessage(ep, '已开始评估', 10000)
    const eFin = await waitMessage(ep, '修改影响评估已完成', 60000)
    await lib.sleep(800)
    de = await openDrawer(ep, pid, c5)
    const ecard = de.locator('.ant-card').filter({ has: ep.locator('.ant-card-head-title', { hasText: '第 12 章' }) })
    await ecard.getByRole('radio', { name: '无需处理' }).check()
    await de.getByRole('button', { name: '保存确认' }).click()
    const eSaved = await waitMessage(ep, '已保存')
    await lib.shot(env, ep, 'g1-A12-editor')
    const task = (await editor.api.get(`/projects/${pid}/consistency/impacts/${c5}`)).assessment
    r.check('A-12', noBase && runBtn && notAssessed && assessDisabled
      && v.checkbox === 0 && v.radio === 0 && v.assessBtn === 0 && v.saveBtn === 0 && v.noPerm && v.readOnly
      && eSaved && eStarted && eFin,
    `新建第 16 章并改稿：no_baseline 文案 ${noBase}、「运行一致性检查」${runBtn}、「尚未评估。」${notAssessed}、评估按钮禁用 ${assessDisabled}；`
      + `查看者：复选框 ${v.checkbox}、单选 ${v.radio}、评估按钮 ${v.assessBtn}、保存确认 ${v.saveBtn}、「当前权限不能发起评估。」${v.noPerm}、只读文案 ${v.readOnly}；`
      + `编辑者：作者改第 5 章后编辑者「重新评估」开始 ${eStarted}、完成 ${eFin}（模型 ${task?.model}）；结果中第 12 章改为「无需处理」保存 ${eSaved}`)
    r.note('console-A12', [...viewer.page._qaConsole, ...ep._qaConsole].slice(0, 8).join(' | ') || '查看者/编辑者无控制台错误/警告')

    }
    r.note('console', page._qaConsole.slice(0, 8).join(' | ') || '无控制台错误/警告')
  } finally {
    await lib.setDelay(env, { first_byte: 0, per_chunk: 0, kinds: null }).catch(() => {})
    await browser.close()
  }
  process.exit(r.save(env) ? 1 : 0)
}

main().catch((err) => { console.error(err); process.exit(2) })
