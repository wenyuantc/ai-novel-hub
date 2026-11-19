// G5 A2 补充：只读身份在分类结果栏实际翻页。
// 结果页大小默认 20（frontend/src/components/settingCategories/categoryQuery.ts:21 initialCategoryFilters；:42 只接受 20/50/100）。
// 作者经接口补建 22 个带「医者」标签的角色，使分类「医者」结果为 23 条（原有苏晴），超过一页；再用 qa_admin_ro 翻页。
// 运行：QA_DIR=<dir> node scenarios/g5-categories-paging.cjs（在 seed.py 之后；可重复执行，已存在的补建角色不重复建）
'use strict'
const lib = require('../lib.cjs')

const EXTRA = 22
const visible = async (locator, timeout = 8000) => { try { await locator.first().waitFor({ state: 'visible', timeout }); return true } catch { return false } }
const absent = async (locator) => (await locator.count()) === 0

async function main() {
  const env = lib.loadEnv()
  if (!env.fixtures) throw new Error('先运行 seed.py')
  const r = lib.reporter('g5-categories-paging')
  const pid = env.fixtures.main_project.id
  const catId = env.fixtures.categories['医者']
  const browser = await lib.launch()
  try {
    // 补建夹具（作者）
    const author = await lib.contextAs(browser, env, 'qa_author', 'desktop')
    const listed = await author.api.get(`/projects/${pid}/characters`)
    const existing = new Set((listed.items || listed).map((c) => c.name))
    let created = 0
    for (let i = 1; i <= EXTRA; i += 1) {
      const name = `QA医者${String(i).padStart(2, '0')}`
      if (existing.has(name)) continue
      const ch = await author.api.post(`/projects/${pid}/characters`, { name, role_type: '配角' })
      const cats = await author.api.get(`/projects/${pid}/setting-categories`)
      await author.api.patch(`/projects/${pid}/setting-entries/character/${ch.id}/tags`, {
        setting_tags: ['医者'], expected_categories_revision: cats.categories_revision ?? cats.revision ?? 0,
      })
      created += 1
    }
    const total = (await author.api.get(`/projects/${pid}/setting-categories/${catId}/entries?page=1&page_size=20`)).total
    r.note('fixture', `补建 ${created} 个带「医者」标签的角色（QA医者01–${EXTRA}），分类「医者」结果共 ${total} 条`)
    await author.context.close()

    // 只读翻页
    const ro = await lib.contextAs(browser, env, 'qa_admin_ro', 'desktop')
    const p = ro.page
    await p.goto(`/projects/${pid}/setting-categories?id=${catId}`)
    const card = p.locator('.ant-card').filter({ has: p.getByText('分类结果', { exact: true }) })
    await visible(card.getByText(/共 \d+ 条匹配设定/), 15000)
    const rows = () => card.locator('.ant-table-tbody tr td:first-child strong').allInnerTexts()
    const totalText = (await card.getByText(/共 \d+ 条匹配设定/).first().innerText()).trim()
    const page1 = await rows()
    const pager = card.getByRole('navigation', { name: '分类结果分页' }).or(card.locator('.ant-pagination'))
    const pagerShown = await visible(pager)
    await card.locator('.ant-pagination-item-2').click()
    await p.waitForFunction(() => new URLSearchParams(location.search).get('page') === '2', null, { timeout: 10000 }).catch(() => {})
    await lib.sleep(800)
    const page2 = await rows()
    const urlPage2 = new URL(p.url()).search
    const activeItem = (await card.locator('.ant-pagination-item-active').innerText()).trim()
    const ro1 = await visible(p.getByText('当前为只读，可查看完整规则、匹配结果和草稿预览。'))
    const ro2 = await absent(p.getByRole('button', { name: '新建分类' }))
    const ro3 = await visible(card.getByRole('button', { name: '查看规则 / 预览' }))
    const ro4 = await absent(card.getByRole('button', { name: '编辑规则 / 草稿预览' }))
    await lib.shot(env, p, 'g5-A2-ro-page2')
    await card.locator('.ant-pagination-prev').click()
    await lib.sleep(800)
    const back = await rows()
    const overlap = page2.filter((n) => page1.includes(n))
    r.check('A2-paging', pagerShown && page1.length === 20 && page2.length === total - 20 && overlap.length === 0 && activeItem === '2'
      && /page=2/.test(urlPage2) && JSON.stringify(back) === JSON.stringify(page1) && ro1 && ro2 && ro3 && ro4,
      `「${totalText}」；分页器 ${pagerShown}；第 1 页 ${page1.length} 条（${page1[0]}…${page1[page1.length - 1]}）；点「2」后第 2 页 ${page2.length} 条 ${JSON.stringify(page2)}，与第 1 页无重叠，`
      + `当前页码 ${activeItem}，URL ${decodeURIComponent(urlPage2)}；点上一页回到第 1 页且内容一致 ${JSON.stringify(back) === JSON.stringify(page1)}；`
      + `翻页后只读态不变：只读提示 ${ro1}、无新建分类 ${ro2}、「查看规则 / 预览」${ro3}、无「编辑规则 / 草稿预览」${ro4}`)
    const errs = p._qaConsole.filter((t) => !t.includes('warning'))
    r.note('console', errs.length ? errs.slice(0, 4).join(' | ') : '无控制台错误')
    await ro.context.close()
  } finally {
    await browser.close()
  }
  process.exit(r.save(env) ? 1 : 0)
}

main().catch((err) => { console.error(err); process.exit(2) })
