// G2 B-3：SQLite 上编辑者 qa_editor（本人无模型、作者授权）发起全书一致性检查，假模型抽取首字节延迟分别 2.5 秒与 6 秒。
// 判定口径（PRD「已知的验证上限」）：页面最终离开「进行中」并显示结果即通过；进度流是否中断、恢复耗时如实记录。
// 每轮前由作者经接口改 5 章，使检查需要重新抽取 5 章。
// 运行：QA_DIR=<dir> node scenarios/g2-b3-slow-model.cjs
'use strict'
const lib = require('../lib.cjs')
const { PID_OF, CH, patchChapter, visible } = require('./g12-common.cjs')

const STALE_CHAPTERS = [6, 7, 9, 11, 13]

async function round(env, browser, author, delay, label) {
  const pid = PID_OF(env)
  for (const no of STALE_CHAPTERS) {
    await patchChapter(author.api, pid, CH(env, no), `第${no}章。林远在路上走了一程（${label}）。路边茶棚冒着热气。`)
  }
  await lib.setDelay(env, { first_byte: delay, per_chunk: 0, kinds: ['extract'] })
  const editor = await lib.contextAs(browser, env, 'qa_editor', 'desktop')
  const page = editor.page
  const t0 = Date.now()
  const at = () => ((Date.now() - t0) / 1000).toFixed(1)
  const streams = []
  page.on('request', (req) => { if (/\/tasks\/\d+\/stream/.test(req.url())) streams.push({ start: at(), end: null, how: null, req }) })
  page.on('requestfinished', (req) => { const s = streams.find((x) => x.req === req); if (s) { s.end = at(); s.how = 'finished' } })
  page.on('requestfailed', (req) => { const s = streams.find((x) => x.req === req); if (s) { s.end = at(); s.how = `failed:${req.failure()?.errorText}` } })

  await page.goto(`/projects/${pid}/consistency`)
  await page.getByRole('button', { name: '发起检查' }).click()
  const modal = page.getByRole('dialog', { name: '发起一致性检查' })
  await modal.getByText('将调用模型抽取事实').waitFor({ timeout: 15000 })
  const preview = (await modal.locator('.ant-descriptions').innerText()).replace(/\s+/g, ' ')
  const ownerNote = await visible(modal.getByText(/作者授权/), 2000)
  await modal.getByRole('button', { name: '开始检查' }).click()
  const tStart = Date.now()
  const events = []
  const seen = new Set()
  const mark = (key, text) => { if (!seen.has(key)) { seen.add(key); events.push(`${at()}s ${text}`) } }
  let doneMsg = false
  let stages = []
  const deadline = Date.now() + 240000
  while (Date.now() < deadline) {
    const inProgress = await page.getByText('一致性检查进行中').first().isVisible().catch(() => false)
    const generic = await page.getByText('检查进行中', { exact: true }).first().isVisible().catch(() => false)
    if (inProgress) {
      mark('progress', '进度条「一致性检查进行中」')
      const st = await page.locator('[role="status"] [aria-live="polite"]').first().textContent().catch(() => '')
      if (st && stages[stages.length - 1] !== st) stages.push(st)
    }
    if (generic) mark('generic', '无实时进度的「检查进行中」')
    if (await page.getByText('连接中断，正在重新连接…').first().isVisible().catch(() => false)) mark('reconnect', '「连接中断，正在重新连接…」')
    if (await page.getByText('暂时无法接收实时进度').first().isVisible().catch(() => false)) mark('lost', '「暂时无法接收实时进度，正在定时查询…」')
    if (await page.locator('.ant-message').getByText('一致性检查已完成').first().isVisible().catch(() => false)) { mark('done', '提示「一致性检查已完成」'); doneMsg = true }
    if (await page.locator('.ant-message').getByText('一致性检查未完成').first().isVisible().catch(() => false)) mark('fail', '提示「一致性检查未完成」')
    const stillRunning = inProgress || generic
    if (seen.has('progress') || seen.has('generic')) {
      if (!stillRunning) { mark('left', '页面离开「进行中」'); break }
    }
    await lib.sleep(400)
  }
  const elapsed = ((Date.now() - tStart) / 1000).toFixed(1)
  await lib.sleep(1500)
  const startEnabled = await page.getByRole('button', { name: '发起检查' }).isEnabled().catch(() => false)
  const lastRun = await page.getByText(/最近一次检查|上次检查/).first().textContent().catch(() => '')
  const summary = await editor.api.get(`/projects/${pid}/consistency/summary`)
  await lib.shot(env, page, `g2-B3-${label}`)
  await lib.setDelay(env, { first_byte: 0, per_chunk: 0, kinds: null })
  const extractCalls = (await lib.modelCalls(env, '?kind=extract')).items.length
  const streamLog = streams.map((s) => `[${s.start}s→${s.end ?? '未结束'}s ${s.how ?? ''}]`).join(' ')
  const pass = seen.has('left') && startEnabled
  await editor.context.close()
  return {
    pass,
    text: `延迟 ${delay}s：预览「${preview}」，作者授权模型提示 ${ownerNote}；发起后 ${elapsed}s 离开进行中；事件：${events.join('，')}；`
      + `阶段文字：${stages.slice(0, 4).join(' / ')}${stages.length > 4 ? ` …共 ${stages.length} 种` : ''}；完成提示 ${doneMsg}；`
      + `「发起检查」恢复可用 ${startEnabled}；概要 running_task_id=${summary.running_task_id}、最近运行 ${JSON.stringify(summary.last_run?.status ?? summary.last_run ?? null)}；`
      + `进度流请求 ${streams.length} 次 ${streamLog}；假模型累计抽取调用 ${extractCalls}${lastRun ? `；页面「${lastRun}」` : ''}`,
  }
}

async function main() {
  const env = lib.loadEnv()
  const r = lib.reporter('g2-b3-slow-model')
  const browser = await lib.launch()
  try {
    const author = await lib.contextAs(browser, env, 'qa_author', 'desktop')
    const a = await round(env, browser, author, 2.5, 'slow2')
    r.check('B-3a', a.pass, a.text)
    const b = await round(env, browser, author, 6, 'slow6')
    r.check('B-3b', b.pass, b.text)
  } finally {
    await lib.setDelay(env, { first_byte: 0, per_chunk: 0, kinds: null }).catch(() => {})
    await browser.close()
  }
  process.exit(r.save(env) ? 1 : 0)
}

main().catch((err) => { console.error(err); process.exit(2) })
