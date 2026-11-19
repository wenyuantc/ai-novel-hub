// G2 一致性检查缺陷复验：B-1 证据高亮不越界（引句以设定名开头）、B-2 引句不以设定名开头、B-4 仅键盘保存与 Esc 关闭。
// B-3（慢模型下编辑者的进度流）见 g2-b3-slow-model.cjs。
// 前置：g12-prep.cjs 已执行（第 8／10 章玉佩证据 + 一次全书检查）；须在 g1-impact.cjs 之前运行（G1 会改第 5、12 章）。
// 运行：QA_DIR=<dir> node scenarios/g2-consistency-fixes.cjs
'use strict'
const lib = require('../lib.cjs')
const { PID_OF, focusedText, visible, waitMessage } = require('./g12-common.cjs')

async function openIssue(page, pid, title) {
  await page.goto(`/projects/${pid}/consistency`)
  await page.getByRole('button', { name: `查看潜在冲突：${title}` }).click()
  const drawer = page.locator('.ant-drawer-content').filter({ hasText: '潜在冲突详情' })
  await drawer.getByText('原文证据').waitFor({ timeout: 15000 })
  return drawer
}

/** 打开问题 → 点某条证据的定位链接 → 返回实际高亮文字与周边检查。 */
async function locate(env, page, pid, issue, evidence, linkName, mustNotFocus) {
  const drawer = await openIssue(page, pid, issue.title)
  const card = drawer.locator('.ant-card').filter({ hasText: evidence.quote })
  await card.getByRole('link', { name: linkName }).click()
  await page.locator('[data-mention-focus]').first().waitFor({ timeout: 20000 })
  await lib.sleep(300)
  const text = await focusedText(page)
  const leaked = mustNotFocus.filter((s) => text.includes(s))
  // 引句内的设定名仍是可点击的提及按钮
  const mentionBtn = await page.locator('button[aria-label^="林远："]').count()
  const url = new URL(page.url())
  await lib.shot(env, page, `g2-${issue.id}-${evidence.chapter_no}-${linkName === '在阅读器中定位' ? 'reader' : 'editor'}`)
  return { text, leaked, mentionBtn, path: url.pathname, from: url.searchParams.get('mention_from') }
}

