// G5 自定义分类：只读分类页（A1–A4，qa_admin_ro）与读取失败重试（A5–A7，qa_author）。
// 场景表：research/targets-categories-notes-skills.md A.6。读取失败用 page.route 返回 500，放行后点重试，
// 并核对重试确实发出了新请求。只读页另在 375×812 视口复查一次（补充观察）。
// 运行：QA_DIR=<dir> node scenarios/g5-categories.cjs
'use strict'
const lib = require('../lib.cjs')

/** 对匹配的请求按开关返回 500；count 统计所有匹配请求（含放行的）。 */
async function faultRoute(page, pathname, method) {
  const state = { failing: false, count: 0, failed: 0 }
  await page.route((url) => new URL(url).pathname === pathname, async (route) => {
    if (route.request().method() !== method) return route.continue()
    state.count += 1
    if (state.failing) { state.failed += 1; return route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'QA 注入的读取失败' }) }) }
    return route.continue()
  })
  return state
}

const visible = async (locator, timeout = 8000) => { try { await locator.first().waitFor({ state: 'visible', timeout }); return true } catch { return false } }
const absent = async (locator) => (await locator.count()) === 0

async function main() {
  const env = lib.loadEnv()
  if (!env.fixtures) throw new Error('先运行 seed.py')
  const r = lib.reporter('g5-categories')
  const pid = env.fixtures.main_project.id
  const cats = env.fixtures.categories
  const url = `/projects/${pid}/setting-categories`
  const browser = await lib.launch()
  try {
    // ---------- 只读：qa_admin_ro（仅 projects.read 的管理员兼查看者） ----------
    const ro = await lib.contextAs(browser, env, 'qa_admin_ro', 'desktop')
    const proj = await ro.api.get(`/projects/${pid}`).catch(() => null)
    const access = proj && (proj.access || proj)
    if (access) r.note('access-ro', `qa_admin_ro 权限快照 role=${access.role} is_collaborator=${access.is_collaborator} can_edit_content=${access.can_edit_content} can_use_ai=${access.can_use_ai}`)
    const p = ro.page
    await p.goto(url)
    await visible(p.getByText('分类列表'), 15000)
    await visible(p.getByRole('button', { name: /查看分类「主角阵营」的结果/ }), 15000)

    // A1
    const noCreate = await absent(p.getByRole('button', { name: '新建分类' }))
    const roHint = await visible(p.getByText('当前为只读，可查看完整规则、匹配结果和草稿预览。'))
    const viewBtns = await p.getByRole('button', { name: /^查看分类「.+」的结果$/ }).count()
    const ruleBtns = await p.getByRole('button', { name: /^查看分类「.+」的规则$/ }).allInnerTexts()
    const editBtns = await p.getByRole('button', { name: /^编辑分类「.+」的规则$/ }).count()
    await lib.shot(env, p, 'g5-A1-ro-list')
    r.check('A1', noCreate && roHint && viewBtns === 3 && ruleBtns.length === 3 && ruleBtns.every((t) => t.trim() === '规则与预览') && editBtns === 0,
      `无「新建分类」：${noCreate}；只读提示：${roHint}；「查看结果」按钮 ${viewBtns} 个；第二按钮文本 ${JSON.stringify(ruleBtns)}；「编辑规则」按钮 ${editBtns} 个`)

    // A2：查看结果，与作者同条件比对
    await p.getByRole('button', { name: '查看分类「江湖人物」的结果' }).click()
    await visible(p.getByText(/共 \d+ 条匹配设定/), 15000)
    const roTotal = (await p.getByText(/共 \d+ 条匹配设定/).first().innerText()).trim()
    const rowsIn = (pg) => pg.locator('.ant-card').filter({ has: pg.getByText('分类结果', { exact: true }) }).locator('.ant-table-tbody tr td:first-child strong').allInnerTexts()
    const roRows = await rowsIn(p)
    const roResultBtn = await visible(p.getByRole('button', { name: '查看规则 / 预览' }))
    const roEditResultBtn = await absent(p.getByRole('button', { name: '编辑规则 / 草稿预览' }))
    const author = await lib.contextAs(browser, env, 'qa_author', 'desktop')
    await author.page.goto(`${url}?id=${cats['江湖人物']}`)
    await visible(author.page.getByText(/共 \d+ 条匹配设定/), 15000)
    const auTotal = (await author.page.getByText(/共 \d+ 条匹配设定/).first().innerText()).trim()
    const auRows = await rowsIn(author.page)
    const auHasCreate = await visible(author.page.getByRole('button', { name: '新建分类' }))
    const auEditBtn = await visible(author.page.getByRole('button', { name: '编辑规则 / 草稿预览' }))
    // 结果类型筛选（角色）→ URL 带 types，结果仍一致
    // 用键盘选择：下拉选项在动画中点击不稳定
    await p.getByRole('combobox', { name: '筛选分类结果类型' }).click()
    await lib.sleep(600)
    await p.locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option[title="角色"]').click({ force: true })
    await lib.sleep(300)
    await p.keyboard.press('Escape')
    await lib.sleep(800)
    const roTypeTotal = (await p.getByText(/共 \d+ 条匹配设定/).first().innerText()).trim()
    const typeInUrl = new URL(p.url()).search
    // 求值位置：切到按章节／场景再切回
    await p.getByRole('radio', { name: '按章节／场景查看' }).check()
    await lib.sleep(800)
    const historyText = await p.locator('.ant-card').filter({ hasText: '分类结果' }).innerText()
    await p.getByRole('radio', { name: '当前（含有效计划变化）' }).check()
    await lib.sleep(800)
    const backTotal = (await p.getByText(/共 \d+ 条匹配设定/).first().innerText()).trim()
    // 名称搜索
    await p.getByRole('searchbox', { name: '搜索分类结果名称' }).fill('老周')
    await p.getByRole('searchbox', { name: '搜索分类结果名称' }).press('Enter')
    await lib.sleep(1000)
    const searchTotal = (await p.getByText(/共 \d+ 条匹配设定/).first().innerText()).trim()
    const searchRows = await rowsIn(p)
    const pager = await p.getByRole('navigation', { name: '分类结果分页' }).count().catch(() => 0)
    await lib.shot(env, p, 'g5-A2-ro-results')
    r.check('A2', roResultBtn && roEditResultBtn && roTotal === auTotal && JSON.stringify(roRows) === JSON.stringify(auRows) && auHasCreate && auEditBtn
      && /共 \d+ 条/.test(roTypeTotal) && backTotal === roTotal && searchRows.length === 1 && searchRows[0] === '老周',
      `只读「${roTotal}」${JSON.stringify(roRows)}，作者「${auTotal}」${JSON.stringify(auRows)}；只读结果栏按钮「查看规则 / 预览」：${roResultBtn}，无「编辑规则 / 草稿预览」：${roEditResultBtn}；`
      + `类型筛选「角色」后「${roTypeTotal}」（URL ${decodeURIComponent(typeInUrl)}）；切到按章节后出现位置选择：${/章/.test(historyText)}，切回后「${backTotal}」；搜索「老周」→「${searchTotal}」${JSON.stringify(searchRows)}；`
      + `分页器存在：${pager > 0}（夹具每类不足一页，翻页未实操）`)

    // A3：只读抽屉
    await p.goto(url)
    await p.getByRole('button', { name: '查看分类「江湖人物」的规则' }).click()
    const drawer = p.locator('.ant-drawer-content').filter({ hasText: '分类规则与只读预览' })
    const titleOk = await visible(drawer.getByText('分类规则与只读预览'), 15000)
    await visible(drawer.locator('#category-name'), 15000)
    const hintOk = await visible(drawer.getByText('当前只读，可查看完整规则与预览匹配结果。'))
    const nameDisabled = await drawer.locator('#category-name').isDisabled()
    const nameValue = await drawer.locator('#category-name').inputValue()
    const typesDisabled = (await drawer.locator('.ant-select').filter({ has: p.locator('#category-types') }).getAttribute('class')).includes('ant-select-disabled')
    const radios = drawer.getByRole('radio')
    const modeDisabled = await radios.first().isDisabled()
    const sortDisabled = await drawer.locator('#category-sort').isDisabled()
    const tagInputs = drawer.getByRole('textbox', { name: /条件 \d+ 整理标签/ })
    const tagCount = await tagInputs.count()
    let tagsDisabled = true
    for (let i = 0; i < tagCount; i += 1) tagsDisabled = tagsDisabled && await tagInputs.nth(i).isDisabled()
    const ruleSortDisabled = await drawer.getByRole('spinbutton', { name: '条件 1 顺序' }).isDisabled()
    const noMove = await absent(drawer.getByRole('button', { name: /上移条件|下移条件|删除条件/ }))
    const noAdd = await absent(drawer.getByRole('button', { name: '添加条件' }))
    const noDelete = await absent(p.getByRole('button', { name: '删除分类' }))
    const noSave = await absent(p.getByRole('button', { name: '保存分类' }))
    const keepBtn = await visible(p.getByRole('button', { name: '关闭并保留草稿' }))
    await lib.shot(env, p, 'g5-A3-ro-drawer')
    r.check('A3', titleOk && hintOk && nameDisabled && typesDisabled && modeDisabled && sortDisabled && tagCount === 2 && tagsDisabled && ruleSortDisabled && noMove && noAdd && noDelete && noSave && keepBtn,
      `标题：${titleOk}；只读提示：${hintOk}；名称「${nameValue}」禁用：${nameDisabled}；适用类型禁用：${typesDisabled}；匹配方式禁用：${modeDisabled}；显示顺序禁用：${sortDisabled}；`
      + `条件 ${tagCount} 行且标签输入全禁用：${tagsDisabled}，条件顺序禁用：${ruleSortDisabled}；无上移/下移/删除条件：${noMove}；无添加条件：${noAdd}；无删除分类：${noDelete}；无保存：${noSave}；有「关闭并保留草稿」：${keepBtn}`)

    // A4：只读草稿预览
    await drawer.getByRole('button', { name: '预览草稿匹配' }).click()
    const previewOk = await visible(drawer.getByText(/共 \d+ 条匹配设定/), 15000)
    const previewText = previewOk ? (await drawer.getByText(/共 \d+ 条匹配设定/).first().innerText()).trim() : ''
    const previewRows = await drawer.locator('.ant-table-tbody tr td:first-child strong').allInnerTexts()
    const noPreviewErr = await absent(drawer.locator('.ant-alert-error'))
    await lib.shot(env, p, 'g5-A4-ro-preview')
    await p.getByRole('button', { name: '关闭并保留草稿' }).click()
    await lib.sleep(600)
    const noDraftWarn = await absent(p.getByText(/份未保存的分类草稿/))
    r.check('A4', previewOk && noPreviewErr && noDraftWarn,
      `预览返回「${previewText}」${JSON.stringify(previewRows)}；无错误条：${noPreviewErr}；关闭后无「未保存的分类草稿」警告：${noDraftWarn}`)

    // 补充：只读页 375×812
    const roM = await lib.contextAs(browser, env, 'qa_admin_ro', 'mobile')
    await roM.page.goto(`${url}?id=${cats['江湖人物']}`)
    await visible(roM.page.getByRole('button', { name: '查看分类「主角阵营」的结果' }), 15000)
    await visible(roM.page.getByText(/共 \d+ 条匹配设定/), 15000)
    const mNoCreate = await absent(roM.page.getByRole('button', { name: '新建分类' }))
    const mTable = await roM.page.locator('.ant-table').count()
    const mOverflow = await lib.noHorizontalOverflow(roM.page)
    await roM.page.getByRole('button', { name: '查看分类「江湖人物」的规则' }).click()
    const mDrawer = await visible(roM.page.getByText('分类规则与只读预览'), 15000)
    await visible(roM.page.locator('#category-name'), 15000)
    const mNameDisabled = await roM.page.locator('#category-name').isDisabled()
    const mOverflow2 = await lib.noHorizontalOverflow(roM.page)
    await lib.shot(env, roM.page, 'g5-A-mobile-ro-drawer')
    r.note('A1-A4-mobile', `375×812：无新建分类 ${mNoCreate}；列表与结果用卡片（表格 ${mTable} 个）；页面无横向溢出 ${mOverflow}；只读抽屉 ${mDrawer}、名称禁用 ${mNameDisabled}、无横向溢出 ${mOverflow2}`)
    await roM.context.close()
    await ro.context.close()

    // ---------- 读取失败重试：qa_author ----------
    const a = author.page
    // A5：列表读取失败
    const list = await faultRoute(a, `/api/v1/projects/${pid}/setting-categories`, 'GET')
    await a.goto(url)
    await visible(a.getByRole('button', { name: '查看分类「主角阵营」的结果' }), 15000)
    const listCard = a.locator('.ant-card').filter({ has: a.getByText('分类列表', { exact: true }) })
    const namesBefore = await listCard.locator('.ant-table-tbody tr td:first-child strong').allInnerTexts()
    list.failing = true
    const beforeFail = list.count
    await listCard.getByRole('button', { name: /刷新/ }).click()
    const errShown = await visible(listCard.locator('.ant-alert-error'), 10000)
    const errText = errShown ? (await listCard.locator('.ant-alert-error').innerText()).replace(/\s+/g, ' ') : ''
    const retryBtn = await visible(listCard.getByRole('button', { name: /^重\s?试$/ }))
    const noOldRows = await absent(listCard.locator('.ant-table-tbody tr td:first-child strong'))
    const filtersStill = await visible(listCard.getByRole('searchbox', { name: '搜索分类名称' }))
    await lib.shot(env, a, 'g5-A5-list-error')
    list.failing = false
    const beforeRetry = list.count
    await listCard.getByRole('button', { name: /^重\s?试$/ }).click()
    await visible(listCard.locator('.ant-table-tbody tr td:first-child strong'), 10000)
    const namesAfter = await listCard.locator('.ant-table-tbody tr td:first-child strong').allInnerTexts()
    const errGone = await absent(listCard.locator('.ant-alert-error'))
    // 再验证首次进入页面即失败的情形
    list.failing = true
    await a.goto(url)
    const errOnEnter = await visible(listCard.locator('.ant-alert-error'), 10000)
    list.failing = false
    const c0 = list.count
    await listCard.getByRole('button', { name: /^重\s?试$/ }).click()
    const recovered = await visible(listCard.locator('.ant-table-tbody tr td:first-child strong'), 10000)
    r.check('A5', errShown && retryBtn && noOldRows && errGone && JSON.stringify(namesBefore) === JSON.stringify(namesAfter) && list.count > beforeRetry && errOnEnter && recovered && list.count > c0,
      `刷新失败：错误条「${errText}」，有「重试」：${retryBtn}，列表区无旧数据：${noOldRows}，筛选控件仍在：${filtersStill}（失败前已请求 ${beforeFail} 次，注入失败 ${list.failed} 次）；`
      + `放行后点重试新发请求 ${list.count - beforeRetry - (list.count > c0 ? 1 : 0)}+ 次，列表 ${JSON.stringify(namesAfter)} 与失败前 ${JSON.stringify(namesBefore)} 一致，错误条消失：${errGone}；`
      + `首次进入即失败也显示错误条：${errOnEnter}，重试后恢复：${recovered}`)
    await a.unroute(() => true).catch(() => {})
    await a.unrouteAll({ behavior: 'ignoreErrors' })

    // A6：结果读取失败（带 id 与筛选参数）
    const entries = await faultRoute(a, `/api/v1/projects/${pid}/setting-categories/${cats['江湖人物']}/entries`, 'GET')
    // 筛选参数直接经地址栏带入（A2 已验证界面选择「角色」会写入 type=character）
    await a.goto(`${url}?id=${cats['江湖人物']}&type=character&q=${encodeURIComponent('林')}`)
    await visible(a.getByText(/共 \d+ 条匹配设定/), 15000)
    const resCard = a.locator('.ant-card').filter({ has: a.getByText('分类结果', { exact: true }) })
    const totalBefore = (await resCard.getByText(/共 \d+ 条匹配设定/).first().innerText()).trim()
    const rowsBefore = await resCard.locator('.ant-table-tbody tr td:first-child strong').allInnerTexts()
    const searchBefore = new URL(a.url()).search
    entries.failing = true
    await resCard.getByRole('button', { name: /刷新/ }).click()
    const resErr = await visible(resCard.locator('.ant-alert-error'), 10000)
    const resErrText = resErr ? (await resCard.locator('.ant-alert-error').innerText()).replace(/\s+/g, ' ') : ''
    const resRetry = await visible(resCard.getByRole('button', { name: '重试读取' }))
    const filtersKept = await visible(resCard.getByRole('combobox', { name: '筛选分类结果类型' })) && await visible(resCard.getByRole('radio', { name: '当前（含有效计划变化）' }))
    const resNoOld = await absent(resCard.getByText(/共 \d+ 条匹配设定/))
    await lib.shot(env, a, 'g5-A6-results-error')
    entries.failing = false
    const resBeforeRetry = entries.count
    await resCard.getByRole('button', { name: '重试读取' }).click()
    await visible(resCard.getByText(/共 \d+ 条匹配设定/), 10000)
    const totalAfter = (await resCard.getByText(/共 \d+ 条匹配设定/).first().innerText()).trim()
    const rowsAfter = await resCard.locator('.ant-table-tbody tr td:first-child strong').allInnerTexts()
    const searchAfter = new URL(a.url()).search
    r.check('A6', resErr && resRetry && filtersKept && entries.count > resBeforeRetry && totalAfter === totalBefore && JSON.stringify(rowsAfter) === JSON.stringify(rowsBefore) && searchAfter === searchBefore,
      `失败：错误条「${resErrText}」，「重试读取」：${resRetry}，筛选控件仍在：${filtersKept}，旧结果已清空：${resNoOld}；放行后重试新发请求 ${entries.count - resBeforeRetry} 次；`
      + `结果「${totalAfter}」${JSON.stringify(rowsAfter)} 与失败前「${totalBefore}」一致；URL 参数 ${decodeURIComponent(searchAfter)} 前后不变：${searchAfter === searchBefore}`)
    await a.unrouteAll({ behavior: 'ignoreErrors' })

    // A7（甲）：抽屉配置读取失败
    const one = await faultRoute(a, `/api/v1/projects/${pid}/setting-categories/${cats['主角阵营']}`, 'GET')
    await a.goto(url)
    await visible(a.getByRole('button', { name: '编辑分类「主角阵营」的规则' }), 15000)
    one.failing = true
    await a.getByRole('button', { name: '编辑分类「主角阵营」的规则' }).click()
    const dr = a.locator('.ant-drawer-content').filter({ hasText: '编辑自动分类' })
    const cfgWarn = await visible(dr.getByText('尚未取得完整分类与字段配置'), 10000)
    const cfgRetry = await visible(dr.getByRole('button', { name: '重试读取' }))
    const sessionErr = cfgWarn ? (await dr.locator('.ant-alert-error').first().innerText().catch(() => '')).replace(/\s+/g, ' ') : ''
    await lib.shot(env, a, 'g5-A7a-config-error')
    one.failing = false
    const cfgBefore = one.count
    await dr.getByRole('button', { name: '重试读取' }).click()
    const formShown = await visible(dr.locator('#category-name'), 10000)
    const formName = formShown ? await dr.locator('#category-name').inputValue() : ''
    // A7（乙）：草稿预览失败，草稿不丢
    const preview = await faultRoute(a, `/api/v1/projects/${pid}/setting-categories/preview`, 'POST')
    await dr.locator('#category-name').fill('主角阵营（草稿）')
    await dr.getByRole('textbox', { name: '条件 1 整理标签' }).fill('剑客')
    preview.failing = true
    await dr.getByRole('button', { name: '预览草稿匹配' }).click()
    const pvErr = await visible(dr.getByRole('button', { name: '重试预览' }), 10000)
    const pvErrText = pvErr ? (await dr.locator('.ant-alert-error').last().innerText()).replace(/\s+/g, ' ') : ''
    await lib.shot(env, a, 'g5-A7b-preview-error')
    preview.failing = false
    const pvBefore = preview.count
    await dr.getByRole('button', { name: '重试预览' }).click()
    const pvOk = await visible(dr.getByText(/共 \d+ 条匹配设定/), 10000)
    const pvText = pvOk ? (await dr.getByText(/共 \d+ 条匹配设定/).first().innerText()).trim() : ''
    const pvRows = await dr.locator('.ant-table-tbody tr td:first-child strong').allInnerTexts()
    const draftName = await dr.locator('#category-name').inputValue()
    const draftTag = await dr.getByRole('textbox', { name: '条件 1 整理标签' }).inputValue()
    r.check('A7', cfgWarn && cfgRetry && one.count > cfgBefore && formShown && formName === '主角阵营' && pvErr && preview.count > pvBefore && pvOk && draftName === '主角阵营（草稿）' && draftTag === '剑客',
      `配置读取失败：「尚未取得完整分类与字段配置」${cfgWarn}＋「重试读取」${cfgRetry}，上方错误条「${sessionErr}」；放行后重试新发请求 ${one.count - cfgBefore} 次，表单出现名称「${formName}」；`
      + `预览失败：错误条「${pvErrText}」＋「重试预览」${pvErr}；放行后重试新发请求 ${preview.count - pvBefore} 次，预览「${pvText}」${JSON.stringify(pvRows)}（按草稿标签「剑客」计算），草稿名称「${draftName}」、标签「${draftTag}」保留`)
    // 收尾：放弃这份未保存的本地草稿（未写入服务端）
    await dr.getByRole('button', { name: '放弃此草稿' }).click().catch(() => {})
    await a.unrouteAll({ behavior: 'ignoreErrors' })

    const consoleErrors = [...p._qaConsole, ...a._qaConsole].filter((t) => !t.includes('warning') && !t.includes('500'))
    r.note('console', consoleErrors.length ? consoleErrors.slice(0, 6).join(' | ') : '除注入的 500 外无控制台错误')
    await author.context.close()
  } finally {
    await browser.close()
  }
  process.exit(r.save(env) ? 1 : 0)
}

main().catch((err) => { console.error(err); process.exit(2) })
