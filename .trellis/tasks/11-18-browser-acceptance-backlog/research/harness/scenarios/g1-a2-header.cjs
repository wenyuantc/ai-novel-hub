// A-2 复验：章节编辑页页头在各视口、停靠面板开／关下，标题区（章号、状态、字数、分析状态、修改影响提示）与工具栏是否都可见可点。
// 前置：seed.py；本脚本先经接口改第 5 章使「修改影响」提示出现，最后点提示核对跳转。
// 运行：QA_DIR=<dir> node scenarios/g1-a2-header.cjs
'use strict'
const lib = require('../lib.cjs')
const { PID_OF, CH, patchChapter, visible } = require('./g12-common.cjs')

const VIEWPORTS = [{ width: 1280, height: 900 }, { width: 1440, height: 900 }, { width: 1920, height: 1080 }]

/** 元素中心点没被别的元素遮住（禁用按钮也适用）。 */
async function unobstructed(locator) {
  await locator.scrollIntoViewIfNeeded().catch(() => {})
  return locator.evaluate((el) => {
    const r = el.getBoundingClientRect()
    if (r.width === 0 || r.height === 0) return false
    const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)
    return !!hit && (el === hit || el.contains(hit) || hit.contains(el))
  })
}

async function measure(page) {
  return page.evaluate(() => {
    const card = document.querySelector('.chapter-editor-card')
    const title = card?.querySelector(':scope > .ant-card-head .ant-card-head-title')
    const extra = card?.querySelector(':scope > .ant-card-head .ant-card-extra')
    const box = (el) => { if (!el) return null; const r = el.getBoundingClientRect(); return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) } }
    const tag = Array.from(card?.querySelectorAll('.ant-tag') || []).find((t) => t.textContent.includes('派生内容可能过期'))
    const tr = tag?.getBoundingClientRect(), ttl = title?.getBoundingClientRect()
    return {
      cardClass: !!card, title: box(title), extra: box(extra), tag: box(tag),
      tagTruncated: tag ? (tag.scrollWidth > tag.clientWidth + 1 || !ttl || tr.right > ttl.right + 1 || tr.left < ttl.left - 1) : null,
      tagText: tag?.textContent || '',
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      toolbarInHeader: !!extra,
    }
  })
}

async function checkHeader(page) {
  const m = await measure(page)
  const titleEl = page.locator('.chapter-editor-card > .ant-card-head .ant-card-head-title')
  const parts = {
    章号: titleEl.getByText(/^第 \d+ 章$/),
    状态: titleEl.locator('.ant-tag').first(),
    字数: titleEl.getByText(/当前场景 \d+ 字/),
    分析状态: titleEl.locator('.ant-tag').filter({ hasText: /分析|未分析|过期/ }).filter({ hasNotText: '派生内容' }).first(),
    修改影响: titleEl.locator('a').filter({ hasText: '派生内容可能过期' }),
  }
  const seen = {}
  for (const [k, loc] of Object.entries(parts)) seen[k] = (await loc.count()) > 0 && await unobstructed(loc.first())
  // 提示可点（trial）
  let hintClickable = false
  try { await parts.修改影响.first().click({ trial: true, timeout: 3000 }); hintClickable = true } catch { /* 不可点 */ }
  // 工具栏：页头里或卡片正文首行的按钮
  const toolbar = page.locator('.chapter-editor-card').getByRole('button').filter({ hasText: /上一章|下一章|阅读模式|分析|本章伏笔|连载与存稿|版本历史|本章 AI 用量|本地草稿|标记为完成|保存|收起侧栏|辅助面板/ })
  const n = await toolbar.count()
  const bad = []
  for (let i = 0; i < n; i += 1) {
    const b = toolbar.nth(i)
    const label = ((await b.textContent()) || '').trim()
    if (!await unobstructed(b)) bad.push(`${label}(遮挡)`)
    else if (await b.isEnabled()) { try { await b.click({ trial: true, timeout: 2000 }) } catch { bad.push(`${label}(不可点)`) } }
  }
  return { m, seen, hintClickable, buttons: n, bad }
}