async function main() {
  const env = lib.loadEnv()
  const r = lib.reporter('g2-consistency-fixes')
  const pid = PID_OF(env)
  const browser = await lib.launch()
  try {
    const author = await lib.contextAs(browser, env, 'qa_author', 'desktop')
    const page = author.page
    const issues = (await author.api.get(`/projects/${pid}/consistency/issues`)).items
    const sword = issues.find((i) => i.title.includes('佩剑'))
    const jade = issues.find((i) => i.title.includes('玉佩'))

    // ---------- B-1：引句以「林远」开头（第 5、12 章） ----------
    const tails = { 5: ['他只得赤手下山。', '山谷一战'], 12: ['守军士气大振。', '敌骑逼近城门'] }
    const b1 = []
    for (const ev of sword.evidence) {
      for (const link of ['在阅读器中定位', '在编辑器中定位']) {
        const res = await locate(env, page, pid, sword, ev, link, tails[ev.chapter_no])
        b1.push({ ch: ev.chapter_no, link, ok: res.text === ev.quote && res.leaked.length === 0 && res.mentionBtn > 0, ...res })
      }
    }
    r.check('B-1', b1.every((x) => x.ok), b1.map((x) => `第${x.ch}章/${x.link}@${x.path}：高亮「${x.text}」`
      + `${x.leaked.length ? ` 越界含「${x.leaked.join('、')}」` : ''}；林远提及按钮 ${x.mentionBtn}；mention_from=${x.from}`).join(' | '))

    // ---------- B-2：引句不以设定名开头（第 8、10 章，林远在句中） ----------
    const heads = { 8: ['官道上人来人往。', '茶棚老板摇头叹息。'], 10: ['雨夜投宿客栈。', '窗外雨声渐歇。'] }
    const b2 = []
    for (const ev of jade.evidence) {
      for (const link of ['在阅读器中定位', '在编辑器中定位']) {
        const res = await locate(env, page, pid, jade, ev, link, heads[ev.chapter_no])
        b2.push({ ch: ev.chapter_no, link, ok: res.text === ev.quote && res.leaked.length === 0 && res.mentionBtn > 0, ...res })
      }
    }
    r.check('B-2', b2.every((x) => x.ok), b2.map((x) => `第${x.ch}章/${x.link}：高亮「${x.text}」`
      + `${x.leaked.length ? ` 越界含「${x.leaked.join('、')}」` : ''}；林远提及按钮 ${x.mentionBtn}`).join(' | '))

    // ---------- B-4：仅键盘 ----------
    const kb = await lib.contextAs(browser, env, 'qa_author', 'desktop')
    const kp = kb.page
    await kp.goto(`/projects/${pid}/consistency`)
    await kp.getByRole('button', { name: `查看潜在冲突：${jade.title}` }).waitFor({ timeout: 15000 })
    const active = () => kp.evaluate(() => {
      const el = document.activeElement
      if (!el) return { tag: null }
      return {
        tag: el.tagName, type: el.getAttribute('type'), issue: el.getAttribute('data-consistency-issue'),
        text: (el.textContent || '').trim().slice(0, 30), value: el.value, checked: el.checked,
        inDrawer: !!el.closest('.ant-drawer-content'), disabled: el.disabled,
      }
    })
    let tabs = 0
    let a = await active()
    while (a.issue === null && tabs < 120) { await kp.keyboard.press('Tab'); tabs += 1; a = await active() }
    const reachedIssue = a.issue !== null
    const firstIssue = a.issue
    await kp.keyboard.press('Enter')
    const drawer = kp.locator('.ant-drawer-content').filter({ hasText: '潜在冲突详情' })
    await drawer.getByText('状态与备注').waitFor({ timeout: 15000 })
    let inner = 0
    a = await active()
    while (!(a.tag === 'INPUT' && a.type === 'radio') && inner < 40) { await kp.keyboard.press('Tab'); inner += 1; a = await active() }
    const radioBefore = a.value
    await kp.keyboard.press('ArrowRight')
    await lib.sleep(200)
    a = await active()
    const radioAfter = a.value
    let toSave = 0
    while (!(a.tag === 'BUTTON' && a.text.replace(/\s/g, '') === '保存') && toSave < 10) { await kp.keyboard.press('Tab'); toSave += 1; a = await active() }
    const onSave = a.tag === 'BUTTON'
    await kp.keyboard.press('Enter')
    const saved = await waitMessage(kp, '已保存', 10000)
    await lib.sleep(500)
    const afterSave = await active()
    await lib.shot(env, kp, 'g2-B4-after-save')
    // 焦点应在当前选中的状态上：按空格不应改变状态
    await kp.keyboard.press('Space')
    await lib.sleep(400)
    const afterSpace = await active()
    const dirtyAfterSpace = await drawer.getByRole('button', { name: /保\s*存/ }).isEnabled()
    await kp.keyboard.press('Escape')
    let closed = false
    try { await drawer.waitFor({ state: 'hidden', timeout: 5000 }); closed = true } catch { /* 记为失败 */ }
    await lib.sleep(600)
    const afterClose = await active()
    const ariaWarn = kp._qaConsole.filter((t) => /aria-hidden/i.test(t))
    const server = await kb.api.get(`/projects/${pid}/consistency/issues/${firstIssue}`)
    r.check('B-4', reachedIssue && onSave && saved && afterSave.tag === 'INPUT' && afterSave.type === 'radio' && afterSave.inDrawer
      && afterSave.checked && afterSave.value === radioAfter && afterSpace.value === radioAfter && afterSpace.checked && !dirtyAfterSpace
      && closed && afterClose.issue === firstIssue && ariaWarn.length === 0,
    `Tab ${tabs} 次到问题 #${firstIssue}；回车打开抽屉，Tab ${inner} 次到状态单选（${radioBefore}），方向键改为 ${radioAfter}，`
      + `Tab ${toSave} 次到「保存」回车：提示已保存 ${saved}，服务端状态 ${server.status}；保存后焦点 ${afterSave.tag}[${afterSave.type}] `
      + `value=${afterSave.value} checked=${afterSave.checked} 在抽屉内 ${afterSave.inDrawer}；按空格后焦点 value=${afterSpace.value} checked=${afterSpace.checked}、「保存」可用（有未保存修改）${dirtyAfterSpace}；Esc 关闭抽屉 ${closed}；关闭后焦点 ${afterClose.tag} `
      + `data-consistency-issue=${afterClose.issue}；aria-hidden 警告 ${ariaWarn.length} 条`)
    r.note('console', [...page._qaConsole, ...kp._qaConsole].slice(0, 6).join(' | ') || '无控制台错误/警告')
    await kb.context.close()
  } finally {
    await browser.close()
  }
  process.exit(r.save(env) ? 1 : 0)
}

main().catch((err) => { console.error(err); process.exit(2) })
