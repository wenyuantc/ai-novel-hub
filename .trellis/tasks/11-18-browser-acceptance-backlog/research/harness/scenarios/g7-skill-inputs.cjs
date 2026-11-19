// G7 Skill 输入表单：手动入口 C1–C6（批量、局部改写、场景 AI、区块改写、切换重置）。
// 除界面外，用假模型 requests.jsonl 核对出站内容：默认值、组件内占位、未声明占位原样保留。
// 运行：QA_DIR=<dir> node scenarios/g7-skill-inputs.cjs
'use strict'
const fs = require('fs')
const path = require('path')
const lib = require('../lib.cjs')

// 第二个 Skill：同样的两个输入项，另引用一个提示词组件（组件正文含 {{ focus }}），并带一个未声明占位 {{ era }}
const COMPONENT = { slug: 'qa-voice', name: 'QA 视角组件', content: '【组件】务必从{{ focus }}的视角收束本段。' }
const FULL = {
  name: 'QA 全量占位', slug: 'qa-full', description: '验收用：默认值、组件内占位、未声明占位', category: '写作', triggers: [],
  body: '请以{{ tone }}的基调写作，并重点刻画{{ focus }}的言行与心理。{{> qa-voice }}未声明占位{{ era }}应原样保留。',
  inputs: [{ name: 'tone', label: '基调', kind: 'select', options: ['轻松', '沉重'], default: '轻松' },
    { name: 'focus', label: '重点人物', kind: 'text', required: true }],
}
const filled = (tone, focus) => `请以${tone}的基调写作，并重点刻画${focus}的言行与心理。【组件】务必从${focus}的视角收束本段。未声明占位{{ era }}应原样保留。`

function readRequests(env, sinceSeq) {
  const file = path.join(env.dir, 'requests.jsonl')
  if (!fs.existsSync(file)) return []
  return fs.readFileSync(file, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l)).filter((r) => r.seq > sinceSeq)
}
const bodyText = (r) => (r.body.messages || []).map((m) => (typeof m.content === 'string' ? m.content : JSON.stringify(m.content))).join('\n')
async function seqNow(env) { return (await lib.modelState(env)).seq }

async function pickOption(page, combobox, text) {
  // 无搜索的 Select 上已选值盖住了输入框，强制点击
  await combobox.click({ force: true })
  const option = page.locator('.ant-select-dropdown:visible .ant-select-item-option').filter({ hasText: text }).first()
  await option.waitFor({ timeout: 10000 })
  await option.click()
  await page.waitForTimeout(200)
}
const inputsGroup = (scope, skillName) => scope.getByRole('group', { name: `Skill「${skillName}」的输入项` })

async function setup(env, api) {
  const comps = await api.get('/prompt-components')
  if (!comps.some((c) => c.slug === COMPONENT.slug)) await api.post('/prompt-components', COMPONENT)
  const skills = await api.get('/skills')
  let full = skills.find((s) => s.slug === FULL.slug)
  if (!full) full = await api.post('/skills', FULL)
  const pid = env.fixtures.main_project.id
  const chapters = (await api.get(`/projects/${pid}/chapters`))
  const list = chapters.items || chapters
  const extra = {}
  for (const no of [16, 17]) {
    const hit = list.find((c) => c.chapter_no === no)
    extra[no] = hit ? hit.id : (await api.post(`/projects/${pid}/chapters`, { title: `第${no}回`, chapter_no: no, content: '' })).id
  }
  return { full, extra }
}