async function main() {
  const env = lib.loadEnv()
  const r = lib.reporter('g1-a2-header')
  const pid = PID_OF(env), c5 = CH(env, 5)
  const browser = await lib.launch()
  try {
    const author = await lib.contextAs(browser, env, 'qa_author', 'desktop')
    await patchChapter(author.api, pid, c5, '山谷一战，林远力竭。林远的佩剑完好无损，他把佩剑收好。他只得赤手下山。')
    const rows = []
    for (const vp of VIEWPORTS) {
      const ctx = await lib.contextAs(browser, env, 'qa_author', vp)
      const page = ctx.page
      await page.goto(`/projects/${pid}/chapters/${c5}`)
      await page.getByRole('textbox', { name: '当前场景正文', exact: true }).waitFor({ timeout: 20000 })
      await page.locator('.chapter-editor-card').getByText(/派生内容可能过期/).first().waitFor({ state: 'attached', timeout: 15000 })
      for (let pass = 0; pass < 2; pass += 1) {
        await lib.sleep(600)
        const open = await page.getByRole('button', { name: '收起侧栏' }).count() > 0
        const res = await checkHeader(page)
        const label = `${vp.width}×${vp.height}/停靠面板${open ? '开' : '关'}`
        await lib.shot(env, page, `a2-${vp.width}-${open ? 'open' : 'closed'}`)
        const ok = res.m.cardClass && res.m.title.w > 0 && Object.values(res.seen).every(Boolean) && res.hintClickable
          && res.m.tagTruncated === false && !res.m.overflow && res.bad.length === 0 && res.buttons >= 10
        rows.push(ok)
        r.check(`A-2 ${label}`, ok, `标题区 ${res.m.title.w}×${res.m.title.h}@y${res.m.title.y}；工具栏${res.m.toolbarInHeader ? `在页头 ${res.m.extra.w}×${res.m.extra.h}@y${res.m.extra.y}` : '不在页头（正文首行）'}；`
          + `可见 ${JSON.stringify(res.seen)}；提示「${res.m.tagText}」${res.m.tag?.w}×${res.m.tag?.h} 截断 ${res.m.tagTruncated}、可点 ${res.hintClickable}；`
          + `横向溢出 ${res.m.overflow}；工具栏按钮 ${res.buttons} 个，问题 ${res.bad.join('、') || '无'}`)
        await page.getByRole('button', { name: open ? '收起侧栏' : '辅助面板' }).click()
      }
      if (vp.width === 1280) {
        // 1280 下点提示核对跳转
        await page.locator('.chapter-editor-card').getByText(/派生内容可能过期/).first().click()
        await page.locator('.ant-drawer-content').getByText('可能过期的派生内容（').waitFor({ timeout: 20000 })
        const url = new URL(page.url()).search
        r.check('A-2 1280 跳转', url.includes('tab=impact') && url.includes(`chapter=${c5}`), `点击提示后地址 ${url}，抽屉已打开`)
      }
      await ctx.context.close()
    }
    // 375 窄屏一瞥
    const m = await lib.contextAs(browser, env, 'qa_author', 'mobile')
    await m.page.goto(`/projects/${pid}/chapters/${c5}`)
    await m.page.getByRole('textbox', { name: '当前场景正文', exact: true }).waitFor({ timeout: 20000 })
    await lib.sleep(800)
    const mm = await measure(m.page)
    const hintM = await visible(m.page.locator('.chapter-editor-card').getByText(/派生内容可能过期/), 3000)
    await lib.shot(env, m.page, 'a2-375')
    r.check('A-2 375×812', mm.title.w > 0 && !mm.overflow && hintM && mm.tagTruncated === false,
      `标题区 ${mm.title.w}×${mm.title.h}；工具栏在页头 ${mm.toolbarInHeader}；提示可见 ${hintM}、截断 ${mm.tagTruncated}；横向溢出 ${mm.overflow}`)
  } finally {
    await browser.close()
  }
  process.exit(r.save(env) ? 1 : 0)
}

main().catch((err) => { console.error(err); process.exit(2) })
