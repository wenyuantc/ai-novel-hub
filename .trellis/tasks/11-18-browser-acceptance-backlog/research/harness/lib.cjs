// Playwright 公共辅助：读取环境、按身份建立独立上下文并登录、截图、断言与假模型控制。
// 运行：QA_DIR=<serve.py 临时目录> node scenarios/<场景>.cjs
// Playwright 取自 npx 缓存（不改 frontend/package.json），可用 PLAYWRIGHT_PATH 覆盖。
'use strict'
const fs = require('fs')
const path = require('path')

const PLAYWRIGHT_PATH = process.env.PLAYWRIGHT_PATH
  || '/Users/wenyuantc/.npm/_npx/e41f203b7505f1fb/node_modules/playwright'
const { chromium } = require(PLAYWRIGHT_PATH)

// 测试账号口令只在隔离库中有效，与 seed.py 的 QA_PASSWORD 保持一致
const QA_PASSWORD = 'Qa-pass-2026'
const VIEWPORTS = { desktop: { width: 1280, height: 900 }, mobile: { width: 375, height: 812 } }

function qaDir() {
  const dir = process.env.QA_DIR || process.argv[2]
  if (!dir) throw new Error('需要 QA_DIR 环境变量（serve.py 打印的 dir）')
  return path.resolve(dir)
}

function loadEnv() {
  const dir = qaDir()
  const state = JSON.parse(fs.readFileSync(path.join(dir, 'state.json'), 'utf8'))
  const fixturesPath = path.join(dir, 'fixtures.json')
  const fixtures = fs.existsSync(fixturesPath) ? JSON.parse(fs.readFileSync(fixturesPath, 'utf8')) : null
  const shots = path.join(dir, 'screenshots')
  fs.mkdirSync(shots, { recursive: true })
  return { dir, state, fixtures, origin: state.origin, modelControl: `http://127.0.0.1:${state.model_port}/_control`, shots }
}

async function launch(opts = {}) {
  return chromium.launch({ channel: 'chrome', headless: process.env.HEADED ? false : true, ...opts })
}

function credentials(env, username) {
  if (username === 'super') return { username: env.state.admin_username, password: env.state.admin_password }
  return { username, password: QA_PASSWORD }
}

/** 新的独立上下文，经接口登录（cookie 落在该上下文）。viewport: 'desktop' | 'mobile' | {width,height}。 */
async function contextAs(browser, env, username, viewport = 'desktop') {
  const vp = typeof viewport === 'string' ? VIEWPORTS[viewport] : viewport
  const context = await browser.newContext({ viewport: vp, baseURL: env.origin, locale: 'zh-CN' })
  const res = await context.request.post('/api/v1/auth/login', { data: credentials(env, username) })
  if (!res.ok()) throw new Error(`登录 ${username} 失败：${res.status()} ${await res.text()}`)
  const page = await context.newPage()
  attachConsole(page, username)
  return { context, page, api: apiFor(context.request, username) }
}

/** 新的独立上下文，从登录页登录（验证真实登录流程）。 */
async function loginViaPage(browser, env, username, viewport = 'desktop') {
  const vp = typeof viewport === 'string' ? VIEWPORTS[viewport] : viewport
  const context = await browser.newContext({ viewport: vp, baseURL: env.origin, locale: 'zh-CN' })
  const page = await context.newPage()
  attachConsole(page, username)
  const cred = credentials(env, username)
  await page.goto('/login')
  await page.getByPlaceholder('请输入用户名').fill(cred.username)
  await page.getByPlaceholder('请输入密码').fill(cred.password)
  await page.getByRole('button', { name: '登录知页' }).click()
  await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 15000 })
  return { context, page, api: apiFor(context.request, username) }
}

function attachConsole(page, label) {
  page._qaConsole = []
  page.on('console', (msg) => {
    if (['error', 'warning'].includes(msg.type())) page._qaConsole.push(`[${label}] ${msg.type()}: ${msg.text()}`)
  })
  page.on('pageerror', (err) => page._qaConsole.push(`[${label}] pageerror: ${err.message}`))
}

/** 共享浏览器上下文 cookie 的接口调用器。 */
function apiFor(request, label) {
  async function call(method, url, data, ok = [200, 201, 202, 204]) {
    const res = await request.fetch(`/api/v1${url}`, { method, data })
    const text = await res.text()
    if (!ok.includes(res.status())) throw new Error(`[${label}] ${method} ${url} -> ${res.status()}: ${text.slice(0, 400)}`)
    return text ? JSON.parse(text) : null
  }
  return {
    get: (url) => call('GET', url),
    post: (url, data) => call('POST', url, data ?? {}),
    patch: (url, data) => call('PATCH', url, data),
    put: (url, data) => call('PUT', url, data),
    del: (url) => call('DELETE', url),
    raw: call,
    async waitTask(task, within = 120000) {
      const deadline = Date.now() + within
      for (;;) {
        const cur = await call('GET', `/tasks/${task.id}?task_key=${encodeURIComponent(task.task_key)}`)
        if (['succeeded', 'failed', 'cancelled', 'interrupted'].includes(cur.status)) return cur
        if (Date.now() > deadline) throw new Error(`任务 ${task.id} 超时：${JSON.stringify(cur)}`)
        await sleep(500)
      }
    },
  }
}

async function shot(env, page, name) {
  const file = path.join(env.shots, `${name}.png`)
  await page.screenshot({ path: file, fullPage: false })
  return file
}

/** 窄屏检查：document 无横向溢出。 */
async function noHorizontalOverflow(page) {
  return page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)
}

// ---- 假模型控制 ----
async function model(env, method, sub, body) {
  const res = await fetch(`${env.modelControl}/${sub}`, {
    method, headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body),
  })
  return res.json()
}
const pushScript = (env, scripts) => model(env, 'POST', 'script', scripts)
const clearScripts = (env) => model(env, 'DELETE', 'script')
const setDelay = (env, delay) => model(env, 'POST', 'delay', delay)
const modelCalls = (env, query = '') => model(env, 'GET', `calls${query}`)
const modelState = (env) => model(env, 'GET', 'state')

// ---- 结果记录 ----
function reporter(name) {
  const results = []
  return {
    results,
    check(id, ok, detail) {
      results.push({ id, ok: !!ok, detail })
      console.log(`${ok ? 'PASS' : 'FAIL'} ${id} — ${detail}`)
      return !!ok
    },
    note(id, detail) {
      results.push({ id, ok: null, detail })
      console.log(`NOTE ${id} — ${detail}`)
    },
    save(env) {
      const file = path.join(env.dir, `result-${name}.json`)
      fs.writeFileSync(file, JSON.stringify(results, null, 2))
      const failed = results.filter((r) => r.ok === false).length
      console.log(`\n${name}: ${results.filter((r) => r.ok).length} 通过，${failed} 失败；结果 ${file}`)
      return failed
    },
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

module.exports = {
  QA_PASSWORD, VIEWPORTS, loadEnv, launch, contextAs, loginViaPage, apiFor, shot, noHorizontalOverflow,
  pushScript, clearScripts, setDelay, modelCalls, modelState, reporter, sleep,
}
