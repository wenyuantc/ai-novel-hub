import { App, Button, Segmented } from 'antd'
import { createElement, isValidElement, type DependencyList, type EffectCallback, type ReactElement, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Chapter, Scene, Task } from '../../api/types'
import ChapterEditorPage from './ChapterEditorPage'

// 仅调度页面与真实 useTaskStream 的 hook；子组件交给真实 SSR。网络边界使用可控 POST / SSE。
const hooks = vi.hoisted(() => ({
  active: false, index: 0,
  slots: [] as { value: unknown; deps?: DependencyList; cleanup?: () => void }[],
  effects: [] as (() => void)[],
}))
const view = vi.hoisted(() => ({
  chapter: null as Chapter | null, scene: null as Scene | null,
  draft: '尚未替换的场景原稿', scope: 1, cid: '3', loaders: 0, autoAnalyze: false, sceneIds: [22],
  controller: null as Record<string, unknown> | null,
  scopes: new Map<number, () => boolean>(),
  track: vi.fn(), loadScene: vi.fn(async () => true), changeDocument: vi.fn(),
  refresh: vi.fn(async () => {}), changed: vi.fn(),
  message: { info: vi.fn(), warning: vi.fn(), error: vi.fn(), success: vi.fn() },
}))
vi.mock('react', async (original) => {
  const actual = await original<typeof import('react')>()
  const changed = (old: DependencyList | undefined, next: DependencyList | undefined) =>
    !old || !next || old.length !== next.length || old.some((value, index) => value !== next[index])
  const memo = <T,>(factory: () => T, deps: DependencyList): T => {
    const index = hooks.index++
    if (!hooks.slots[index] || changed(hooks.slots[index].deps, deps)) hooks.slots[index] = { value: factory(), deps }
    return hooks.slots[index].value as T
  }
  return { ...actual,
    useState: <T,>(initial: T | (() => T)) => {
      if (!hooks.active) return actual.useState(initial)
      const index = hooks.index++
      if (!hooks.slots[index]) hooks.slots[index] = { value: typeof initial === 'function' ? (initial as () => T)() : initial }
      return [hooks.slots[index].value, (next: T | ((old: T) => T)) => {
        hooks.slots[index].value = typeof next === 'function' ? (next as (old: T) => T)(hooks.slots[index].value as T) : next
      }]
    },
    useRef: <T,>(initial: T) => hooks.active ? memo(() => ({ current: initial }), []) : actual.useRef(initial),
    useMemo: <T,>(factory: () => T, deps: DependencyList) => hooks.active ? memo(factory, deps) : actual.useMemo(factory, deps),
    useCallback: <T,>(callback: T, deps: DependencyList) => hooks.active ? memo(() => callback, deps) : actual.useCallback(callback as never, deps),
    useEffect: (setup: EffectCallback, deps?: DependencyList) => {
      if (!hooks.active) return actual.useEffect(setup, deps)
      const index = hooks.index++
      const old = hooks.slots[index]
      if (old && !changed(old.deps, deps)) return
      hooks.effects.push(() => {
        old?.cleanup?.()
        const cleanup = setup()
        hooks.slots[index] = { value: null, deps, cleanup: typeof cleanup === 'function' ? cleanup : undefined }
      })
    },
  }
})
vi.mock('antd', async (original) => {
  const actual = await original<typeof import('antd')>()
  const Provider = (props: Parameters<typeof actual.App>[0]) => createElement(actual.App, props)
  return { ...actual, App: Object.assign(Provider, { useApp: () => ({ message: view.message, modal: {} }) }),
    theme: { ...actual.theme, useToken: () => ({ token: actual.theme.getDesignToken() }) } }
})
vi.mock('../../hooks/useProjectId', () => ({ useProjectId: () => 1 }))
vi.mock('../../hooks/useProjectAccess', () => ({ useProjectAccess: () => {
  const scope = view.scope
  if (!view.scopes.has(scope)) view.scopes.set(scope, () => scope === view.scope)
  return { canUseAI: true, canEdit: true, managingOther: false,
    canEditNow: view.scopes.get(scope), canReadNow: view.scopes.get(scope) }
} }))
vi.mock('../../hooks/useLoader', () => ({ useLoader: () => ({ data: view.loaders++ % 2 === 0 ? view.chapter : [view.chapter], reload: view.changed }) }))
vi.mock('../../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
vi.mock('../../hooks/useElementWidth', () => ({ useElementWidth: () => [{ current: null }, 1000] }))
vi.mock('../../hooks/useHasAIConfig', () => ({ useHasAIConfig: () => true }))
vi.mock('../../hooks/useAgentPageContext', () => ({ usePublishAgentContext: () => {} }))
vi.mock('../../hooks/useProjectRefresh', () => ({ useProjectRefresh: () => {} }))
vi.mock('../../hooks/useSkillChoice', () => ({ useSkillChoice: () => ({ payload: {} }) }))
vi.mock('../../utils/skillInputs', () => ({ skillChoiceProblemText: () => '' }))
vi.mock('../../hooks/useChapterAnalysis', () => ({ useChapterAnalysis: () => ({ state: null, latest: null, status: 'none', running: false, refresh: view.refresh }) }))
// 页头的「派生内容可能过期」提示是独立的只读请求，不属于本文件验证的生成网络边界
vi.mock('../../hooks/useChapterImpactHint', () => ({ useChapterImpactHint: () => null }))
vi.mock('../../components/editorDrafts/useChapterAutosave', async () => ({
  useChapterAutosave: (await import('./ChapterEditorPage.testSupport')).autosavePageFixture,
}))
vi.mock('../../stores/project', () => ({ useProjectStore: Object.assign(
  (select: (state: unknown) => unknown) => select({ project: { id: 1, auto_analyze: view.autoAnalyze, default_style: null }, refresh: view.refresh }),
  { subscribe: () => () => {} },
) }))
vi.mock('../../stores/tasks', () => ({ useTasksStore: (select: (state: unknown) => unknown) => select({ active: [], track: view.track }) }))
vi.mock('../../stores/auth', () => ({ useAuthStore: (select: (state: unknown) => unknown) => select({ status: 'authenticated', user: { id: 1 } }) }))
vi.mock('react-router-dom', () => ({ useParams: () => ({ cid: view.cid }), useNavigate: () => () => {}, useLocation: () => ({ state: null }), useSearchParams: () => [new URLSearchParams(), () => {}], useBlocker: () => ({ state: 'unblocked' }) }))
vi.mock('../../components/systemSettings/useSettingsConfirmation', () => ({ useSettingsConfirmation: () => (options: { onOk: () => void }) => options.onOk() }))
vi.mock('../../components/scenes/useSceneEditor', () => ({ useSceneEditor: () => ({
  scene: view.scene, draft: { title: '场景', content: view.draft, summary: '' }, dirty: false, saving: false, conflict: null, externalChange: false,
  page: { items: view.sceneIds.map((id) => ({ id })), total: view.sceneIds.length }, controller: view.controller,
}) }))
vi.mock('../../components/scenes/SceneWritingPanel', () => ({ default: ({ state }: { state: { draft: { content: string } } }) => createElement('textarea', { 'aria-label': '当前场景正文', value: state.draft.content, readOnly: true }) }))
vi.mock('../../components/chapter/AnalysisDrawer', () => ({ default: () => null }))
vi.mock('../../components/chapter/VersionDrawer', () => ({ default: () => null }))
vi.mock('../../components/notes/NoteSidePanel', () => ({ default: () => null }))
vi.mock('../../components/chapter/RewritePanel', () => ({ default: () => null }))
vi.mock('../../components/chapter/CastSelect', () => ({ default: () => null }))
vi.mock('../../components/chapter/ChapterStyleSetting', () => ({ default: () => null }))
vi.mock('../../components/chapter/ContextPreviewPanel', () => ({ default: () => null }))
vi.mock('../../components/foreshadows/ChapterForeshadowButton', () => ({ default: () => null }))
vi.mock('../../components/AIConfigSelect', () => ({ default: () => null }))
vi.mock('../../components/skills/SkillChoiceField', () => ({ default: () => null }))
vi.mock('../../components/styles/StyleChoiceSelect', () => ({ default: () => null }))

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function task(status: Task['status'] = 'running', sceneId: number | null = 22): Task {
  return { id: 10, task_key: '00000000-0000-4000-8000-000000000010', kind: 'chapter.generate', status, title: '场景生成', project_id: 1, chapter_id: 3, scene_id: sceneId, analysis_id: null, progress: 0, stage: '', error: '', retry_of_id: null, character_id: null, outline_id: null, checkpoint: null, step_cursor: 0, queue_position: null, created_at: '', started_at: null, finished_at: null }
}
function network() {
  const created = deferred<Task>()
  const recovery = deferred<Chapter>()
  let controller!: ReadableStreamDefaultController<Uint8Array>
  const body = new ReadableStream<Uint8Array>({ start(value) { controller = value } })
  const send = (event: string, data: unknown) => controller.enqueue(new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`))
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith('/generate')) return Response.json(await created.promise, { status: 202 })
    if (url.includes('/stream?')) return new Response(body, { headers: { 'content-type': 'text/event-stream' } })
    if (url.includes('/cancel?')) { send('error', { task: task('cancelled'), detail: '任务已取消' }); return Response.json(task('cancelled')) }
    if (url === '/api/v1/projects/1/chapters/3' && (!init?.method || init.method === 'GET')) return Response.json(await recovery.promise)
    throw new Error(`意外请求：${url}`)
  })
  vi.stubGlobal('fetch', fetch)
  return { created, recovery, send, fetch }
}
function elements(node: ReactNode): ReactElement<Record<string, unknown>>[] {
  if (Array.isArray(node)) return node.flatMap(elements)
  if (!isValidElement<Record<string, unknown>>(node)) return []
  return [node, ...elements(node.props.children as ReactNode), ...elements(node.props.extra as ReactNode)]
}
function text(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(text).join('')
  return isValidElement<{ children?: ReactNode }>(node) ? text(node.props.children) : ''
}
function page() {
  hooks.active = true; hooks.index = 0; view.loaders = 0
  let tree: ReactElement
  try {
    const loader = ChapterEditorPage() as ReactElement
    const editor = (loader.type as (props: unknown) => ReactElement)(loader.props)
    tree = (editor.type as (props: unknown) => ReactElement)(editor.props)
  } finally { hooks.active = false }
  const html = renderToStaticMarkup(createElement(App, null, tree))
  hooks.effects.splice(0).forEach((effect) => effect())
  return { html, tree,
    click: (label: string) => {
      const button = elements(tree).find((element) => element.type === Button && text(element.props.children as ReactNode).includes(label))
      if (!button) throw new Error(`没有按钮：${label}`)
      expect(button.props.disabled).not.toBe(true)
      ;(button.props.onClick as () => void)()
    },
  }
}
function unmount() { hooks.slots.forEach((slot) => slot.cleanup?.()) }

beforeEach(() => {
  vi.clearAllMocks()
  hooks.slots = []; hooks.effects = []; hooks.index = 0; hooks.active = false
  view.scope = 1; view.cid = '3'; view.scopes.clear(); view.draft = '尚未替换的场景原稿'
  view.autoAnalyze = false; view.sceneIds = [22]
  view.chapter = { id: 3, project_id: 1, outline_id: null, plan_id: null, style_id: null, chapter_no: 1, title: '章', status: 'draft', word_count: 10, content_revision: 1, content: view.draft, updated_at: '' }
  view.scene = { id: 22, chapter_id: 3, title: '场景', sort: 0, content: view.draft, content_revision: 1, planning_revision: 1 } as Scene
  view.controller = { getSnapshot: () => ({ scene: view.scene, page: { total: view.sceneIds.length }, draft: { content: view.draft } }), load: view.loadScene, changeDocument: view.changeDocument, canAcknowledgeLatest: () => false }
  vi.stubGlobal('window', { addEventListener: vi.fn(), removeEventListener: vi.fn() })
})
afterEach(async () => { unmount(); await Promise.resolve(); vi.unstubAllGlobals() })

describe('章节生成的创建阶段和主区实时预览', () => {
  it('单场景默认整章生成在发送前发现新增场景时，提示重新选择范围', async () => {
    view.autoAnalyze = true
    const current = page()
    const net = network()
    view.sceneIds = [22, 23]
    current.click('按现有场景生成整章')
    await vi.waitFor(() => expect(view.message.warning).toHaveBeenCalledWith('场景目录已变化，请重新选择 AI 处理范围。'))
    expect(net.fetch).not.toHaveBeenCalled()
  })

  it.each([
    { autoAnalyze: true, sceneIds: [22], chooseScene: false, whole: true },
    { autoAnalyze: true, sceneIds: [22], chooseScene: true, whole: false },
    { autoAnalyze: true, sceneIds: [22, 23], chooseScene: false, whole: false },
    { autoAnalyze: false, sceneIds: [22], chooseScene: false, whole: false },
  ])('自动分析 $autoAnalyze，场景 $sceneIds，手选当前场景 $chooseScene 时使用正确的生成接口', async ({ autoAnalyze, sceneIds, chooseScene, whole }) => {
    view.autoAnalyze = autoAnalyze
    view.sceneIds = []
    page()
    view.sceneIds = sceneIds
    let current = page()
    if (chooseScene) {
      const scope = elements(current.tree).find((element) => element.type === Segmented && element.props['aria-label'] === 'AI处理范围')
      ;(scope?.props.onChange as (value: string) => void)('scene')
      current = page()
    }
    const net = network()
    current.click(whole ? '按现有场景生成整章' : '重新生成当前场景')
    const url = whole ? '/api/v1/chapters/3/generate' : '/api/v1/projects/1/chapters/3/scenes/22/generate'
    await vi.waitFor(() => expect(net.fetch).toHaveBeenCalledWith(url, expect.objectContaining({ method: 'POST' })))
  })

  it('延迟 POST 明确显示正在创建任务，不显示灰色停止或伪运行状态', async () => {
    const net = network()
    page().click('重新生成当前场景')
    await vi.waitFor(() => expect(net.fetch).toHaveBeenCalledOnce())
    const pending = page().html
    expect(pending).toContain('正在创建任务')
    expect(pending).not.toContain('停止（已输出')
    expect(view.track).not.toHaveBeenCalled()
    net.created.resolve(task())
    await vi.waitFor(() => expect(net.fetch.mock.calls.some(([url]) => url.includes('/stream?'))).toBe(true))
  })

  it('创建失败后恢复 GET 挂起，也立即结束创建状态并保留原稿', async () => {
    const net = network()
    page().click('重新生成当前场景')
    await vi.waitFor(() => expect(net.fetch).toHaveBeenCalledOnce())
    net.created.reject(new Error('连接断开'))
    await vi.waitFor(() => expect(net.fetch.mock.calls.some(([url]) => url === '/api/v1/projects/1/chapters/3')).toBe(true))
    const failed = page().html
    expect(failed).not.toContain('正在创建任务')
    expect(failed).not.toContain('停止（已输出')
    expect(failed).toContain('重新生成当前场景')
    expect(failed).toContain(view.draft)
    expect(view.message.error).toHaveBeenCalledOnce()
    net.recovery.resolve(view.chapter!)
  })

  it('旧授权请求迟到 202 不挂接，也不清除新授权请求的创建状态', async () => {
    const first = network()
    page().click('重新生成当前场景')
    await vi.waitFor(() => expect(first.fetch).toHaveBeenCalledOnce())
    view.scope += 1
    const refreshed = page()
    expect(refreshed.html).not.toContain('停止（已输出')
    const next = network()
    refreshed.click('重新生成当前场景')
    await vi.waitFor(() => expect(next.fetch).toHaveBeenCalledOnce())
    first.created.resolve(task())
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(view.track).not.toHaveBeenCalled()
    expect(page().html).toContain('正在创建任务')
    next.created.resolve(task())
    await vi.waitFor(() => expect(view.track).toHaveBeenCalledOnce())
  })

  it('失败请求的迟到恢复读取不能覆盖后续任务的章节状态', async () => {
    const first = network()
    page().click('重新生成当前场景')
    await vi.waitFor(() => expect(first.fetch).toHaveBeenCalledOnce())
    first.created.reject(new Error('连接断开'))
    await vi.waitFor(() => expect(first.fetch.mock.calls.some(([url]) => url === '/api/v1/projects/1/chapters/3')).toBe(true))
    const next = network()
    page().click('重新生成当前场景')
    next.created.resolve(task())
    await vi.waitFor(() => expect(view.track).toHaveBeenCalledOnce())
    first.recovery.resolve({ ...view.chapter!, word_count: 99 })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(page().html).toContain('整章 10 字')
    expect(page().html).not.toContain('整章 99 字')
  })

  it('离开章节后的迟到 202 不注册、不订阅、不改新页面', async () => {
    const net = network()
    page().click('重新生成当前场景')
    await vi.waitFor(() => expect(net.fetch).toHaveBeenCalledOnce())
    unmount()
    net.created.resolve(task())
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(view.track).not.toHaveBeenCalled()
    expect(net.fetch).toHaveBeenCalledOnce()
  })

  it('202 排队任务收到运行中首快照后立即切换按钮与实际阶段，不等待正文首字', async () => {
    const net = network()
    page().click('重新生成当前场景')
    net.created.resolve({ ...task('queued'), queue_position: 1 })
    await vi.waitFor(() => expect(net.fetch.mock.calls.some(([url]) => url.includes('/stream?task_key='))).toBe(true))
    expect(page().html).toContain('取消排队')

    const running = { ...task('running'), stage: '正在构建场景上下文', progress: 10 }
    net.send('snapshot', { task: running, text: '' })
    await vi.waitFor(() => expect(page().html).toContain('正在构建场景上下文'))
    const started = page().html
    expect(started).toContain('停止生成')
    expect(started).not.toContain('取消排队')
    expect(started).not.toContain('已输出 0 字')
    expect(started).toContain(view.draft)

    net.send('delta', { text: '首段已实时到达主区。' })
    await vi.waitFor(() => expect(page().html).toContain('首段已实时到达主区。'))
    expect(view.changeDocument).not.toHaveBeenCalled()
    expect(view.loadScene).not.toHaveBeenCalled()
    net.send('done', { task: task('succeeded'), result: { chapter: { ...view.chapter, status: 'draft' } } })
    await vi.waitFor(() => expect(view.loadScene).toHaveBeenCalledOnce())
    expect(page().html).not.toContain('aria-label="AI 实时预览"')
  })

  it.each([22, null])('任务范围 %s 的真实 delta 在正文主区逐步可见，原稿始终不变', async (sceneId) => {
    const net = network()
    let current = page()
    if (sceneId === null) {
      const scope = elements(current.tree).find((element) => element.type === Segmented && element.props['aria-label'] === 'AI处理范围')
      ;(scope?.props.onChange as (value: string) => void)('chapter')
      current = page()
    }
    current.click(sceneId === null ? '按现有场景生成整章' : '重新生成当前场景')
    net.created.resolve(task('running', sceneId))
    await vi.waitFor(() => expect(net.fetch.mock.calls.some(([url]) => url.includes('/stream?task_key='))).toBe(true))
    expect(page().html).toContain('等待模型输出')
    net.send('delta', { text: '\n ' })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(page().html).toContain('等待模型输出')
    expect(page().html).not.toContain('已输出 0 字')
    net.send('stage', { stage: '正在构建场景上下文' })
    await vi.waitFor(() => expect(page().html).toContain('正在构建场景上下文'))
    net.send('delta', { text: '第一段实时文字。' })
    await vi.waitFor(() => expect(page().html).toContain('第一段实时文字。'))
    net.send('delta', { text: '第二段接续文字。' })
    await vi.waitFor(() => expect(page().html).toContain('第一段实时文字。第二段接续文字。'))
    const streamed = page()
    const preview = streamed.html.indexOf('aria-label="AI 实时预览"')
    const editor = streamed.html.indexOf('aria-label="当前场景正文"')
    const sidebar = streamed.html.indexOf('AI 配置')
    expect(preview).toBeGreaterThan(-1)
    expect(preview).toBeLessThan(editor)
    expect(editor).toBeLessThan(sidebar)
    expect(streamed.html).toContain(view.draft)
    expect(view.changeDocument).not.toHaveBeenCalled()
    expect(view.loadScene).not.toHaveBeenCalled()
    streamed.click('收起侧栏')
    expect(page().html).toContain('第一段实时文字。第二段接续文字。')
    net.send('done', { task: task('succeeded', sceneId), result: { chapter: { ...view.chapter, status: 'draft', content: '完成后的新正文' } } })
    await vi.waitFor(() => expect(view.loadScene).toHaveBeenCalledOnce())
    expect(page().html).not.toContain('aria-label="AI 实时预览"')
    expect(view.draft).toBe('尚未替换的场景原稿')
  })

  it.each(['cancelled', 'failed'] as const)('%s 结束预览并保留原稿，取消携带真实 task_key', async (status) => {
    const net = network()
    page().click('重新生成当前场景')
    net.created.resolve(task())
    await vi.waitFor(() => expect(net.fetch.mock.calls.some(([url]) => url.includes('/stream?'))).toBe(true))
    net.send('delta', { text: '尚未完成的提案' })
    await vi.waitFor(() => expect(page().html).toContain('尚未完成的提案'))
    page().click('收起侧栏')
    if (status === 'cancelled') page().click('停止')
    else net.send('error', { task: task('failed'), detail: '模型调用失败' })
    await vi.waitFor(() => expect(net.fetch.mock.calls.some(([url]) => url === '/api/v1/projects/1/chapters/3')).toBe(true))
    net.recovery.resolve(view.chapter!)
    const ended = page().html
    expect(ended).not.toContain('aria-label="AI 实时预览"')
    expect(ended).toContain(view.draft)
    expect(view.changeDocument).not.toHaveBeenCalled()
    expect(view.loadScene).not.toHaveBeenCalled()
    if (status === 'cancelled') expect(net.fetch.mock.calls.some(([url]) => url.includes('/cancel?task_key=00000000-0000-4000-8000-000000000010'))).toBe(true)
    else expect(ended).toContain('模型调用失败')
  })
})
