import { afterEach, describe, expect, it, vi } from 'vitest'
import { IDBFactory } from 'fake-indexeddb'
import { chapterApi } from '../../api/chapters'
import { ApiError } from '../../api/client'
import type { Chapter, Scene, ScenePage, SceneUpdate } from '../../api/types'
import { SceneDraftController } from '../scenes/sceneDraftController'
import { ChapterAutosave } from './chapterAutosave'
import { AUTOSAVE_DELAY, AUTOSAVE_MAX_WAIT, ChapterAutosaveTiming } from './chapterAutosaveTiming'
import { chapterTitleDirty, ChapterTitleDraft } from './chapterTitleDraft'

vi.mock('../../stores/project', () => ({ isProjectScopeCurrent: () => true }))
vi.mock('./verifyDraftScope', () => ({
  verifyDraftScope: async () => true,
  invalidateMismatchedEditorActor: () => {},
}))
vi.mock('../../utils/editorDraftStorage', async (original) => {
  const actual = await original<typeof import('../../utils/editorDraftStorage')>()
  return { ...actual, editorDraftStorage: actual.createEditorDraftStorage({ factory: new IDBFactory(), broadcast: false }) }
})

function chapter(revision = 2, title = '原章标题', content = '原正文'): Chapter {
  return {
    id: 9, project_id: 3, outline_id: null, plan_id: null, style_id: null, title, content,
    chapter_no: 1, content_revision: revision, status: 'draft', word_count: content.length, updated_at: '2026-11-17T08:00:00Z',
  }
}

