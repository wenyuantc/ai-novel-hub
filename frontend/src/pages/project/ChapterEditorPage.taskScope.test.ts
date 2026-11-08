import { App } from 'antd'
import { createElement, type DependencyList, type EffectCallback } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Chapter, ReviseChapterRequest, Scene, Task } from '../../api/types'
import type { TaskOutcome } from '../../hooks/useTaskStream'
import ChapterEditorPage from './ChapterEditorPage'

// SSR 只捕获真实编辑器交给分析抽屉的动作，执行真实 runRevise/followTask；不模拟浏览器交互。
const view = vi.hoisted(() => ({
  chapter: null as Chapter | null,
  scene: null as Scene | null,
  draft: '', loaderCalls: 0,
  activeTasks: [] as Task[],
  controller: null as Record<string, unknown> | null,
  lastRef: null as { current: unknown } | null,
  otherSceneRef: null as { current: unknown } | null,
  effects: [] as { setup: EffectCallback; deps: DependencyList | undefined }[],
  stateUpdates: [] as unknown[],
  onRevise: null as ((body: ReviseChapterRequest) => Promise<boolean>) | null,
  reloadSiblings: vi.fn(), refreshProject: vi.fn(async () => {}), refreshAnalysis: vi.fn(async () => null),
  analyze: vi.fn(async () => {}), loadScene: vi.fn(async () => true), changeScene: vi.fn(), track: vi.fn(),
  createTask: vi.fn<(pid: number, cid: number, sid: number, body: object) => Promise<Task>>(),
  follow: vi.fn<(task: Task) => Promise<TaskOutcome>>(),
  getChapter: vi.fn<(pid: number, cid: number) => Promise<Chapter>>(),
  parseDone: vi.fn(),
}))
vi.mock('react', async (original) => {
  const actual = await original<typeof import('react')>()
  return { ...actual,
    useRef: (initial: unknown) => { const ref = actual.useRef(initial); view.lastRef = ref; return ref },
    useEffect: (setup: EffectCallback, deps?: DependencyList) => {
      // SSR 不执行 effect。只为这一个真实 watcher 续接上次的 ref，随后由测试运行其真实闭包。
      if (view.controller && deps?.includes(view.controller) && typeof deps[0] === 'boolean' && view.lastRef) {
        if (view.otherSceneRef) view.lastRef.current = view.otherSceneRef.current
        view.otherSceneRef = view.lastRef
      }
      view.effects.push({ setup, deps })
      actual.useEffect(setup, deps)
    },
    useState: (initial: unknown) => {
      const [state, setState] = actual.useState(initial)
      return [state, (value: unknown) => { view.stateUpdates.push(value); setState(value) }] as const
    },
  }
})
vi.mock('../../hooks/useProjectId', () => ({ useProjectId: () => 1 }))
vi.mock('../../hooks/useProjectAccess', () => ({ useProjectAccess: () => ({ canUseAI: true, canEdit: true, canEditNow: () => true, canReadNow: () => true, managingOther: false }) }))
vi.mock('../../hooks/useLoader', () => ({ useLoader: () => ({ data: view.loaderCalls++ === 0 ? view.chapter : [view.chapter], reload: view.reloadSiblings }) }))
vi.mock('../../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
vi.mock('../../hooks/useElementWidth', () => ({ useElementWidth: () => [{ current: null }, 1000] }))
vi.mock('../../hooks/useHasAIConfig', () => ({ useHasAIConfig: () => true }))
vi.mock('../../hooks/useAgentPageContext', () => ({ usePublishAgentContext: () => {} }))
vi.mock('../../hooks/useProjectRefresh', () => ({ useProjectRefresh: () => {} }))
vi.mock('../../hooks/useChapterAnalysis', () => ({ useChapterAnalysis: () => ({ state: { latest_analysis_id: 5 }, latest: null, status: 'analyzed', running: false, refresh: view.refreshAnalysis, start: view.analyze }) }))
vi.mock('../../hooks/useTaskStream', () => ({ useTaskStream: () => ({ running: false, task: null, text: '', error: null, follow: view.follow, detach: () => {} }) }))
vi.mock('../../stores/project', () => ({ useProjectStore: (select: (state: unknown) => unknown) => select({ project: { auto_analyze: true, default_style: null }, refresh: view.refreshProject }) }))
vi.mock('../../stores/tasks', () => ({ useTasksStore: (select: (state: unknown) => unknown) => select({ active: view.activeTasks, track: view.track }) }))
vi.mock('../../stores/auth', () => ({ useAuthStore: (select: (state: unknown) => unknown) => select({ status: 'authenticated', user: { id: 1 } }) }))
vi.mock('react-router-dom', () => ({ useParams: () => ({ cid: '3' }), useNavigate: () => () => {}, useLocation: () => ({ state: null }), useSearchParams: () => [new URLSearchParams(), () => {}], useBlocker: () => ({ state: 'unblocked' }) }))
vi.mock('../../api/scenes', () => ({ sceneApi: { revise: view.createTask } }))
vi.mock('../../api/chapters', async (original) => {
  const actual = await original<typeof import('../../api/chapters')>()
  return { ...actual, chapterApi: { ...actual.chapterApi, get: view.getChapter }, parseChapterDone: view.parseDone }
})
vi.mock('../../components/scenes/useSceneEditor', () => ({ useSceneEditor: () => ({
  scene: view.scene, draft: { title: '场景', content: view.draft, summary: '' }, dirty: false, saving: false, conflict: null, externalChange: false,
  page: { items: [{ id: 22 }, { id: 23 }], total: 2 },
  controller: view.controller,
}) }))
vi.mock('../../components/chapter/AnalysisDrawer', () => ({ default: ({ onRevise }: { onRevise: (body: ReviseChapterRequest) => Promise<boolean> }) => { view.onRevise = onRevise; return null } }))
vi.mock('../../components/scenes/SceneWritingPanel', () => ({ default: () => null }))
vi.mock('../../components/chapter/VersionDrawer', () => ({ default: () => null }))
vi.mock('../../components/chapter/RewritePanel', () => ({ default: () => null }))
vi.mock('../../components/chapter/CastSelect', () => ({ default: () => null }))
vi.mock('../../components/chapter/ChapterStyleSetting', () => ({ default: () => null }))
vi.mock('../../components/chapter/ContextPreviewPanel', () => ({ default: () => null }))
vi.mock('../../components/foreshadows/ChapterForeshadowButton', () => ({ default: () => null }))
vi.mock('../../components/AIConfigSelect', () => ({ default: () => null }))
vi.mock('../../components/skills/SkillSelect', () => ({ default: () => null }))
vi.mock('../../components/styles/StyleChoiceSelect', () => ({ default: () => null }))

function scene(id: number): Scene {
  return {
    id, chapter_id: 3, chapter_no: 1, chapter_title: '章', title: `场景${id}`, sort: id - 22, content: '原场景正文', summary: '',
    separator_before: '', active: true, word_count: 5, content_revision: 1, planning_revision: 1, created_at: '', updated_at: '',
    planning: { unit_id: null, binding_epoch: null, revision: 1, raw: { modes: { pov: 'inherit', tags: 'inherit', character: 'inherit', world: 'inherit', organization: 'inherit', career: 'inherit', relationship: 'inherit' }, pov_character_id: null, tag_ids: [], references: [] }, effective: { pov_character_id: null, pov_character_name: null, tags: [], references: [], sources: { pov: 'chapter', tags: 'chapter', character: 'chapter', world: 'chapter', organization: 'chapter', career: 'chapter', relationship: 'chapter' } } },
  }
}
function task(status: Task['status'] = 'running'): Task {
  return { id: 10, kind: 'chapter.revise', status, title: '场景22重写', project_id: 1, chapter_id: 3, scene_id: 22, analysis_id: 5, progress: 0, stage: '', error: '', retry_of_id: null, character_id: null, outline_id: null, checkpoint: null, step_cursor: 0, queue_position: null, created_at: '', started_at: null, finished_at: null }
}
beforeEach(() => {
  vi.clearAllMocks()
  view.loaderCalls = 0
  view.onRevise = null
  view.activeTasks = []
  view.lastRef = null
  view.otherSceneRef = null
  view.effects = []
  view.stateUpdates = []
  view.chapter = { id: 3, project_id: 1, outline_id: null, plan_id: null, style_id: null, chapter_no: 1, title: '章', status: 'draft', word_count: 10, content_revision: 1, content: '原整章正文', updated_at: '' }
  view.scene = scene(22)
  view.draft = '原场景正文'
  view.controller = { getSnapshot: () => ({ scene: view.scene, draft: { content: view.draft } }), load: view.loadScene, change: view.changeScene }
  view.getChapter.mockResolvedValue({ ...view.chapter, content_revision: 2, content: '后台整章投影' })
  view.createTask.mockResolvedValue(task())
})

describe('另一场景任务终态的章分析刷新', () => {
  const renderStoreState = (activeTasks: Task[]) => {
    view.activeTasks = activeTasks
    view.loaderCalls = 0
    view.effects = []
    renderToStaticMarkup(createElement(App, null, createElement(ChapterEditorPage)))
    const watcher = view.effects.find((effect) => effect.deps?.includes(view.controller))
    if (!watcher) throw new Error('未捕获真实异景 active-store watcher')
    return { watcher, attach: view.effects.find((effect) => effect.deps?.[0] === activeTasks) }
  }

  it('未订阅的另一Scene从active store退出后刷新章分析/列表，只读保留当前草稿且只执行一次', async () => {
    view.scene = scene(23)
    view.draft = '场景23未保存草稿😀'
    const active = renderStoreState([task()])
    active.attach?.setup()
    active.watcher.setup()
    expect(view.follow).not.toHaveBeenCalled()
    expect(view.otherSceneRef?.current).toBe(true)
    expect(view.refreshAnalysis).not.toHaveBeenCalled()

    const finished = renderStoreState([])
    view.stateUpdates = []
    finished.watcher.setup()
    await vi.waitFor(() => expect(view.refreshAnalysis).toHaveBeenCalledExactlyOnceWith())
    expect(view.getChapter).toHaveBeenCalledExactlyOnceWith(1, 3)
    expect(view.refreshAnalysis).toHaveBeenCalledExactlyOnceWith()
    expect(view.reloadSiblings).toHaveBeenCalledExactlyOnceWith()
    expect(view.refreshProject).toHaveBeenCalledExactlyOnceWith()
    expect(view.stateUpdates.filter((value) => value === null)).toHaveLength(1) // 清掉旧全章 compare
    expect(view.loadScene).toHaveBeenCalledExactlyOnceWith() // 默认 preserveDraft，不传 false
    expect(view.analyze).not.toHaveBeenCalled()
    expect(view.follow).not.toHaveBeenCalled()
    expect(view.parseDone).not.toHaveBeenCalled()
    expect(view.changeScene).not.toHaveBeenCalled()
    expect(view.scene.id).toBe(23)
    expect(view.draft).toBe('场景23未保存草稿😀')
    finished.watcher.setup()
    expect(view.refreshAnalysis).toHaveBeenCalledTimes(1)
    expect(view.reloadSiblings).toHaveBeenCalledTimes(1)
  })

  it('首次挂载没有异景任务时不伪造退出事件或清理比较', () => {
    renderStoreState([]).watcher.setup()
    expect(view.getChapter).not.toHaveBeenCalled()
    expect(view.refreshAnalysis).not.toHaveBeenCalled()
    expect(view.reloadSiblings).not.toHaveBeenCalled()
    expect(view.refreshProject).not.toHaveBeenCalled()
    expect(view.stateUpdates.filter((value) => value === null)).toHaveLength(0)
  })

  it.each(['succeeded', 'cancelled'] as const)('%s 只按真实终态刷新派生状态，保留当前另一场景草稿', async (status) => {
    let finish!: (outcome: TaskOutcome) => void
    view.follow.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    renderToStaticMarkup(createElement(App, null, createElement(ChapterEditorPage)))
    if (!view.onRevise) throw new Error('分析抽屉未获得真实编辑器动作')
    expect(await view.onRevise({ analysis_id: 5, suggestion_ids: [1], instruction: '', preserve: [] })).toBe(true)
    expect(view.createTask).toHaveBeenCalledWith(1, 3, 22, expect.any(Object))

    // 在任务返回前已打开另一 Scene，并留下本地草稿；终态不能套用旧目标的整章结果。
    view.scene = scene(23)
    view.draft = '场景23未保存草稿😀'
    finish(status === 'succeeded'
      ? { status, task: task(status), result: { chapter: { ...view.chapter, content: '后台整章稿，不能写入当前Scene' } } }
      : { status, task: task(status), detail: '已取消' })
    await vi.waitFor(() => expect(view.loadScene).toHaveBeenCalledExactlyOnceWith())
    expect(view.getChapter).toHaveBeenCalledExactlyOnceWith(1, 3)
    expect(view.refreshAnalysis).toHaveBeenCalledTimes(status === 'succeeded' ? 1 : 0)
    expect(view.reloadSiblings).toHaveBeenCalledTimes(status === 'succeeded' ? 1 : 0)
    expect(view.refreshProject).toHaveBeenCalledTimes(status === 'succeeded' ? 1 : 0)
    expect(view.analyze).not.toHaveBeenCalled()
    expect(view.parseDone).not.toHaveBeenCalled()
    expect(view.changeScene).not.toHaveBeenCalled()
    expect(view.scene.id).toBe(23)
    expect(view.draft).toBe('场景23未保存草稿😀')
  })
})
