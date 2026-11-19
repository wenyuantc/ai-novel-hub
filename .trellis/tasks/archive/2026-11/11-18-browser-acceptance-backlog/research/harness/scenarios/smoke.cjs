// 冒烟（AC5）：登录、建作品、生成、分析、一致性检查、修改影响评估、智能体工具调用、协作者打开共享作品。
// 用独立的「冒烟作品」，不改 seed.py 建立的验收夹具。
// 运行：QA_DIR=<dir> node scenarios/smoke.cjs
'use strict'
const lib = require('../lib.cjs')

const FILLER = '林远沿着官道继续赶路，天色平常，一路无事。'

async function main() {
  const env = lib.loadEnv()
  if (!env.fixtures) throw new Error('先运行 seed.py')
  const r = lib.reporter('smoke')
  const browser = await lib.launch()
  try {
    // 1. 登录页登录（作者，桌面视口）
    const author = await lib.loginViaPage(browser, env, 'qa_author', 'desktop')
    r.check('login', /\/projects/.test(author.page.url()), `登录后到达 ${new URL(author.page.url()).pathname}`)
    await lib.shot(env, author.page, 'smoke-01-login')
    const api = author.api

    // 2. 建作品与章节
    const project = await api.post('/projects', { title: `冒烟作品-${Date.now()}`, genre: '武侠' })
    const pid = project.id
    const chapters = {}
    for (let no = 1; no <= 13; no += 1) {
      let content = `第${no}章。${FILLER}`
      if (no === 5) content = '山谷一战，林远力竭。林远的佩剑在山谷中被巨石砸断，彻底毁了。他只得赤手下山。'
      if (no === 12) content = '敌骑逼近城门。林远拔出佩剑迎敌，剑光如雪。守军士气大振。'
      if (no === 13) content = ''
      chapters[no] = (await api.post(`/projects/${pid}/chapters`, { title: `第${no}回`, chapter_no: no, content })).id
    }
    await api.post(`/projects/${pid}/characters`, { name: '林远', role_type: '主角' })
    r.check('create-project', !!pid, `作品 ${pid}，13 章`)

    // 3. 生成一章（空的第 13 章）
    const gen = await api.post(`/chapters/${chapters[13]}/generate`, { target_words: 300 })
    const genDone = await api.waitTask(gen)
    const ch13 = await api.get(`/projects/${pid}/chapters/${chapters[13]}`)
    r.check('generate', genDone.status === 'succeeded' && ch13.content.length > 50,
      `任务 ${genDone.status}，第 13 章 ${ch13.content.length} 字`)

    // 4. 分析第 5 章
    const an = await api.post(`/chapters/${chapters[5]}/analyze`, {})
    const anDone = await api.waitTask(an)
    const analyses = await api.get(`/chapters/${chapters[5]}/analyses`)
    const list = analyses.items || analyses
    const analysis = list.length ? await api.get(`/analyses/${list[0].id}`) : null
    r.check('analyze', anDone.status === 'succeeded' && analysis && analysis.summary,
      `任务 ${anDone.status}；摘要「${analysis?.summary?.slice(0, 20)}…」；回写 ${analysis?.changes?.length ?? 0} 条`)

    // 5. 一致性检查（全书），期望出现佩剑冲突
    const check = await api.post(`/projects/${pid}/consistency/checks`, { mode: 'all' })
    const checkDone = await api.waitTask(check, 180000)
    const issues = await api.get(`/projects/${pid}/consistency/issues`)
    const issueList = issues.items || issues
    const sword = issueList.find((i) => (i.title || '').includes('佩剑'))
    r.check('consistency-check', checkDone.status === 'succeeded' && !!sword,
      `任务 ${checkDone.status}；问题 ${issueList.length} 条；${sword ? `「${sword.title}」` : '无佩剑问题'}`)

    // 6. 改第 5 章后评估修改影响
    const before = await api.get(`/projects/${pid}/chapters/${chapters[5]}`)
    await api.patch(`/projects/${pid}/chapters/${chapters[5]}`, {
      content: '山谷一战，林远力竭。林远的佩剑完好无损，他把佩剑收好。他只得赤手下山。',
      expected_content_revision: before.content_revision,
    })
    const assess = await api.post(`/projects/${pid}/consistency/impacts/${chapters[5]}/assess`, {})
    const assessDone = await api.waitTask(assess, 120000)
    const impact = await api.get(`/projects/${pid}/consistency/impacts/${chapters[5]}`)
    const result = impact.assessment || {}
    const hit12 = (result.items || []).some((item) => item.chapter_id === chapters[12])
    r.check('impact-assess', assessDone.status === 'succeeded' && (result.changes || []).length > 0 && hit12,
      `任务 ${assessDone.status}；事实变化 ${(result.changes || []).map((c) => `${c.subject}:${c.before}→${c.after}`).join('，')}；`
      + `受影响含第 12 章：${hit12}`)

    // 7. 智能体：压入一次工具调用，界面上应出现该工具步骤
    await lib.clearScripts(env)
    await lib.pushScript(env, { match: 'tools', type: 'tool_calls', tool_calls: [{ name: 'get_project_overview', arguments: {} }] })
    const page = author.page
    await page.goto(`/projects/${pid}/overview`)
    await page.getByRole('button', { name: '项目智能体' }).click()
    await page.getByRole('button', { name: '开始新会话' }).click()
    const box = page.locator('textarea[aria-label="发给项目智能体的消息"]')
    await box.waitFor({ timeout: 15000 })
    await box.fill('请看看这本书现在的情况')
    await page.getByRole('button', { name: '发送' }).click()
    const step = page.getByText('读取项目概览 · 完成')
    let stepOk = false
    try { await step.first().waitFor({ timeout: 30000 }); stepOk = true } catch { /* 记为失败 */ }
    const calls = await lib.modelCalls(env, '?kind=agent')
    await lib.shot(env, page, 'smoke-02-agent-tool-step')
    r.check('agent-tool-step', stepOk, `时间线出现「读取项目概览 · 完成」：${stepOk}；假模型收到智能体请求 ${calls.items.length} 次`)

    // 8. 协作者 qa_editor（窄屏）打开共享作品
    const editor = await lib.contextAs(browser, env, 'qa_editor', 'mobile')
    const mainPid = env.fixtures.main_project.id
    await editor.page.goto(`/projects/${mainPid}`)
    const title = editor.page.getByText(env.fixtures.main_project.title).first()
    let titleOk = false
    try { await title.waitFor({ timeout: 15000 }); titleOk = true } catch { /* 记为失败 */ }
    const noOverflow = await lib.noHorizontalOverflow(editor.page)
    const vp = editor.page.viewportSize()
    await lib.shot(env, editor.page, 'smoke-03-editor-mobile')
    r.check('collaborator-open', titleOk, `qa_editor 打开作品 ${mainPid}，可见标题：${titleOk}`)
    r.check('mobile-viewport', vp.width === 375 && vp.height === 812 && noOverflow,
      `视口 ${vp.width}×${vp.height}，无横向溢出：${noOverflow}`)

    const consoleErrors = [...page._qaConsole, ...editor.page._qaConsole].filter((t) => !t.includes('warning'))
    r.note('console', consoleErrors.length ? consoleErrors.slice(0, 5).join(' | ') : '无控制台错误')
    r.note('smoke-project', `冒烟作品 id ${pid}`)
  } finally {
    await browser.close()
  }
  process.exit(r.save(env) ? 1 : 0)
}

main().catch((err) => { console.error(err); process.exit(2) })
