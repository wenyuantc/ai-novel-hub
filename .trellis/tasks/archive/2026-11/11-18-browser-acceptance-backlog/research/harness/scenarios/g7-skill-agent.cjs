// G7 Skill 输入表单：智能体代填路径 C7–C9。
// 按 PRD「已知的验证上限」：只判定输入项定义是否送达、工具调用被受理或拒绝，不判定代填语义。
// 运行：QA_DIR=<dir> node scenarios/g7-skill-agent.cjs
'use strict'
const lib = require('../lib.cjs')
const ag = require('./agent-common.cjs')

const msgText = (r) => (r.body.messages || []).map((m) => `[${m.role}] ${typeof m.content === 'string' ? m.content : JSON.stringify(m.content)}`).join('\n')

async function main() {
  const env = lib.loadEnv()
  const r = lib.reporter('g7-skill-agent')
  const browser = await lib.launch()
  const pid = env.fixtures.main_project.id
  const skill = env.fixtures.skill
  try {
    const author = await lib.contextAs(browser, env, 'qa_author')
    const { page, api } = author
    const skillName = (await api.get(`/skills/${skill.id}`)).name
    await lib.clearScripts(env)

    // ---------- C7：触发词启用 ----------
    let cid = await ag.openNewConversation(page, api, pid)
    const box = page.locator(ag.BOX)
    await box.click()
    await page.keyboard.type('/')
    const opt = page.locator('.ant-select-dropdown:visible .ant-select-item-option').filter({ hasText: `/${skill.trigger}` }).first()
    let optShown = false
    try { await opt.waitFor({ timeout: 5000 }); optShown = true } catch { /* 记录 */ }
    const optText = optShown ? (await opt.innerText()).replace(/\s+/g, ' ') : '（无候选）'
    if (optShown) await opt.click()
    await box.press('End')
    await page.keyboard.type(' 请按这个基调写一段林远下山的开头')
    const typed = await box.inputValue()
    const hint = page.getByText(new RegExp(`本轮将启用 Skill「${skillName}」；它有 2 个输入项`))
    const hintText = (await hint.count()) ? await hint.first().innerText() : '（无提示）'
    const formShown = await page.getByRole('group', { name: `Skill「${skillName}」的输入项` }).count()
    await lib.shot(env, page, 'g7-c7-before-send')
    let seq = (await lib.modelState(env)).seq
    await page.getByRole('button', { name: /发\s*送/ }).click()
    await page.waitForTimeout(800)
    const cleared = (await box.inputValue()) === ''
    await ag.waitTurn(api, pid, cid)
    let reqs = ag.agentRequests(env, seq)
    const sys = reqs.length ? msgText(reqs[0]) : ''
    const hasDefs = sys.includes('【该 Skill 的输入项】') && sys.includes('{{ tone }}') && sys.includes('{{ focus }}')
    const unfilled = sys.includes('{{ tone }}的基调') || sys.includes('请以{{ tone }}')
    await page.waitForTimeout(800)
    const userTag = await page.getByText(`Skill：${skillName}`).count()
    await lib.shot(env, page, 'g7-c7-after-send')
    const defsLine = (sys.split('\n').find((l) => l.includes('{{ focus }}｜')) || '').trim()
    r.check('C7', optShown && hintText.includes('将由智能体根据对话代填') && formShown === 0 && cleared && hasDefs,
      `候选「${optText}」；输入「${typed}」；提示「${hintText}」；表单出现 ${formShown}；发送后清空 ${cleared}；`
      + `消息带 Skill 标签 ${userTag}；模型请求 ${reqs.length} 次，含「【该 Skill 的输入项】」清单 ${hasDefs}（${defsLine}），正文未填充保留占位 ${unfilled}`)

    // ---------- C8：智能体自行 load_skill ----------
    await lib.clearScripts(env)
    await lib.pushScript(env, { match: 'tools', type: 'tool_calls', tool_calls: [{ name: 'load_skill', arguments: { slug: skill.slug } }] })
    seq = (await lib.modelState(env)).seq
    await ag.typeAndSend(page, '看看有哪些写作工作流可用')
    await ag.waitTurn(api, pid, cid)
    const step = page.getByText(/加载 Skill · /).last()
    await step.waitFor({ timeout: 15000 })
    const stepText = await step.innerText()
    reqs = ag.agentRequests(env, seq)
    const toolMsg = reqs.length >= 2 ? (reqs[1].body.messages || []).filter((m) => m.role === 'tool').map((m) => m.content).pop() || '' : ''
    let parsed = null
    try { parsed = JSON.parse(toolMsg) } catch { /* 非 JSON */ }
    const resultObj = parsed && (parsed.data || parsed.result || parsed)
    const inputs = resultObj?.inputs || []
    await step.click()
    await lib.shot(env, page, 'g7-c8-load-skill')
    r.check('C8', stepText.includes('完成') && inputs.length === 2 && !!resultObj?.instructions && String(resultObj?.body || '').includes('{{ focus }}'),
      `时间线「${stepText}」；第二次模型请求的工具结果含 inputs ${inputs.map((i) => `${i.name}${i.required ? '(必填)' : ''}${i.default ? `=默认${i.default}` : ''}`).join('、')}；`
      + `instructions「${String(resultObj?.instructions || '').slice(0, 30)}…」；body 未填充：${String(resultObj?.body || '').slice(0, 40)}`)

    // ---------- C9：start_task 传输入值（先缺必填，再带齐） ----------
    const chId = env.fixtures.main_project.chapters['2']
    await lib.clearScripts(env)
    await lib.pushScript(env, [
      { match: 'tools', type: 'tool_calls', tool_calls: [{ name: 'start_task', arguments: { kind: 'chapter.polish', params: { chapter_id: chId, skill_id: skill.id, skill_inputs: [{ name: 'tone', value: '沉重' }] } } }] },
      { match: 'tools', type: 'tool_calls', tool_calls: [{ name: 'start_task', arguments: { kind: 'chapter.polish', params: { chapter_id: chId, skill_id: skill.id, skill_inputs: [{ name: 'tone', value: '沉重' }, { name: 'focus', value: '老周' }] } } }] },
    ])
    seq = (await lib.modelState(env)).seq
    const tasksBefore = (await api.get(`/tasks?project_id=${pid}&kind=chapter.polish&limit=50`))
    const nBefore = (tasksBefore.items || tasksBefore).length
    await ag.typeAndSend(page, '用基调 Skill 润色第 2 章')
    const paused = await ag.waitTurn(api, pid, cid, { until: 'paused', within: 60000 }).catch((e) => ({ error: e.message }))
    await page.waitForTimeout(1000)
    const steps = await page.getByText(/启动后台任务 · /).allInnerTexts()
    const firstResult = (() => {
      const rq = ag.agentRequests(env, seq)
      return rq.length >= 2 ? (rq[1].body.messages || []).filter((m) => m.role === 'tool').map((m) => m.content).pop() || '' : ''
    })()
    const midTasks = (await api.get(`/tasks?project_id=${pid}&kind=chapter.polish&limit=50`))
    const nMid = (midTasks.items || midTasks).length
    const proposal = (paused.changes || []).filter((c) => c.tool_name === 'start_task' && c.status === 'proposed')
    const cardText = proposal.length ? await page.locator('.ant-card').filter({ hasText: '待确认' }).last().innerText().catch(() => '') : ''
    await lib.shot(env, page, 'g7-c9-proposal')
    // 确认提案
    let started = null
    let filledOut = false
    if (proposal.length) {
      seq = (await lib.modelState(env)).seq
      await page.locator('.ant-card').filter({ hasText: '待确认' }).last().getByRole('button', { name: /^确\s*认$/ }).click()
      await ag.waitTurn(api, pid, cid, { within: 60000 })
      const after = (await api.get(`/tasks?project_id=${pid}&kind=chapter.polish&limit=50`))
      const list = after.items || after
      started = list.length > nMid ? list[0] : null
      if (started) {
        const done = await api.waitTask(started, 120000)
        started = done
        const { readFileSync } = require('fs')
        const rows = readFileSync(`${env.dir}/requests.jsonl`, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l))
          .filter((x) => x.seq > seq && x.kind === 'text')
        filledOut = rows.some((x) => msgText(x).includes('请以沉重的基调写作，并重点刻画老周的言行与心理。'))
      }
    }
    await page.waitForTimeout(800)
    await lib.shot(env, page, 'g7-c9-confirmed')
    r.check('C9', /error|必填/.test(firstResult) && nMid === nBefore && proposal.length === 1 && started && started.status === 'succeeded' && filledOut,
      `第一次 start_task 工具结果：${firstResult.slice(0, 120)}；此时 chapter.polish 任务数 ${nBefore}→${nMid}；时间线步骤 ${JSON.stringify(steps)}；`
      + `待确认提案 ${proposal.length} 个（卡片：${cardText.replace(/\s+/g, ' ').slice(0, 160)}）；确认后任务 ${started ? started.status : '未创建'}；出站含填充正文（沉重/老周）${filledOut}`)

    const errs = page._qaConsole.filter((x) => !x.includes('warning'))
    r.note('console', errs.length ? errs.slice(0, 5).join(' | ') : '无控制台错误')
  } finally {
    await browser.close()
  }
  process.exit(r.save(env) ? 1 : 0)
}

main().catch((err) => { console.error(err); process.exit(2) })
