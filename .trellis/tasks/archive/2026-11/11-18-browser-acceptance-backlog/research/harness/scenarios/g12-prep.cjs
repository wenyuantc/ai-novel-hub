// G1/G2 前置：作者经接口改写第 8、10 章（构造「引句不以设定名开头」的玉佩证据），再经界面发起一次全书一致性检查。
// 运行：QA_DIR=<dir> node scenarios/g12-prep.cjs   （只需在 seed.py 之后执行一次）
'use strict'
const lib = require('../lib.cjs')
const { PID_OF, CH, patchChapter, waitMessage } = require('./g12-common.cjs')

const CH8 = '官道上人来人往。据说那块玉佩被林远失手摔碎了。茶棚老板摇头叹息。'
const CH10 = '雨夜投宿客栈。夜里林远取出玉佩，对着灯火端详。窗外雨声渐歇。'

async function main() {
  const env = lib.loadEnv()
  const r = lib.reporter('g12-prep')
  const pid = PID_OF(env)
  const browser = await lib.launch()
  try {
    const author = await lib.contextAs(browser, env, 'qa_author', 'desktop')
    await patchChapter(author.api, pid, CH(env, 8), CH8)
    await patchChapter(author.api, pid, CH(env, 10), CH10)
    r.note('prep-chapters', '第 8 章改为玉佩被摔碎、第 10 章改为取出玉佩（接口）')

    const page = author.page
    await page.goto(`/projects/${pid}/consistency`)
    await page.getByRole('button', { name: '发起检查' }).click()
    const modal = page.getByRole('dialog', { name: '发起一致性检查' })
    await modal.getByText('将调用模型抽取事实').waitFor({ timeout: 15000 })
    await modal.getByRole('button', { name: '开始检查' }).click()
    const done = await waitMessage(page, '一致性检查已完成', 120000)
    const issues = await author.api.get(`/projects/${pid}/consistency/issues`)
    const titles = issues.items.map((i) => i.title)
    r.check('prep-check', done && titles.some((t) => t.includes('佩剑')) && titles.some((t) => t.includes('玉佩')),
      `界面全书检查完成提示：${done}；问题：${titles.join('、')}`)
    await lib.shot(env, page, 'g12-prep-check-done')
  } finally {
    await browser.close()
  }
  process.exit(r.save(env) ? 1 : 0)
}

main().catch((err) => { console.error(err); process.exit(2) })
