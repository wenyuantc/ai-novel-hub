// G1/G2（一致性检查、修改影响）共用的小工具：改章、读取高亮、等待文本。
'use strict'

const PID_OF = (env) => env.fixtures.main_project.id
const CH = (env, no) => env.fixtures.main_project.chapters[String(no)]

/** 经接口改写整章正文（作者身份），返回新章节。 */
async function patchChapter(api, pid, cid, content) {
  const before = await api.get(`/projects/${pid}/chapters/${cid}`)
  return api.patch(`/projects/${pid}/chapters/${cid}`, { content, expected_content_revision: before.content_revision })
}

/** 页面上所有 [data-mention-focus] 的文本按文档顺序拼接（即实际被高亮的文字）。 */
async function focusedText(page) {
  return page.evaluate(() => Array.from(document.querySelectorAll('[data-mention-focus]')).map((n) => n.textContent).join(''))
}

async function waitText(page, text, timeout = 15000) {
  const loc = page.getByText(text, { exact: false }).first()
  try { await loc.waitFor({ state: 'visible', timeout }); return true } catch { return false }
}

async function visible(locator, timeout = 3000) {
  try { await locator.first().waitFor({ state: 'visible', timeout }); return true } catch { return false }
}

/** antd message 文本（最近出现的）。 */
async function waitMessage(page, text, timeout = 15000) {
  const loc = page.locator('.ant-message').getByText(text, { exact: false }).first()
  try { await loc.waitFor({ state: 'visible', timeout }); return true } catch { return false }
}

module.exports = { PID_OF, CH, patchChapter, focusedText, waitText, visible, waitMessage }
