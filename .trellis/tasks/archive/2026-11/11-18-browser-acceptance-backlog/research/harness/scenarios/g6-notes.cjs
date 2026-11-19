// G6 随手笔记：只读账号界面（B1–B4、B9，查看者 qa_viewer 与只读管理员 qa_admin_ro 各走一遍）与移动端抽屉（B5–B8，作者）。
// 场景表：research/targets-categories-notes-skills.md B.5。窄屏先设 375×812 再加载页面，中途不改宽度
// （跨断点丢草稿是 TODOS 第 23 项已知问题）。B6 会新建一篇「QA 抽屉新笔记」并把它固定，供 B8 切换使用。
// 运行：QA_DIR=<dir> node scenarios/g6-notes.cjs
'use strict'
const lib = require('../lib.cjs')

const visible = async (locator, timeout = 8000) => { try { await locator.first().waitFor({ state: 'visible', timeout }); return true } catch { return false } }
const absent = async (locator) => (await locator.count()) === 0
const btn = (scope, text) => scope.getByRole('button', { name: new RegExp(`(^|\\s)${text.split('').join('\\s?')}$`) })

async function main() {
  const env = lib.loadEnv()
  if (!env.fixtures) throw new Error('先运行 seed.py')
  const r = lib.reporter('g6-notes')
  const pid = env.fixtures.main_project.id
  const chapterId = env.fixtures.main_project.chapters['1']
  const notesUrl = `/projects/${pid}/notes`
  const editorUrl = `/projects/${pid}/chapters/${chapterId}`
  const browser = await lib.launch()
  const consoles = []
  try {
    for (const who of ['qa_viewer', 'qa_admin_ro']) {
      // ---------- B1–B4：桌面 ----------
      const s = await lib.contextAs(browser, env, who, 'desktop')
      const p = s.page
      consoles.push(p)
      const proj = await s.api.get(`/projects/${pid}`)
      const acc = proj.access || proj
      r.note(`${who}-access`, `role=${acc.role} is_collaborator=${acc.is_collaborator} can_edit_content=${acc.can_edit_content} can_use_ai=${acc.can_use_ai}`)

      await p.goto(notesUrl)
      await visible(p.getByRole('button', { name: '江湖地图草稿' }), 15000)
      const noCreate = await absent(p.getByRole('button', { name: /新建笔记/ }))
      const row = (title) => p.locator('.ant-list-item').filter({ has: p.getByRole('button', { name: title, exact: true }) })
      const mapStatic = await row('江湖地图草稿').getByRole('img', { name: '已收藏' }).count() + await row('江湖地图草稿').getByRole('img', { name: '已固定' }).count()
      const swordStatic = await row('佩剑来历').getByRole('img', { name: '已收藏' }).count()
      const swordNoPin = await absent(row('佩剑来历').getByRole('img', { name: '已固定' }))
      const flagButtons = await p.getByRole('button', { name: /^(取消)?(收藏|固定)「/ }).count()
      const miscFlags = await row('杂记').locator('[aria-label="已收藏"],[aria-label="已固定"],.anticon-star,.anticon-pushpin').count()
      const pinnedTag = await visible(row('江湖地图草稿').getByText('已固定', { exact: true }))
      await p.getByRole('searchbox', { name: '搜索作品笔记' }).fill('佩剑')
      await p.getByRole('searchbox', { name: '搜索作品笔记' }).press('Enter')
      await lib.sleep(1000)
      const searchTitles = await p.locator('.ant-list-item .ant-btn-link').allInnerTexts()
      await p.getByRole('searchbox', { name: '搜索作品笔记' }).fill('')
      await p.getByRole('searchbox', { name: '搜索作品笔记' }).press('Enter')
      await lib.sleep(600)
      await p.locator('.ant-segmented-item').filter({ hasText: '收藏' }).click()
      await lib.sleep(1000)
      const favTitles = await p.locator('.ant-list-item .ant-btn-link').allInnerTexts()
      await p.locator('.ant-segmented-item').filter({ hasText: '全部' }).click()
      await lib.sleep(800)
      await lib.shot(env, p, `g6-B1-${who}-list`)
      r.check(`B1(${who})`, noCreate && mapStatic === 2 && swordStatic === 1 && swordNoPin && flagButtons === 0 && miscFlags === 0
        && searchTitles.length === 1 && searchTitles[0] === '佩剑来历' && favTitles.length === 2,
        `无「新建笔记」：${noCreate}；「江湖地图草稿」静态星标+图钉 ${mapStatic} 个、「已固定」标签 ${pinnedTag}；「佩剑来历」静态星标 ${swordStatic}、无图钉 ${swordNoPin}；`
        + `可点击的收藏/固定按钮 ${flagButtons} 个；「杂记」行标记元素 ${miscFlags} 个；搜索「佩剑」→ ${JSON.stringify(searchTitles)}；「收藏」筛选 → ${JSON.stringify(favTitles)}`)

      // B2
      await p.getByRole('button', { name: '江湖地图草稿', exact: true }).click()
      const dr = p.locator('.ant-drawer-content')
      const title = await visible(dr.getByText('查看笔记', { exact: true }), 10000)
      const roText = await visible(dr.getByText('只读查看', { exact: true }), 10000)
      const noSave = await absent(btn(dr, '保存'))
      const noDel = await absent(dr.getByRole('button', { name: /删除笔记/ }))
      const noSwitch = await absent(dr.locator('.ant-segmented'))
      const noInputs = (await dr.locator('textarea, input[type="text"], input:not([type])').count()) === 0
      const body = (await dr.locator('section[aria-label="笔记原文"]').innerText()).replace(/\s+/g, ' ')
      await lib.shot(env, p, `g6-B2-${who}-view`)
      r.check(`B2(${who})`, title && roText && noSave && noDel && noSwitch && noInputs && body.includes('江湖地图草稿') && body.includes('临江城'),
        `抽屉标题「查看笔记」：${title}；「只读查看」：${roText}；无保存 ${noSave}／删除 ${noDel}／编辑切换 ${noSwitch}；无输入框 ${noInputs}；只读正文「${body.slice(0, 40)}」`)

      // B3
      await p.goto(`${notesUrl}?new=1`)
      const denied = await visible(p.getByText('当前账号无权创建笔记'), 10000)
      const drTitle = denied ? (await p.locator('.ant-drawer-title').innerText()) : ''
      await lib.shot(env, p, `g6-B3-${who}-new`)
      r.check(`B3(${who})`, denied, `访问 ?new=1：抽屉「${drTitle}」内显示「当前账号无权创建笔记」：${denied}`)

      // B4：章节编辑页笔记栏（桌面）
      await p.goto(editorUrl)
      await visible(p.getByRole('tab', { name: /笔记/ }), 20000)
      const tabs = (await p.getByRole('tab').allInnerTexts()).map((t) => t.trim())
      await p.getByRole('tab', { name: /笔记/ }).click()
      const pinnedList = p.locator('ul[aria-label="固定笔记"]')
      await visible(pinnedList, 10000)
      const panel = p.locator('.ant-card').filter({ has: pinnedList })
      const noNew = await absent(btn(panel, '新建'))
      const emptyText = (await panel.getByText(/即可在写作时对照查看/).innerText()).trim()
      await pinnedList.getByRole('button', { name: /江湖地图草稿/ }).click()
      const roSide = await visible(panel.getByText('只读查看', { exact: true }), 10000)
      const sideNoSave = await absent(btn(panel, '保存'))
      const sideNoInputs = (await panel.locator('textarea').count()) === 0
      await panel.getByRole('combobox', { name: '搜索并打开其他笔记' }).click()
      await lib.sleep(800)
      await p.locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option[title="杂记"]').click({ force: true })
      await lib.sleep(1000)
      const sideBody = (await panel.locator('section[aria-label="笔记原文"]').innerText().catch(() => '')).replace(/\s+/g, ' ')
      const roSide2 = await visible(panel.getByText('只读查看', { exact: true }))
      await lib.shot(env, p, `g6-B4-${who}-sidepanel`)
      r.check(`B4(${who})`, !tabs.some((t) => t.includes('AI 助手')) && noNew && !emptyText.includes('或新建笔记') && roSide && sideNoSave && sideNoInputs && sideBody.includes('杂记') && roSide2,
        `检视面板标签 ${JSON.stringify(tabs)}；无「新建」${noNew}；空态「${emptyText}」；打开固定笔记为只读 ${roSide}、无保存 ${sideNoSave}、无输入框 ${sideNoInputs}；搜索打开「杂记」→「${sideBody.slice(0, 20)}」只读 ${roSide2}`)
      await s.context.close()

      // ---------- B9：375×812 的移动端抽屉 ----------
      const m = await lib.contextAs(browser, env, who, 'mobile')
      const mp = m.page
      consoles.push(mp)
      await mp.goto(editorUrl)
      const fab = mp.locator('.ant-float-btn-primary')
      await visible(fab, 20000)
      await fab.click()
      const mdr = mp.locator('.ant-drawer-content-wrapper').filter({ has: mp.locator('.ant-segmented') })
      await visible(mdr, 10000)
      const segs = (await mdr.locator('.ant-drawer-extra .ant-segmented-item').allInnerTexts()).map((t) => t.trim())
      await mdr.locator('.ant-drawer-extra .ant-segmented-item').filter({ hasText: '笔记' }).click()
      const mTitle = (await mdr.locator('.ant-drawer-title').innerText()).trim()
      const mNoNew = await absent(btn(mdr, '新建'))
      const mEmpty = (await mdr.getByText(/即可在写作时对照查看/).innerText().catch(() => '')).trim()
      await mdr.locator('ul[aria-label="固定笔记"]').getByRole('button', { name: /江湖地图草稿/ }).click()
      const mRo = await visible(mdr.getByText('只读查看', { exact: true }), 10000)
      const mNoSave = await absent(btn(mdr, '保存'))
      const mOverflow = await lib.noHorizontalOverflow(mp)
      await lib.shot(env, mp, `g6-B9-${who}-mobile`)
      r.check(`B9(${who})`, !segs.some((t) => t.includes('AI 助手')) && mTitle.includes('笔记') && mNoNew && mEmpty && !mEmpty.includes('或新建笔记') && mRo && mNoSave && mOverflow,
        `分段控件 ${JSON.stringify(segs)}；抽屉标题「${mTitle}」；无「新建」${mNoNew}；空态「${mEmpty}」；固定笔记只读 ${mRo}、无保存 ${mNoSave}；无横向溢出 ${mOverflow}`)
      await m.context.close()
    }

    // ---------- B5–B8：作者，375×812 ----------
    const a = await lib.contextAs(browser, env, 'qa_author', 'mobile')
    const p = a.page
    consoles.push(p)
    const chapterBefore = await a.api.get(`/projects/${pid}/chapters/${chapterId}`)
    await p.goto(editorUrl)
    const fab = p.locator('.ant-float-btn-primary')
    await visible(fab, 20000)
    // B5
    const noSidebar = await absent(p.getByRole('button', { name: /收起侧栏|辅助面板/ }))
    const noInspectorTabs = await absent(p.getByRole('tab', { name: /笔记/ }))
    await fab.click()
    const dr = p.locator('.ant-drawer-content-wrapper').filter({ has: p.locator('.ant-segmented') })
    await visible(dr, 10000)
    await lib.sleep(500)
    const segs = (await dr.locator('.ant-drawer-extra .ant-segmented-item').allInnerTexts()).map((t) => t.trim())
    await dr.locator('.ant-drawer-extra .ant-segmented-item').filter({ hasText: '笔记' }).click()
    const dTitle = (await dr.locator('.ant-drawer-title').innerText()).trim()
    const box = await dr.boundingBox()
    const overflow = await lib.noHorizontalOverflow(p)
    await lib.shot(env, p, 'g6-B5-author-drawer')
    r.check('B5', noSidebar && noInspectorTabs && dTitle === '📝 笔记' && box && Math.abs(box.height - 812 * 0.85) < 4 && overflow,
      `无桌面侧栏（无收起侧栏按钮 ${noSidebar}、无检视标签 ${noInspectorTabs}）；底部抽屉高 ${box && Math.round(box.height)}px（视口 812 的 85% ≈ 690）；分段控件 ${JSON.stringify(segs)}；标题「${dTitle}」；无横向溢出 ${overflow}`)

    // B6：编辑保存 + 新建保存
    const pinned = dr.locator('ul[aria-label="固定笔记"]')
    const status = () => dr.getByText(/有未保存修改|已保存 · |^新笔记$/).first().innerText()
    await pinned.getByRole('button', { name: /江湖地图草稿/ }).click()
    const text = dr.locator('textarea').first()
    await visible(text, 10000)
    const original = await text.inputValue()
    await text.fill(`${original}\n（抽屉内补记）`)
    const st1 = await status()
    await btn(dr, '保存').click()
    await visible(dr.getByText(/已保存 · /), 10000)
    const st2 = await status()
    const savedNote = await a.api.get(`/projects/${pid}/notes/${env.fixtures.notes['江湖地图草稿']}`)
    await btn(dr, '新建').click()
    await visible(dr.getByText('新笔记', { exact: true }), 10000)
    await dr.getByLabel('标题').fill('QA 抽屉新笔记')
    await dr.locator('textarea').first().fill('移动端抽屉里新建的笔记。')
    await btn(dr, '保存').click()
    const newSaved = await visible(dr.getByText(/已保存 · /), 10000)
    await dr.locator('textarea').first().fill('移动端抽屉里新建的笔记。继续补充一句。')
    const st3 = await status()
    await btn(dr, '保存').click()
    await visible(dr.getByText(/已保存 · /), 10000)
    const st4 = await status()
    // 固定新笔记，供 B8 切换
    await dr.getByRole('button', { name: '固定「QA 抽屉新笔记」' }).click()
    await visible(pinned.getByRole('button', { name: /QA 抽屉新笔记/ }), 10000)
    const chapterAfter = await a.api.get(`/projects/${pid}/chapters/${chapterId}`)
    const bodySame = chapterAfter.content === chapterBefore.content && chapterAfter.content_revision === chapterBefore.content_revision
    await lib.shot(env, p, 'g6-B6-author-saved')
    r.check('B6', st1 === '有未保存修改' && st2.startsWith('已保存 · ') && savedNote.content.includes('（抽屉内补记）') && newSaved && st3 === '有未保存修改' && st4.startsWith('已保存 · ') && bodySame,
      `改固定笔记：保存前「${st1}」→ 保存后「${st2}」，服务端正文含补记 ${savedNote.content.includes('（抽屉内补记）')}；新建「QA 抽屉新笔记」保存 ${newSaved}，继续编辑「${st3}」→ 再保存「${st4}」；章节正文与 content_revision 未变 ${bodySame}（补建：新笔记已固定，供 B8）`)

    // B7：草稿在切标签、关开抽屉后保留
    await pinned.getByRole('button', { name: /江湖地图草稿/ }).click()
    await visible(dr.getByText(/已保存 · /), 10000)
    const draftText = `${await dr.locator('textarea').first().inputValue()}\n未保存的草稿行`
    await dr.locator('textarea').first().fill(draftText)
    await dr.locator('.ant-drawer-extra .ant-segmented-item').filter({ hasText: '场景规划' }).click()
    await lib.sleep(500)
    const hiddenWhileScene = !(await dr.locator('textarea').first().isVisible().catch(() => false)) || (await dr.locator('.ant-drawer-title').innerText()).includes('场景')
    await dr.locator('.ant-drawer-extra .ant-segmented-item').filter({ hasText: '笔记' }).click()
    await lib.sleep(400)
    const afterTab = await dr.locator('ul[aria-label="固定笔记"] ~ * textarea, textarea').evaluateAll((els, want) => els.some((e) => e.value === want), draftText)
    const stTab = await status()
    await dr.locator('.ant-drawer-close').click()
    await lib.sleep(600)
    await fab.click()
    await lib.sleep(600)
    const afterReopen = await dr.locator('textarea').evaluateAll((els, want) => els.some((e) => e.value === want), draftText)
    const stReopen = await status()
    await lib.shot(env, p, 'g6-B7-author-draft-kept')
    r.check('B7', hiddenWhileScene && afterTab && stTab === '有未保存修改' && afterReopen && stReopen === '有未保存修改',
      `切到「场景规划」再切回：草稿保留 ${afterTab}、状态「${stTab}」；关闭抽屉再打开：草稿保留 ${afterReopen}、状态「${stReopen}」`)

    // B8：未保存时切换笔记与离开页面
    await pinned.getByRole('button', { name: /QA 抽屉新笔记/ }).click()
    const modal = p.locator('.ant-modal-confirm, .ant-modal').filter({ hasText: '笔记有未保存修改' })
    const confirmShown = await visible(modal, 5000)
    const confirmText = confirmShown ? (await modal.innerText()).replace(/\s+/g, ' ') : ''
    const hasDiscard = confirmShown && await visible(modal.getByRole('button', { name: '放弃修改并切换' }))
    const hasKeep = confirmShown && await visible(modal.getByRole('button', { name: '继续编辑' }))
    await lib.shot(env, p, 'g6-B8-author-switch-confirm')
    if (hasKeep) await modal.getByRole('button', { name: '继续编辑' }).click()
    await lib.sleep(500)
    const stillDraft = await dr.locator('textarea').evaluateAll((els, want) => els.some((e) => e.value === want), draftText)
    await dr.locator('.ant-drawer-close').click()
    await lib.sleep(500)
    await p.getByRole('button', { name: '打开菜单' }).click()
    await lib.sleep(600)
    await p.locator('.studio-mobile-drawer').getByRole('link', { name: /作品概览/ }).click()
    const leave = p.locator('.ant-modal').filter({ hasText: /留\s?下/ })
    const leaveShown = await visible(leave, 5000)
    const leaveText = leaveShown ? (await leave.innerText()).replace(/\s+/g, ' ') : ''
    await lib.shot(env, p, 'g6-B8-author-leave-block')
    if (leaveShown) await btn(leave, '留下').click()
    await lib.sleep(500)
    const stayed = new URL(p.url()).pathname === editorUrl
    r.check('B8', confirmShown && hasDiscard && hasKeep && stillDraft && leaveShown && /笔记栏/.test(leaveText) && stayed,
      `切换笔记弹窗「${confirmText}」（放弃修改并切换 ${hasDiscard}／继续编辑 ${hasKeep}）；继续编辑后草稿保留 ${stillDraft}；点侧栏「作品概览」拦截框「${leaveText}」；点留下后仍在本章 ${stayed}`)
    await a.context.close()

    const errs = consoles.flatMap((pg) => pg._qaConsole).filter((t) => !t.includes('warning'))
    r.note('console', errs.length ? errs.slice(0, 6).join(' | ') : '无控制台错误')
  } finally {
    await browser.close()
  }
  process.exit(r.save(env) ? 1 : 0)
}

main().catch((err) => { console.error(err); process.exit(2) })