async function main() {
  const env = lib.loadEnv()
  const r = lib.reporter('g7-skill-inputs')
  const browser = await lib.launch()
  const pid = env.fixtures.main_project.id
  const tone5 = env.fixtures.skill
  try {
    const author = await lib.contextAs(browser, env, 'qa_author')
    const { full, extra } = await setup(env, author.api)
    const page = author.page
    r.note('setup', `Skill「${full.name}」id ${full.id}；组件 ${COMPONENT.slug}；新增空章 16/17 = ${extra[16]}/${extra[17]}`)
    const preview = await author.api.post(`/skills/${full.id}/preview`, { inputs: { focus: '苏晴' } }).catch((e) => ({ error: e.message }))
    r.note('setup-preview-api', JSON.stringify(preview).slice(0, 300))

    // ---------- C1 / C2：批量 ----------
    // STAGES=editor 时跳过批量（批量会写入第 16/17 章，重跑时需要新环境）
    if (process.env.STAGES !== 'editor') {
    await page.goto(`/projects/${pid}/chapters`)
    await page.getByRole('button', { name: '批量生成' }).click()
    const dlg = page.getByRole('dialog', { name: '批量生成章节' })
    await dlg.waitFor()
    await page.waitForTimeout(800)
    await pickOption(page, dlg.getByRole('combobox', { name: '选择 Skill' }), full.name)
    const group = inputsGroup(dlg, full.name)
    await group.waitFor({ timeout: 5000 })
    const toneShown = await group.locator('.ant-select-selection-item').first().innerText()
    const reqErr = await group.getByText('必填项，请填写').count()
    const okBtn = dlg.getByRole('button', { name: '开始批量生成' })
    await page.waitForTimeout(800)
    const okDisabled = await okBtn.isDisabled()
    await lib.shot(env, page, 'g7-c1-batch-required')
    r.check('C1', toneShown === '轻松' && reqErr === 1 && okDisabled,
      `表单出现；基调预填「${toneShown}」；必填错误条数 ${reqErr}；「开始批量生成」禁用：${okDisabled}`)

    // C6（批量弹窗）：填值后换 Skill 再换回 → 回到默认值
    await group.getByRole('textbox').fill('苏晴')
    await pickOption(page, group.getByRole('combobox'), '沉重')
    await pickOption(page, dlg.getByRole('combobox', { name: '选择 Skill' }), '基调与重点人物')
    await pickOption(page, dlg.getByRole('combobox', { name: '选择 Skill' }), full.name)
    const g2 = inputsGroup(dlg, full.name)
    const afterTone = await g2.locator('.ant-select-selection-item').first().innerText()
    const afterFocus = await g2.getByRole('textbox').inputValue()
    r.check('C6-batch', afterTone === '轻松' && afterFocus === '',
      `批量弹窗：填「沉重/苏晴」→ 换「基调与重点人物」→ 换回后基调「${afterTone}」、重点人物「${afterFocus}」`)

    // C2：填写（基调保持默认「轻松」，验证默认值出站）→ 预览 → 提交
    await g2.getByRole('textbox').fill('苏晴')
    await g2.getByRole('button', { name: '预览填入后的 Skill 正文' }).click()
    const pv = page.getByRole('dialog', { name: `预览：${full.name}` })
    await pv.waitFor()
    await pv.locator('pre').waitFor()
    const pvText = await pv.locator('pre').innerText()
    await lib.shot(env, page, 'g7-c2-batch-preview')
    r.check('C2-preview', pvText.trim() === filled('轻松', '苏晴'), `预览正文：${pvText.trim()}`)
    await pv.getByRole('button', { name: 'Close' }).or(pv.locator('.ant-modal-close')).first().click()
    await page.waitForTimeout(1500)
    const previewItems = await dlg.innerText()
    const seq0 = await seqNow(env)
    await okBtn.waitFor()
    for (let i = 0; i < 20 && await okBtn.isDisabled(); i += 1) await page.waitForTimeout(500)
    await okBtn.click()
    await page.getByText('已开始批量生成').first().waitFor({ timeout: 15000 })
    // 等任务结束
    const tasks = await author.api.get(`/tasks?project_id=${pid}&kind=chapter.batch&limit=1`)
    const batchTask = (tasks.items || tasks)[0]
    const done = await author.api.waitTask(batchTask, 180000)
    const reqs = readRequests(env, seq0).filter((x) => x.kind === 'text')
    const withSkill = reqs.filter((x) => bodyText(x).includes(filled('轻松', '苏晴')))
    const ch16 = await author.api.get(`/projects/${pid}/chapters/${extra[16]}`)
    const ch17 = await author.api.get(`/projects/${pid}/chapters/${extra[17]}`)
    r.check('C2', done.status === 'succeeded' && reqs.length >= 2 && withSkill.length === reqs.length,
      `批量任务 ${done.status}；弹窗预览含「第 16 章」：${previewItems.includes('第 16 章')}；正文请求 ${reqs.length} 次，`
      + `其中含同一段填充正文 ${withSkill.length} 次；第 16/17 章字数 ${ch16.word_count}/${ch17.word_count}`)
    await lib.shot(env, page, 'g7-c2-batch-started')
    }

    // ---------- 章节编辑页：C3 / C4 / C5 ----------
    const chId = env.fixtures.scene_chapter.chapter_id
    await page.goto(`/projects/${pid}/chapters/${chId}`)
    const aiSkill = page.getByRole('combobox', { name: '选择 Skill' })
    await aiSkill.waitFor({ timeout: 15000 })
    await page.waitForTimeout(1000)
    await pickOption(page, aiSkill, full.name)
    const warnText = `请先填写 Skill「${full.name}」的输入项：重点人物`
    const genBtn = page.getByRole('button', { name: /重新生成当前场景|生成当前场景正文/ })
    const c4blocked = (await page.getByText(warnText).count()) >= 1 && await genBtn.isDisabled()
    await lib.shot(env, page, 'g7-c4-required')

    // C3 前半：选中正文一段 → 局部改写 → 警告 + 按钮禁用
    const editor = page.getByRole('textbox', { name: '当前场景正文' })
    const prose = editor.locator('p').filter({ hasText: /林远沿着/ }).first()
    await prose.click({ clickCount: 3 })
    await page.getByRole('tab', { name: '局部改写' }).click()
    await page.waitForTimeout(500)
    const rewriteBtn = page.getByRole('button', { name: /选中内容$/ })
    const rwSelected = await page.getByText(/^已选中 \d+ 字/).first().innerText().catch(() => '（无选区提示）')
    const c3blocked = (await page.getByText(warnText).count()) >= 1 && await rewriteBtn.isDisabled()
    await lib.shot(env, page, 'g7-c3-required')

    // C5 前半：区块内选字 → 选区 AI 编辑 → 警告 + 发送按钮禁用
    const sectionText = page.locator('[data-section-key] p, .rich-section p').filter({ hasText: '林远推开柴门' }).first()
    const sectionFallback = editor.getByText('林远推开柴门，晨雾扑面而来。').first()
    const sectionTarget = (await sectionText.count()) ? sectionText : sectionFallback
    await sectionTarget.click({ clickCount: 3 })
    await page.getByRole('button', { name: '选区 AI 编辑' }).click()
    const secDlg = page.getByRole('dialog').filter({ hasText: '补充要求（可选）' }).filter({ hasText: '放弃并关闭' })
    await secDlg.waitFor({ timeout: 10000 })
    const secAlert = await secDlg.getByText('请在 AI 助手的「Skill（可选）」下方填写。').count()
    const secSend = secDlg.getByRole('button', { name: '继续并确认发送' })
    const c5blocked = secAlert === 1 && await secSend.isDisabled()
    await lib.shot(env, page, 'g7-c5-required')
    await secDlg.getByRole('button', { name: '放弃并关闭' }).click()
    await page.waitForTimeout(500)

    // 填写：基调改「沉重」，重点人物「苏晴」
    const aiGroup = inputsGroup(page, full.name)
    await aiGroup.getByRole('textbox').fill('苏晴')
    await pickOption(page, aiGroup.getByRole('combobox'), '沉重')

    // C3 后半：重新选区 → 改写 → 出站核对
    await prose.click({ clickCount: 3 })
    await page.waitForTimeout(300)
    await page.getByRole('tab', { name: '局部改写' }).click()
    let seq = await seqNow(env)
    const rwEnabled = !(await rewriteBtn.isDisabled())
    await rewriteBtn.click()
    await page.getByText('与原文对比').first().waitFor({ timeout: 30000 })
    let out = readRequests(env, seq).filter((x) => x.kind === 'text')
    await lib.shot(env, page, 'g7-c3-done')
    r.check('C3', c3blocked && rwEnabled && out.length === 1 && bodyText(out[0]).includes(filled('沉重', '苏晴')),
      `留空时改写面板警告且「改写选中内容」禁用：${c3blocked}（${rwSelected}）；填好后可开始：${rwEnabled}；`
      + `出站 ${out.length} 次，含填充正文（沉重/苏晴，组件展开、{{ era }} 原样）：${out.length ? bodyText(out[0]).includes(filled('沉重', '苏晴')) : false}`)
    await page.getByRole('button', { name: '放弃' }).click()

    // C5 后半：区块选字 → 发送 → 确认 → 出站核对
    await sectionTarget.click({ clickCount: 3 })
    await page.getByRole('button', { name: '选区 AI 编辑' }).click()
    await secDlg.waitFor()
    const secEnabled = !(await secSend.isDisabled())
    seq = await seqNow(env)
    await secSend.click()
    await page.getByRole('dialog', { name: '确认本次发送' }).getByRole('button', { name: '确认并发送' }).click()
    await secDlg.getByText('与原选区对比').waitFor({ timeout: 30000 })
    out = readRequests(env, seq).filter((x) => x.kind === 'text')
    await lib.shot(env, page, 'g7-c5-done')
    r.check('C5', c5blocked && secEnabled && out.length === 1 && bodyText(out[0]).includes(filled('沉重', '苏晴')),
      `留空时弹窗警告「请在 AI 助手的「Skill（可选）」下方填写。」且「继续并确认发送」禁用：${c5blocked}；填好后可发送：${secEnabled}；`
      + `出站 ${out.length} 次，含填充正文：${out.length ? bodyText(out[0]).includes(filled('沉重', '苏晴')) : false}`)
    await secDlg.getByRole('button', { name: '放弃并关闭' }).click({ timeout: 10000 })
    await page.waitForTimeout(800)
    await lib.shot(env, page, 'g7-c5-closed')

    // C4：AI 生成 · 当前场景
    await page.getByRole('tab', { name: 'AI 生成' }).click()
    const sceneTexts = async () => {
      const list = await author.api.get(`/projects/${pid}/chapters/${chId}/scenes`)
      const result = []
      for (const sc of (list.items || list)) result.push((await author.api.get(`/projects/${pid}/chapters/${chId}/scenes/${sc.id}/document`)).document.content)
      return result
    }
    const sb = await sceneTexts()
    // 本次场景生成返回带标记的正文，便于确认只改了当前场景
    await lib.clearScripts(env)
    await lib.pushScript(env, { match: 'text', type: 'text', times: 5, text: 'C4场景标记：苏晴站在檐下，雨声沉沉，她把药囊攥得更紧。'.repeat(20) })
    seq = await seqNow(env)
    await genBtn.click()
    const confirmGen = page.getByRole('dialog').filter({ hasText: '重新生成当前场景？' })
    if (await confirmGen.count()) await confirmGen.getByRole('button', { name: /^生\s*成$/ }).click()
    await page.waitForTimeout(1000)
    const t = await author.api.get(`/tasks?project_id=${pid}&limit=1`)
    const sceneTask = (t.items || t)[0]
    // 带分区块的场景：生成结束后停在「待审核」（富文本候选需作者核对），经任务面板「核对候选」采纳
    let sDone
    for (let i = 0; i < 240; i += 1) {
      sDone = await author.api.get(`/tasks/${sceneTask.id}?task_key=${encodeURIComponent(sceneTask.task_key)}`)
      if (['succeeded', 'failed', 'cancelled', 'interrupted'].includes(sDone.status)) break
      if (sDone.status === 'paused' && sDone.rich_review?.phase === 'awaiting_review') break
      await lib.sleep(500)
    }
    let reviewNote = '无需核对'
    if (sDone.status === 'paused') {
      await page.getByRole('button', { name: /后台任务/ }).first().click()
      const panel = page.locator('.ant-drawer:visible')
      await panel.getByRole('button', { name: /核对候选/ }).first().click()
      const review = page.getByRole('dialog', { name: '核对 AI 候选稿' })
      await review.waitFor({ timeout: 15000 })
      await page.waitForTimeout(1500)
      const apply = review.getByRole('button', { name: '确认并采纳整章' })
      // 未能可靠定位的区块：在只读候选里放光标，再「使用候选光标」
      const cand = review.locator('textarea[readonly], [readonly]').first()
      if (await cand.count()) { await cand.click({ position: { x: 20, y: 10 } }); await page.waitForTimeout(300) }
      for (const b of await review.getByRole('button', { name: '使用候选光标' }).all()) if (!(await b.isDisabled())) await b.click()
      await page.waitForTimeout(500)
      const ok = !(await apply.isDisabled())
      await lib.shot(env, page, 'g7-c4-review')
      reviewNote = `停在「待审核」，任务面板「核对候选」弹窗可采纳：${ok}`
      if (ok) {
        await apply.click()
        for (let i = 0; i < 120; i += 1) {
          sDone = await author.api.get(`/tasks/${sceneTask.id}?task_key=${encodeURIComponent(sceneTask.task_key)}`)
          if (sDone.status !== 'paused' && sDone.status !== 'running') break
          await lib.sleep(500)
        }
      }
      await page.keyboard.press('Escape')
      const closeDrawer = page.locator('.ant-drawer:visible .ant-drawer-close')
      if (await closeDrawer.count()) await closeDrawer.first().click()
      await page.waitForTimeout(500)
    }
    await lib.clearScripts(env)
    const sa = await sceneTexts()
    out = readRequests(env, seq).filter((x) => x.kind === 'text')
    const changed = sa.map((s, i) => (s !== sb[i] ? `${i + 1}（含标记：${s.includes('C4场景标记')}）` : null)).filter(Boolean)
    await page.waitForTimeout(1500)
    await lib.shot(env, page, 'g7-c4-done')
    r.check('C4', c4blocked && sDone.status === 'succeeded' && out.length >= 1 && out.every((x) => bodyText(x).includes(filled('沉重', '苏晴')))
      && changed.length === 1 && changed[0].startsWith("1（"),
      `留空时警告且生成按钮禁用：${c4blocked}；任务 ${sceneTask.kind} ${sDone.status}（${reviewNote}）；出站 ${out.length} 次均含填充正文：`
      + `${out.every((x) => bodyText(x).includes(filled('沉重', '苏晴')))}；正文变化的场景：${changed.join('、') || '无'}`)

    // C6（AI 助手）：换 Skill 再换回 → 回到默认
    await pickOption(page, aiSkill, '基调与重点人物')
    await pickOption(page, aiSkill, full.name)
    const g3 = inputsGroup(page, full.name)
    const t3 = await g3.locator('.ant-select-selection-item').first().innerText()
    const f3 = await g3.getByRole('textbox').inputValue()
    r.check('C6-editor', t3 === '轻松' && f3 === '', `AI 助手：填「沉重/苏晴」并用过后换「基调与重点人物」再换回 → 基调「${t3}」、重点人物「${f3}」`)

    const errs = page._qaConsole.filter((x) => !x.includes('warning'))
    r.note('console', errs.length ? errs.slice(0, 5).join(' | ') : '无控制台错误')
    void tone5
  } finally {
    await browser.close()
  }
  process.exit(r.save(env) ? 1 : 0)
}

main().catch((err) => { console.error(err); process.exit(2) })