function scene(base: Chapter): Scene {
  return {
    id: 4, chapter_id: base.id, chapter_no: base.chapter_no, chapter_title: base.title,
    content_revision: base.content_revision, planning_revision: 0, title: '景名', summary: '摘要',
    content: base.content, separator_before: '', sort: 0, active: true, word_count: base.word_count,
    created_at: '2026-11-17T08:00:00Z', updated_at: '2026-11-17T08:00:00Z',
    planning: {
      unit_id: null, binding_epoch: null, revision: 0,
      raw: { modes: { pov: 'inherit', tags: 'inherit', character: 'inherit', world: 'inherit',
        organization: 'inherit', career: 'inherit', relationship: 'inherit' }, pov_character_id: null, tag_ids: [], references: [] },
      effective: { pov_character_id: null, pov_character_name: null, tags: [], references: [],
        sources: { pov: 'chapter', tags: 'chapter', character: 'chapter', world: 'chapter',
          organization: 'chapter', career: 'chapter', relationship: 'chapter' } },
    },
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

const cleanups: (() => void)[] = []
afterEach(() => {
  for (const cleanup of cleanups.splice(0)) cleanup()
  vi.restoreAllMocks()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

async function setup() {
  const browser = new EventTarget()
  const connectivity = { onLine: true }
  vi.stubGlobal('window', browser)
  vi.stubGlobal('document', new EventTarget())
  vi.stubGlobal('navigator', connectivity)
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] })
  let remote = chapter()
  let remoteScene = scene(remote)
  const get = vi.spyOn(chapterApi, 'get').mockImplementation(async () => remote)
  const saveTitle = vi.spyOn(chapterApi, 'saveTitle')
  const setRemote = (value: Chapter) => { remote = value; remoteScene = scene(value) }
  const update = vi.fn(async (_sid: number, body: SceneUpdate) => {
    remote = chapter(remote.content_revision + 1, remote.title, body.content ?? remote.content)
    remoteScene = { ...remoteScene, ...body, content_revision: remote.content_revision }
    return remoteScene
  })
  const controller = new SceneDraftController(remote.id, {
    list: async (): Promise<ScenePage> => ({
      items: [remoteScene], total: 1, offset: 0, limit: 100, content_revision: remote.content_revision, planning_revision: 0,
    }), get: async () => remoteScene, update,
  }, () => true, () => true)
  controller.activate()
  cleanups.push(() => controller.deactivate())
  await controller.load()
  const born = '2026-11-17T08:00:00.123456Z'
  const save = new ChapterAutosave(remote, controller, { actorId: 7, projectId: 3, version: 1, identity: 'actor' },
    { actor_id: 7, project_id: 3, actor_created_at: born, project_created_at: born })
  save.setBlocked(false)
  cleanups.push(save.activate())
  await save.review()
  expect(save.readyToSave).toBe(true)
  return { save, get, saveTitle, update, setRemote, controller, born, browser, connectivity }
}

describe('章节保存调度', () => {
  it('停顿后提交，持续输入仍以首次脏编辑的 10 秒为上限', async () => {
    vi.useFakeTimers()
    const fired = vi.fn()
    const timing = new ChapterAutosaveTiming(fired)
    timing.pause(false)
    timing.edit()
    await vi.advanceTimersByTimeAsync(AUTOSAVE_DELAY - 1)
    expect(fired).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    expect(fired).toHaveBeenCalledOnce()
    fired.mockClear()
    timing.edit()
    for (let i = 0; i < 9; i += 1) {
      await vi.advanceTimersByTimeAsync(1_000)
      timing.edit()
    }
    expect(fired).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(AUTOSAVE_MAX_WAIT - 9_000)
    expect(fired).toHaveBeenCalledOnce()
  })

  it('输入法暂停冻结，网络 ACK 只推进已提交标题，后续输入使用新基线', async () => {
    const { save, saveTitle, setRemote, controller, born } = await setup()
    const ack = deferred<Chapter>()
    saveTitle.mockImplementationOnce(() => ack.promise)
    save.setTitleComposing(true)
    save.changeTitle('第一稿')
    await vi.advanceTimersByTimeAsync(AUTOSAVE_MAX_WAIT + 1)
    expect(saveTitle).not.toHaveBeenCalled()
    save.setTitleComposing(false)
    await vi.waitFor(() => expect(saveTitle).toHaveBeenCalledOnce())
    expect(saveTitle.mock.calls[0][2]).toEqual({
      title: '第一稿', expected_title: '原章标题', expected_scene_id: 4, expected_content_revision: 2,
    })
    expect(saveTitle.mock.calls[0][3]).toEqual({ id: 7, createdAt: born })
    save.changeTitle('第二稿')
    const first = chapter(3, '第一稿')
    setRemote(first)
    ack.resolve(first)
    await vi.waitFor(() => expect(save.saving).toBe(false))
    expect(save.title.getSnapshot()).toMatchObject({ value: '第二稿', base: { title: '第一稿', revision: 3 } })
    expect(controller.getSnapshot().scene?.content_revision).toBe(3)
    saveTitle.mockResolvedValueOnce(chapter(4, '第二稿'))
    await vi.advanceTimersByTimeAsync(AUTOSAVE_DELAY)
    await vi.waitFor(() => expect(saveTitle).toHaveBeenCalledTimes(2))
    expect(saveTitle.mock.calls[1][2]).toMatchObject({ title: '第二稿', expected_title: '第一稿', expected_content_revision: 3 })
  })

  it('未知标题结果重试遇同版本 409 仍保留原请求，新输入和破坏性动作继续等待核对', async () => {
    const { save, saveTitle } = await setup()
    saveTitle.mockRejectedValueOnce(new ApiError(0, '断网'))
    save.changeTitle('可能已写')
    expect(await save.saveNow()).toBe(false)
    const frozen = save.title.getSnapshot().attempt
    expect(frozen?.title).toBe('可能已写')
    save.changeTitle('继续编辑')
    expect(await save.retry()).toBe(false)
    expect(save.retryableUnknown).toBe(true)
    saveTitle.mockRejectedValueOnce(new ApiError(409, '写入占用', { code: 'collaboration_busy' }))
    expect(await save.retry()).toBe(false)
    expect(save.title.getSnapshot()).toMatchObject({ unknown: true, value: '继续编辑', attempt: frozen })
    const action = vi.fn(async () => true)
    expect(await save.runSavedAction(action)).toBeUndefined()
    expect(action).not.toHaveBeenCalled()
    expect(saveTitle.mock.calls[0][2]).toEqual(saveTitle.mock.calls[1][2])
  })

  it('标题提交后丢失响应，核对只推进标题版本并保留在途编辑正文', async () => {
    const { save, saveTitle, update, setRemote, controller } = await setup()
    saveTitle.mockImplementationOnce(async () => {
      controller.change({ content: '标题响应前继续写的正文' })
      setRemote(chapter(3, '已写标题'))
      throw new ApiError(0, '响应丢失')
    })
    save.changeTitle('已写标题')
    expect(await save.saveNow()).toBe(false)
    expect(save.unknown).toBe(true)
    expect(await save.review()).toBe(true)
    expect(save.reviewNeeded).toBe(false)
    expect(save.error).toBeNull()
    expect(save.title.getSnapshot()).toMatchObject({ base: { title: '已写标题', revision: 3 }, attempt: null })
    expect(controller.getSnapshot().draft.content).toBe('标题响应前继续写的正文')
    await vi.advanceTimersByTimeAsync(AUTOSAVE_DELAY)
    await vi.waitFor(() => expect(update).toHaveBeenCalledOnce())
    expect(update.mock.calls[0][1]).toMatchObject({ expected_content_revision: 3, content: '标题响应前继续写的正文' })
    expect(saveTitle).toHaveBeenCalledOnce()
  })

  it('自动正文不提交作者摘要，动作等待手动字段和标题都已保存', async () => {
    const { save, saveTitle, update, setRemote, controller } = await setup()
    controller.change({ summary: '作者正在写摘要', content: '新正文' })
    await vi.advanceTimersByTimeAsync(AUTOSAVE_DELAY)
    await vi.waitFor(() => expect(update).toHaveBeenCalledTimes(1))
    expect(update.mock.calls[0][1]).toEqual({ content: '新正文', expected_content_revision: 2 })
    expect(controller.getSnapshot().draft.summary).toBe('作者正在写摘要')
    save.changeTitle('新标题')
    saveTitle.mockImplementationOnce(async (_pid, _cid, body) => {
      const updated = chapter(body.expected_content_revision + 1, body.title, '新正文')
      setRemote(updated)
      return updated
    })
    const action = vi.fn(async () => controller.getSnapshot().scene?.content_revision)
    expect(await save.runSavedAction(action)).toBe(5)
    expect(update.mock.calls[1][1]).toEqual({ summary: '作者正在写摘要', expected_content_revision: 3 })
    expect(saveTitle.mock.calls[0][2].expected_content_revision).toBe(4)
    expect(action).toHaveBeenCalledOnce()
  })

  it('正文在途期间开始组合输入，标题写等待组合提交且不报失败', async () => {
    const { save, saveTitle, update, setRemote, controller } = await setup()
    const start = deferred<Scene>()
    update.mockImplementationOnce(() => start.promise)
    controller.change({ content: '等待 ACK 的正文' })
    save.changeTitle('已输入的标题')
    const saving = save.saveNow()
    await vi.waitFor(() => expect(update).toHaveBeenCalledOnce())
    save.setTitleComposing(true)
    const current = chapter(3, '原章标题', '等待 ACK 的正文')
    setRemote(current)
    start.resolve(scene(current))
    expect(await saving).toBe(false)
    expect(saveTitle).not.toHaveBeenCalled()
    expect(save.error).toBeNull()
    save.changeTitle('组合提交后的标题')
    saveTitle.mockResolvedValueOnce(chapter(4, '组合提交后的标题', current.content))
    save.setTitleComposing(false)
    await vi.advanceTimersByTimeAsync(AUTOSAVE_DELAY)
    await vi.waitFor(() => expect(saveTitle).toHaveBeenCalledOnce())
    expect(saveTitle.mock.calls[0][2]).toMatchObject({ title: '组合提交后的标题', expected_content_revision: 3 })
  })

  it('已知离线输入仍持久化，重连读取版本后才自动提交', async () => {
    const { save, saveTitle, setRemote, browser, connectivity, get } = await setup()
    connectivity.onLine = false
    browser.dispatchEvent(new Event('offline'))
    save.changeTitle('断网写下的标题')
    await vi.waitFor(() => expect(save.journal.getSnapshot().local).toBe('saved'))
    await vi.advanceTimersByTimeAsync(AUTOSAVE_MAX_WAIT)
    expect(saveTitle).not.toHaveBeenCalled()
    get.mockClear()
    saveTitle.mockImplementationOnce(async (_pid, _cid, body) => {
      expect(get).toHaveBeenCalled()
      const updated = chapter(3, body.title)
      setRemote(updated)
      return updated
    })
    connectivity.onLine = true
    browser.dispatchEvent(new Event('online'))
    await vi.waitFor(() => expect(saveTitle).toHaveBeenCalledOnce())
  })
})

describe('章标题基线', () => {
  it('从大纲继承的原样标题在未编辑时不产生草稿，编辑后仍按原值匹配', () => {
    const base = { title: ' 旧章名 ', revision: 4 }
    const title = new ChapterTitleDraft(base)
    expect(chapterTitleDirty(title.getSnapshot())).toBe(false)
    title.edit('旧章名', 7)
    expect(chapterTitleDirty(title.getSnapshot())).toBe(true)
    expect(title.freeze('s')?.body).toEqual({
      title: '旧章名', expected_title: base.title, expected_scene_id: 7, expected_content_revision: 4,
    })
  })

  it('请求遵循服务器标题空白语义，原标题保持精确值', () => {
    const title = new ChapterTitleDraft({ title: '现值', revision: 4 })
    title.edit('\u0085\u001c下一标题\uFEFF\u0085', 7)
    expect(title.freeze('s')?.body).toEqual({
      title: '下一标题\uFEFF', expected_title: '现值', expected_scene_id: 7, expected_content_revision: 4,
    })
  })
})
