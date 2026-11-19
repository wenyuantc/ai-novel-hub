// G3 C-4 补充：系列设定协作者编辑共享基础后，各书各自的剧情变化（setting progressions）不变，
// 按章节回溯的有效设定叠加在新基础上。
// 运行：QA_DIR=<dir> node scenarios/g3-c4-progressions.cjs（需刚跑完 seed.py 的环境；会让续篇也采用「沈青」）
// 变化记录经接口创建：POST /projects/{pid}/setting-timelines（建时间线）与
// POST /projects/{pid}/setting-timelines/{tid}/progressions（即角色抽屉「字段与剧情变化」页签所用接口）。
'use strict'
const lib = require('../lib.cjs')

const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
const B = (t) => ({ name: new RegExp('^(?:[a-z-]+\\s+)?\\s*' + [...t].map(esc).join('\\s*') + '\\s*$') })
const pick = (e) => ({ id: e.id, field_key: e.field_key, operation: e.operation, value: e.value, anchor_kind: e.anchor_kind,
  chapter_id: e.chapter_id, chapter_no: e.chapter_no, boundary: e.boundary, revision: e.revision, active: e.active, source: e.source, note: e.note })

async function main() {
  const env = lib.loadEnv()
  const fx = env.fixtures
  const sid = fx.series.id
  const eid = fx.series.entry_adopted.id
  const r = lib.reporter('g3-c4-progressions')
  const browser = await lib.launch()
  try {
    const author = await lib.contextAs(browser, env, 'qa_author')
    const A = author.api
    const sRev = async () => (await A.get(`/series/${sid}`)).revision
    // 续篇也采用沈青
    const entry = await A.get(`/series/${sid}/entries/${eid}`)
    if (!entry.uses.some((u) => u.project_id === fx.book_b.id)) {
      await A.post(`/series/${sid}/entries/${eid}/uses`, { project_id: fx.book_b.id, expected_revision: await sRev() })
    }
    const uses = (await A.get(`/series/${sid}/entries/${eid}`)).uses
    const oldBase = (await A.get(`/series/${sid}/entries/${eid}`)).data.profile
    const books = [
      { pid: fx.main_project.id, title: '剑影长歌', ch: fx.main_project.chapters },
      { pid: fx.book_b.id, title: '剑影续篇', ch: null },
    ]
    for (const b of books) {
      b.local = uses.find((u) => u.project_id === b.pid).ref.id
      if (!b.ch) {
        const list = await A.get(`/projects/${b.pid}/chapters?page_size=50`)
        const items = list.items || list
        b.ch = Object.fromEntries(items.map((c) => [String(c.chapter_no), c.id]))
      }
      const path = `/projects/${b.pid}/setting-timelines`
      let tl = (await A.get(`${path}?subject_kind=character&character_id=${b.local}`)).items[0]
      if (!tl) tl = await A.post(path, { subject_kind: 'character', character_id: b.local })
      b.tl = tl.id
      b.path = `${path}/${tl.id}`
      const existing = (await A.get(`${b.path}/progressions`)).items
      if (!existing.length) {
        const add = async (body) => {
          const rev = (await A.get(`${b.path}/progressions`)).timeline_revision
          return A.post(`${b.path}/progressions`, { expected_revision: rev, ...body })
        }
        if (b.pid === fx.main_project.id) {
          await add({ field_key: 'profile', operation: 'append', value: '第5章山谷一战后左臂负伤，暂不能用剑。', anchor_kind: 'chapter', chapter_id: b.ch['5'], boundary: 'end', note: '验收：主作品本书变化' })
          await add({ field_key: 'current_state', operation: 'replace', value: '在临江城养伤', anchor_kind: 'chapter', chapter_id: b.ch['5'], boundary: 'end', note: '验收：主作品状态' })
          await add({ field_key: 'current_state', operation: 'replace', value: '伤愈，随林远守城', anchor_kind: 'chapter', chapter_id: b.ch['12'], boundary: 'start', note: '验收：主作品状态 2' })
        } else {
          await add({ field_key: 'profile', operation: 'append', value: '续篇开篇已任临江城守将。', anchor_kind: 'chapter', chapter_id: b.ch['1'], boundary: 'end', note: '验收：续篇本书变化' })
          await add({ field_key: 'current_state', operation: 'replace', value: '镇守临江城', anchor_kind: 'planned', chapter_no: 3, boundary: 'start', note: '验收：续篇计划章节' })
        }
      }
    }
    // 记录前态
    const points = { main: [['4', 'start'], ['5', 'end'], ['6', 'start'], ['12', 'start'], ['15', 'end']], b: [['1', 'start'], ['1', 'end']] }
    const snapshot = async (b, pts) => {
      const ev = (await A.get(`${b.path}/progressions`)).items.map(pick)
      const eff = {}
      for (const [no, bd] of pts) {
        const e = await A.get(`${b.path}/effective?chapter_id=${b.ch[no]}&boundary=${bd}`)
        eff[`${no}:${bd}`] = { profile: e.fields.profile, current_state: e.fields.current_state, applied: e.applied_event_ids }
      }
      if (b.pid === fx.book_b.id) {
        const e = await A.get(`${b.path}/effective?chapter_no=3&boundary=start`)
        eff['planned3:start'] = { profile: e.fields.profile, current_state: e.fields.current_state, applied: e.applied_event_ids }
      }
      return { ev, eff }
    }
    const before = { main: await snapshot(books[0], points.main), b: await snapshot(books[1], points.b) }
    r.note('before', JSON.stringify(before))

    // qa_series 在界面编辑共享基础
    const ser = await lib.contextAs(browser, env, 'qa_series')
    const sp = ser.page
    const newBase = `林远的同门师姐，剑法以快见长（设定协作者 ${new Date().toISOString().slice(11, 19)} 修订）`
    await sp.goto(`/series/${sid}`)
    await sp.getByText('系列共享设定').waitFor({ timeout: 15000 })
    await sp.locator('.ant-list-item').filter({ hasText: '沈青' }).getByRole('button', B('编辑共享基础')).click()
    const m = sp.locator('.ant-modal').filter({ hasText: '编辑共享基础 · 沈青' })
    await m.waitFor()
    await m.getByRole('textbox', { name: '简介' }).fill(newBase)
    await m.getByRole('button', B('保存共享基础')).click()
    const saved = await sp.getByText('共享基础已保存').first().waitFor({ timeout: 10000 }).then(() => true, () => false)
    await lib.shot(env, sp, 'g3-c4p-series-edit')

    const after = { main: await snapshot(books[0], points.main), b: await snapshot(books[1], points.b) }
    r.note('after', JSON.stringify(after))

    // 核对
    for (const [key, b] of [['main', books[0]], ['b', books[1]]]) {
      const evSame = JSON.stringify(before[key].ev) === JSON.stringify(after[key].ev)
      const problems = []
      for (const [pt, v0] of Object.entries(before[key].eff)) {
        const v1 = after[key].eff[pt]
        const expProfile = typeof v0.profile === 'string' ? v0.profile.split(oldBase).join(newBase) : v0.profile
        if (!(typeof v0.profile === 'string' && v0.profile.includes(oldBase))) problems.push(`${pt} 前态未含旧基础`)
        if (v1.profile !== expProfile) problems.push(`${pt} profile「${v1.profile}」≠ 期望「${expProfile}」`)
        if (v1.current_state !== v0.current_state) problems.push(`${pt} current_state 由「${v0.current_state}」变为「${v1.current_state}」`)
        if (JSON.stringify(v1.applied) !== JSON.stringify(v0.applied)) problems.push(`${pt} 生效事件 ${JSON.stringify(v0.applied)}→${JSON.stringify(v1.applied)}`)
      }
      const summary = Object.entries(after[key].eff).map(([pt, v]) => `${pt}: profile「${v.profile}」/ 状态「${v.current_state}」/ 事件 ${JSON.stringify(v.applied)}`).join('；')
      r.check(`C-4 ${b.title}`, saved && evSame && problems.length === 0,
        `变化记录 ${before[key].ev.length}→${after[key].ev.length} 条，逐字段一致 ${evSame}；回溯：${summary}${problems.length ? `；问题：${problems.join('；')}` : ''}`)
    }

    // 界面：作者在两本书角色抽屉「字段与剧情变化」页签查看
    for (const b of books) {
      const p = author.page
      await p.goto(`/projects/${b.pid}/characters`)
      await p.getByText('沈青').first().waitFor({ timeout: 15000 })
      await p.getByText('沈青', { exact: true }).first().click()
      const tab = p.getByRole('tab', { name: '字段与剧情变化' })
      const ok = await tab.waitFor({ timeout: 10000 }).then(() => true, () => false)
      let text = ''
      if (ok) {
        await tab.click()
        await p.waitForTimeout(2000)
        text = (await p.locator('.ant-drawer-body').innerText()).replace(/\s+/g, ' ')
      }
      await lib.shot(env, p, `g3-c4p-ui-${b.pid}`)
      const expect = b.pid === fx.main_project.id ? ['第5章山谷一战后左臂负伤', '在临江城养伤', '伤愈，随林远守城'] : ['续篇开篇已任临江城守将', '镇守临江城']
      const seen = expect.filter((t) => text.includes(t))
      r.check(`C-4 界面 ${b.title}`, seen.length === expect.length,
        `「字段与剧情变化」页签可见本书变化 ${seen.length}/${expect.length}；含新基础 ${text.includes(newBase)}；片段「${text.slice(0, 160)}」`)
    }
  } finally {
    await browser.close()
  }
  process.exit(r.save(env) ? 1 : 0)
}

main().catch((err) => { console.error(err); process.exit(2) })
