import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Project, ProjectAccess, ReaderTocItem, User } from '../api/types'
import { ApiError } from '../api/client'
import { projectApi } from '../api/projects'
import { readerApi } from '../api/reader'
import { deferred } from '../components/systemSettings/systemSettingsTestFixtures'
import { useAuthStore } from '../stores/auth'
import ReaderPage from './ReaderPage'
import { bookSearchHref } from '../utils/mentions'

const view = vi.hoisted(() => ({
  effects: [] as Array<{ effect: () => void | (() => void); deps: readonly unknown[] | undefined }>,
  layouts: [] as Array<{ effect: () => void | (() => void); deps: readonly unknown[] | undefined }>,
  loads: [] as unknown[],
  loaders: [] as Array<() => Promise<unknown>>,
  params: { pid: '9', cid: '11' as string | undefined },
  search: '',
  navigate: vi.fn(),
}))
vi.mock('react', async (original) => {
  const actual = await original<typeof import('react')>()
  return { ...actual,
    useEffect: (effect: () => void | (() => void), deps?: readonly unknown[]) => {
      view.effects.push({ effect, deps })
      return actual.useEffect(effect, deps)
    },
    useLayoutEffect: (effect: () => void | (() => void), deps?: readonly unknown[]) => {
      view.layouts.push({ effect, deps })
      return actual.useEffect(effect, deps)
    },
  }
})
vi.mock('../stores/auth', async (original) => {
  const actual = await original<typeof import('../stores/auth')>()
  const hook = (selector: (state: ReturnType<typeof actual.useAuthStore.getState>) => unknown) => selector(actual.useAuthStore.getState())
  return { ...actual, useAuthStore: Object.assign(hook, actual.useAuthStore) }
})
vi.mock('react-router-dom', () => ({
  Link: ({ children, to }: { children: import('react').ReactNode; to: string }) => createElement('a', { href: to }, children),
  useParams: () => view.params,
  useNavigate: () => view.navigate,
  useLocation: () => ({ state: null, search: view.search, key: 'reader' }),
}))
vi.mock('../hooks/useLoader', () => ({ useLoader: (load: () => Promise<unknown>) => {
  view.loaders.push(load)
  return { data: view.loads.shift(), loading: false, error: null }
} }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
vi.mock('../theme/themeStore', () => ({ useResolvedTheme: () => 'light' }))
vi.mock('../components/reader/ReaderAnalysisPanel', () => ({ default: () => createElement('div', { 'data-testid': 'analysis' }) }))
vi.mock('../components/reader/RichReaderContent', () => ({ default: () => null }))
vi.mock('../components/mentions/MentionPreview', () => ({ default: () => null }))

const actor = (id: number, admin = false): User => ({ id, username: `用户${id}`, is_admin: admin, is_super_admin: admin,
  effective_permissions: [], authorization_revision: 1, roles: [], created_at: '' })
const viewer: ProjectAccess = {
  role: 'viewer', is_collaborator: true, authorization_version: 'grant-1', allow_owner_ai: false, can_read: true, can_edit_content: false,
  can_export: true, can_use_ai: false, can_manage_collaborators: false, can_recycle: false, can_backup: false,
  can_write_reading_progress: false,
}
const owner: ProjectAccess = { ...viewer, role: 'owner', is_collaborator: false, authorization_version: null, can_edit_content: true,
  can_use_ai: true, can_manage_collaborators: true, can_recycle: true, can_backup: true, can_write_reading_progress: true }
const toc: ReaderTocItem[] = [11, 12].map((id, index) => ({ id, chapter_no: index + 1, title: '', word_count: 4,
  volume_no: null, volume_title: null }))
const project = (access: ProjectAccess): Project => ({
  id: 9, owner: { id: 2, username: '作者' }, access, title: '星河', genre: '', premise: '', status: 'writing',
  created_at: '', updated_at: '', chapter_count: 2, total_words: 8, cover_updated_at: null, auto_analyze: true,
  wizard_progress: { done: [], skipped: [], current: null }, default_style_id: null, default_style: null, synopsis: '',
  theme: '', tags: [], selling_points: '', audience: '', target_words: null, last_edited_chapter_id: null,
  reading: { chapter_id: 12, chapter_no: 2, ratio: 0.7 },
})
const chapter = { chapter: { id: 11, project_id: 9, outline_id: null, plan_id: null, style_id: null, chapter_no: 1,
  title: '', status: 'done' as const, word_count: 4, content_revision: 1, updated_at: '', content: '已经写好的正文' },
content_hash: 'a'.repeat(64), blocks: null }

function renderReader(access: ProjectAccess, user: User, withChapter = true) {
  view.effects = []
  view.layouts = []
  view.loaders = []
  view.params.cid = withChapter ? '11' : undefined
  view.loads = [{ project: project(access), toc }, ...(withChapter ? [chapter] : [])]
  useAuthStore.setState({ status: 'authenticated', user })
  return renderToString(createElement(ReaderPage))
}

function progressEffect(canWrite: boolean) {
  const effects = view.effects.filter(({ deps }) => deps?.length === 5 && deps[0] === true
    && typeof deps[1] === 'function' && typeof deps[2] === 'function' && deps[3] === canWrite
    && typeof deps[4] === 'function')
  expect(effects).toHaveLength(1)
  return effects[0].effect
}

function installProgressRuntime() {
  const callbacks: Record<string, () => void> = {}
  const frames: Array<() => void> = []
  const timers: Array<() => void> = []
  vi.stubGlobal('window', {
    addEventListener: (name: string, callback: () => void) => { callbacks[name] = callback },
    removeEventListener: vi.fn(), requestAnimationFrame: (callback: () => void) => { frames.push(callback); return 1 },
    cancelAnimationFrame: vi.fn(), setTimeout: (callback: () => void) => { timers.push(callback); return 1 },
    clearTimeout: vi.fn(), scrollY: 500, innerHeight: 1000,
  })
  vi.stubGlobal('document', { documentElement: { scrollHeight: 2000 }, visibilityState: 'visible',
    addEventListener: vi.fn(), removeEventListener: vi.fn() })
  const cleanup = progressEffect(true)()
  callbacks.scroll()
  expect(frames).toHaveLength(1)
  frames[0]()
  expect(timers).toHaveLength(1)
  return { cleanup, timer: timers[0] }
}

afterEach(() => {
  useAuthStore.getState().clear()
  view.effects = []
  view.layouts = []
  view.loads = []
  view.loaders = []
  view.search = ''
  view.navigate.mockClear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('阅读器的协作能力', () => {
  it('只读协作者从搜索进入时直接高亮命中，保留只读能力', () => {
    view.search = new URL(bookSearchHref(9, 11, { start: 5, end: 7, content_hash: chapter.content_hash }), 'https://reader.test').search
    const html = renderReader(viewer, actor(3))
    expect(html).toMatch(/data-mention-focus="true"[^>]*>正文<\/mark>/)
    expect(html).toContain('查看本章')
    expect(html).not.toContain('编辑本章')
    expect(progressEffect(false)()).toBeUndefined()
  })
  it.each([
    ['viewer', viewer, actor(3), false],
    ['editor', { ...viewer, role: 'editor' as const, can_edit_content: true, can_use_ai: true }, actor(3), true],
    ['管理员兼 viewer', { ...viewer, can_edit_content: true }, actor(3, true), true],
  ])('%s 仅按人工编辑能力显示编辑入口，并不挂载分析或进度监听', (_label, access, user, canEdit) => {
    const html = renderReader(access, user)
    expect(html).toContain(canEdit ? '编辑本章' : '查看本章')
    expect(html).not.toContain('管理员正在管理')
    expect(html).not.toContain('data-testid="analysis"')
    const add = vi.fn()
    vi.stubGlobal('window', { addEventListener: add })
    expect(progressEffect(false)()).toBeUndefined()
    expect(add).not.toHaveBeenCalled()
  })

  it.each([
    ['作者', owner, actor(2), '/projects/9/read/12'],
    ['查看者', viewer, actor(3), '/projects/9/read/11'],
    ['编辑者', { ...viewer, role: 'editor' as const, can_edit_content: true, can_use_ai: true }, actor(3), '/projects/9/read/11'],
  ])('%s 的初始章节只使用被允许的阅读进度', (_label, access, user, destination) => {
    renderReader(access, user, false)
    const initial = view.effects.filter(({ deps }) => deps?.length === 6 && deps[0] === null && typeof deps[1] === 'number')
    expect(initial).toHaveLength(1)
    initial[0].effect()
    expect(view.navigate).toHaveBeenCalledWith(destination, { replace: true, state: null })
  })

  it('作者待上报进度在退出并以相同账号重登后不会通过定时器或卸载发送', () => {
    const save = vi.spyOn(readerApi, 'saveProgress').mockResolvedValue(undefined)
    const fetch = vi.fn().mockResolvedValue({})
    renderReader(owner, actor(2))
    vi.stubGlobal('fetch', fetch)
    const { cleanup, timer } = installProgressRuntime()
    useAuthStore.getState().clear()
    useAuthStore.setState({ status: 'authenticated', user: actor(2) })
    timer()
    if (typeof cleanup === 'function') cleanup()
    expect(save).not.toHaveBeenCalled()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('作者在当前会话中仍上报进度', () => {
    const save = vi.spyOn(readerApi, 'saveProgress').mockResolvedValue(undefined)
    renderReader(owner, actor(2))
    const { timer } = installProgressRuntime()
    timer()
    expect(save).toHaveBeenCalledExactlyOnceWith(9, { chapter_id: 11, ratio: 0.5 })
  })

  it('章节返回 403 后清除当前阅读会话，待发送进度不再提交', async () => {
    const save = vi.spyOn(readerApi, 'saveProgress').mockResolvedValue(undefined)
    const fetch = vi.fn().mockResolvedValue({})
    vi.spyOn(readerApi, 'chapter').mockRejectedValue(new ApiError(403, '无权访问'))
    vi.spyOn(projectApi, 'get').mockRejectedValue(new ApiError(403, '作品无权访问'))
    renderReader(owner, actor(2))
    vi.stubGlobal('fetch', fetch)
    const { cleanup, timer } = installProgressRuntime()
    await expect(view.loaders[1]()).rejects.toBeInstanceOf(ApiError)
    timer()
    if (typeof cleanup === 'function') cleanup()
    expect(save).not.toHaveBeenCalled()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('章节 404 但作品仍可读时仅报告缺章，阅读会话和进度继续有效', async () => {
    const save = vi.spyOn(readerApi, 'saveProgress').mockResolvedValue(undefined)
    vi.spyOn(readerApi, 'chapter').mockRejectedValue(new ApiError(404, '章节不存在'))
    const get = vi.spyOn(projectApi, 'get').mockResolvedValue(project(owner))
    renderReader(owner, actor(2))
    const { timer } = installProgressRuntime()
    await expect(view.loaders[1]()).rejects.toMatchObject({ status: 404 })
    timer()
    expect(get).toHaveBeenCalledTimes(1)
    expect(save).toHaveBeenCalledExactlyOnceWith(9, { chapter_id: 11, ratio: 0.5 })
  })

  it('章节 404 后确认作品时遇到网络错误，不当作撤权', async () => {
    const save = vi.spyOn(readerApi, 'saveProgress').mockResolvedValue(undefined)
    vi.spyOn(readerApi, 'chapter').mockRejectedValue(new ApiError(404, '章节不存在'))
    vi.spyOn(projectApi, 'get').mockRejectedValue(new ApiError(0, '网络错误'))
    renderReader(owner, actor(2))
    const { timer } = installProgressRuntime()
    await expect(view.loaders[1]()).rejects.toMatchObject({ status: 404 })
    timer()
    expect(save).toHaveBeenCalledExactlyOnceWith(9, { chapter_id: 11, ratio: 0.5 })
  })

  it('目录子资源 404 仅显示加载错误，权威作品仍可读时不关闭阅读会话', async () => {
    const save = vi.spyOn(readerApi, 'saveProgress').mockResolvedValue(undefined)
    const get = vi.spyOn(projectApi, 'get').mockResolvedValue(project(owner))
    vi.spyOn(readerApi, 'toc').mockRejectedValue(new ApiError(404, '目录不存在'))
    renderReader(owner, actor(2))
    const { timer } = installProgressRuntime()
    await expect(view.loaders[0]()).rejects.toMatchObject({ status: 404 })
    timer()
    expect(get).toHaveBeenCalledTimes(2)
    expect(save).toHaveBeenCalledExactlyOnceWith(9, { chapter_id: 11, ratio: 0.5 })
  })

  it('迟到章节 404 的作品复查不能关闭已更新章节的阅读会话', async () => {
    const save = vi.spyOn(readerApi, 'saveProgress').mockResolvedValue(undefined)
    const slow = deferred<Project>()
    vi.spyOn(readerApi, 'chapter').mockRejectedValueOnce(new ApiError(404, '章节不存在')).mockResolvedValueOnce(chapter)
    vi.spyOn(projectApi, 'get').mockReturnValueOnce(slow.promise)
    renderReader(owner, actor(2))
    const { timer } = installProgressRuntime()
    const old = view.loaders[1]().catch((error: unknown) => error)
    await Promise.resolve(); await Promise.resolve()
    await expect(view.loaders[1]()).resolves.toEqual(chapter)
    slow.reject(new ApiError(403, '旧作品读取失败'))
    expect(await old).toMatchObject({ status: 404 })
    timer()
    expect(save).toHaveBeenCalledExactlyOnceWith(9, { chapter_id: 11, ratio: 0.5 })
  })

  it('章节切换或授权轮次变化的布局清理立即使旧确认失效', async () => {
    const save = vi.spyOn(readerApi, 'saveProgress').mockResolvedValue(undefined)
    const slow = deferred<Project>()
    vi.spyOn(readerApi, 'chapter').mockRejectedValue(new ApiError(404, '章节不存在'))
    vi.spyOn(projectApi, 'get').mockReturnValue(slow.promise)
    renderReader(owner, actor(2))
    const { timer } = installProgressRuntime()
    const invalidate = view.layouts.find(({ deps }) => deps?.[0] === 11)?.effect()
    const old = view.loaders[1]().catch((error: unknown) => error)
    await Promise.resolve(); await Promise.resolve()
    if (typeof invalidate === 'function') invalidate()
    slow.reject(new ApiError(403, '旧作品读取失败'))
    expect(await old).toMatchObject({ status: 404 })
    timer()
    expect(save).toHaveBeenCalledExactlyOnceWith(9, { chapter_id: 11, ratio: 0.5 })
  })

  it('迟到的旧目录响应不能覆盖较新阅读位置', async () => {
    const slow = deferred<Project>()
    const newer = { ...project(owner), reading: { chapter_id: 11, chapter_no: 1, ratio: 0.7 } }
    vi.spyOn(projectApi, 'get').mockReturnValueOnce(slow.promise).mockResolvedValueOnce(newer)
    vi.spyOn(readerApi, 'toc').mockResolvedValue(toc)
    renderReader(owner, actor(2))
    const oldRequest = view.loaders[0]()
    await view.loaders[0]()
    slow.resolve({ ...project(owner), reading: { chapter_id: 11, chapter_no: 1, ratio: 0.1 } })
    await expect(oldRequest).rejects.toThrow('作品访问权限已变化')
    const frames: Array<() => void> = []
    const scrollTo = vi.fn()
    vi.stubGlobal('window', { requestAnimationFrame: (callback: () => void) => { frames.push(callback); return 1 },
      cancelAnimationFrame: vi.fn(), scrollTo, innerHeight: 1000, setTimeout: vi.fn(() => 1), clearTimeout: vi.fn() })
    vi.stubGlobal('document', { documentElement: { scrollHeight: 2000 } })
    const restore = view.effects.filter(({ deps }) => deps?.length === 7 && deps[0] === chapter.chapter && deps[1] === true)
    expect(restore).toHaveLength(1)
    restore[0].effect()
    frames[0]()
    expect(scrollTo).toHaveBeenCalledWith(0, 700)
  })

  it('旧目录请求的迟到 403 不会使较新的会话失效', async () => {
    const slow = deferred<Project>()
    const save = vi.spyOn(readerApi, 'saveProgress').mockResolvedValue(undefined)
    vi.spyOn(projectApi, 'get').mockReturnValueOnce(slow.promise).mockResolvedValueOnce(project(owner))
    vi.spyOn(readerApi, 'toc').mockResolvedValue(toc)
    renderReader(owner, actor(2))
    const oldRequest = view.loaders[0]().catch((error: unknown) => error)
    await view.loaders[0]()
    slow.reject(new ApiError(403, '旧请求失效'))
    expect(await oldRequest).toBeInstanceOf(ApiError)
    const { timer } = installProgressRuntime()
    timer()
    expect(save).toHaveBeenCalledExactlyOnceWith(9, { chapter_id: 11, ratio: 0.5 })
  })

  it('目录读取期间邀请轮次变化时，不返回旧轮次的目录', async () => {
    const newer = project({ ...viewer, authorization_version: 'grant-2' })
    vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project(viewer)).mockResolvedValueOnce(newer)
    vi.spyOn(readerApi, 'toc').mockResolvedValue(toc)
    renderReader(viewer, actor(3), false)
    await expect(view.loaders[0]()).rejects.toThrow('作品访问权限已变化')
  })

  it('章节响应后授权轮次已变化时，不显示旧轮次的正文', async () => {
    vi.spyOn(readerApi, 'chapter').mockResolvedValue(chapter)
    vi.spyOn(projectApi, 'get').mockResolvedValue(project({ ...viewer, authorization_version: 'grant-2' }))
    renderReader(viewer, actor(3))
    await expect(view.loaders[1]()).rejects.toThrow('作品访问权限已变化')
  })

  it('同一有效邀请可正常读取目录和章节', async () => {
    vi.spyOn(projectApi, 'get').mockResolvedValue(project(viewer))
    vi.spyOn(readerApi, 'toc').mockResolvedValue(toc)
    vi.spyOn(readerApi, 'chapter').mockResolvedValue(chapter)
    renderReader(viewer, actor(3))
    await expect(view.loaders[0]()).resolves.toMatchObject({ toc })
    await expect(view.loaders[1]()).resolves.toEqual(chapter)
  })

  it('正文后的授权复查在同账号重新登录后返回，也不能接受旧会话正文', async () => {
    const pending = deferred<Project>()
    vi.spyOn(projectApi, 'get').mockReturnValue(pending.promise)
    vi.spyOn(readerApi, 'chapter').mockResolvedValue(chapter)
    renderReader(viewer, actor(3))
    const result = view.loaders[1]()
    await Promise.resolve()
    useAuthStore.getState().clear()
    useAuthStore.setState({ status: 'authenticated', user: actor(3) })
    pending.resolve(project(viewer))
    await expect(result).rejects.toThrow('作品访问权限已变化')
  })
})
