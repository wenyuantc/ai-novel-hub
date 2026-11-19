// 项目智能体场景的共用操作（G4、G7 C7–C9）。
'use strict'
const fs = require('fs')
const path = require('path')
const lib = require('../lib.cjs')

// 输入框被 AntD AutoComplete 包裹，暴露为 combobox（见 verification-g7-g4.md 的 D-extra）
const transient404 = []
const BOX = 'textarea[aria-label="发给项目智能体的消息"]'

/** 打开作品内项目智能体面板并新建会话；返回会话 id（从接口最新会话取）。 */
async function openNewConversation(page, api, pid) {
  // 用页头「项目智能体」按钮打开（写入本地偏好，跨页保持）；?agent=open 只对当前地址生效
  await page.goto(`/projects/${pid}/overview`)
  const toggle = page.getByRole('button', { name: '项目智能体' })
  await toggle.waitFor({ timeout: 15000 })
  if ((await toggle.getAttribute('aria-expanded')) !== 'true') await toggle.click()
  const start = page.getByRole('button', { name: '开始新会话' })
  const plus = page.getByRole('button', { name: '新建会话' })
  await Promise.race([start.waitFor({ timeout: 15000 }), plus.waitFor({ timeout: 15000 })]).catch(() => {})
  const before = new Set([].concat((x => x.items || x)(await api.get(`/projects/${pid}/agent/conversations`))).map((c) => c.id))
  if (await start.isVisible().catch(() => false)) await start.click(); else await plus.click()
  await page.locator(BOX).waitFor({ timeout: 15000 })
  let cid = null
  for (let i = 0; i < 20 && !cid; i += 1) {
    const list = await api.get(`/projects/${pid}/agent/conversations`)
    cid = (list.items || list).map((c) => c.id).find((id) => !before.has(id)) ?? null
    if (!cid) await lib.sleep(250)
  }
  return cid
}

async function typeAndSend(page, text) {
  const box = page.locator(BOX)
  await box.fill(text)
  await page.getByRole('button', { name: /发\s*送/ }).click()
}

/** 等本会话没有进行中的轮次（或轮次进入待确认）。 */
async function waitTurn(api, pid, cid, { until = 'idle', within = 60000 } = {}) {
  const deadline = Date.now() + within
  for (;;) {
    // 编辑者（协作者）读会话详情时，轮次状态切换的瞬间可能短暂返回 404「智能体轮次不存在」；计数后重试
    const d = await api.raw('GET', `/projects/${pid}/agent/conversations/${cid}`, undefined, [200, 404])
    if (!d.messages) {
      transient404.push({ cid, at: Date.now(), detail: d.detail })
      if (Date.now() > deadline) throw new Error(`会话 ${cid} 持续 404：${d.detail}`)
      await lib.sleep(200)
      continue
    }
    const t = d.active_task
    if (until === 'idle' && !t) return d
    if (until === 'paused' && t && t.status === 'paused') return d
    if (Date.now() > deadline) throw new Error(`会话 ${cid} 等待 ${until} 超时：${JSON.stringify(t)}`)
    await lib.sleep(400)
  }
}

function agentRequests(env, sinceSeq) {
  const file = path.join(env.dir, 'requests.jsonl')
  return fs.readFileSync(file, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l))
    .filter((r) => r.seq > sinceSeq && r.kind === 'agent')
}

module.exports = { transient404, BOX, openNewConversation, typeAndSend, waitTurn, agentRequests }
