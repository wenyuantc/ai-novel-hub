import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AIConfig, Project, ProjectAccess, User } from '../../api/types'
import * as projectsApi from '../../api/projects'
import { projectApi } from '../../api/projects'
import { useAuthStore } from '../../stores/auth'
import { captureProjectScope, useProjectStore } from '../../stores/project'
import { coverAvailability, coverChoice, requestCoverForScope, saveCoverForScope } from './coverGeneration'

const user: User = { id: 1, username: '编辑者', created_at: '', is_admin: false, is_super_admin: false,
  effective_permissions: [], roles: [], authorization_revision: 1 }
const editor: ProjectAccess = { role: 'editor', is_collaborator: true, authorization_version: 'grant-1', allow_owner_ai: true,
  can_read: true, can_edit_content: true, can_use_ai: true, can_export: true, can_manage_collaborators: false,
  can_recycle: false, can_backup: false, can_write_reading_progress: false }
const image = (id: number, is_default = false) => ({ id, is_default, model_type: 'image' } as AIConfig)
function project(access = editor): Project {
  return { id: 2, owner: { id: 9, username: '作者' }, access, title: '作品', genre: '', premise: '', status: 'planning',
    created_at: '', updated_at: '', chapter_count: 0, total_words: 0, cover_updated_at: null, auto_analyze: true,
    wizard_progress: { done: [], skipped: [], current: null }, default_style_id: null, default_style: null,
    synopsis: '', theme: '', tags: [], selling_points: '', audience: '', target_words: null, last_edited_chapter_id: null, reading: null }
}
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done }); return { promise, resolve } }
beforeEach(() => { useProjectStore.getState().reset(); useAuthStore.setState({ status: 'authenticated', user }) })
afterEach(() => { useProjectStore.getState().reset(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('封面生图能力与来源', () => {
  it('只有作者或真实 editor 且服务端确认可编辑、可使用 AI 时可生图', () => {
    expect(coverAvailability(project(), user, 2)).toBe(true)
    expect(coverAvailability(project({ ...editor, can_use_ai: false }), user, 2)).toBe(false)
    expect(coverAvailability(project({ ...editor, role: 'viewer', can_edit_content: true }), user, 2)).toBe(false)
    expect(coverAvailability(project({ ...editor, role: 'admin', is_collaborator: false, authorization_version: null }), user, 2)).toBe(false)
    expect(coverAvailability(project({ ...editor, can_edit_content: false }), user, 2)).toBe(false)
  })

  it('加载完成前不猜测费用；本人配置优先并显式固定，授权回退只在本人无配置时可选', () => {
    expect(coverChoice(editor, false, [], null)).toMatchObject({ allowed: false, mode: 'loading' })
    expect(coverChoice(editor, true, [image(2), image(3, true)], null)).toMatchObject({ allowed: true, mode: 'actor', configId: 3 })
    expect(coverChoice(editor, true, [image(2), image(3, true)], 2)).toMatchObject({ allowed: true, mode: 'actor', configId: 2 })
    expect(coverChoice(editor, true, [], null)).toMatchObject({ allowed: true, mode: 'owner' })
    expect(coverChoice({ ...editor, allow_owner_ai: false }, true, [], null)).toMatchObject({ allowed: false, mode: 'missing' })
    expect(coverChoice(editor, true, [], 99)).toMatchObject({ allowed: false, mode: 'invalid' })
  })

  it('旧轮次返回的图片不得显示，也不能在新轮次用旧预览保存', async () => {
    const changed = { ...editor, authorization_version: 'grant-2' }
    vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project()).mockResolvedValueOnce(project(changed))
    await useProjectStore.getState().load(2)
    const old = captureProjectScope(2)
    const pending = deferred<Response>()
    const fetch = vi.fn(() => pending.promise)
    vi.stubGlobal('fetch', fetch)
    const generating = requestCoverForScope(2, old, { prompt: '星空' }, 'owner', new AbortController().signal)
    await useProjectStore.getState().refresh()
    pending.resolve(new Response(JSON.stringify({ image_base64: 'YQ==', mime_type: 'image/png', model_source: 'owner' })))
    expect(await generating).toBeNull()
    expect(await saveCoverForScope(2, old, { image_base64: 'YQ==', mime_type: 'image/png', model_source: 'owner' })).toBeNull()
    expect(fetch).toHaveBeenCalledOnce()
  })

  it('普通作品刷新不使同一授权下的生成预览失效', async () => {
    vi.spyOn(projectApi, 'get').mockResolvedValue(project())
    await useProjectStore.getState().load(2)
    const scope = captureProjectScope(2)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      image_base64: 'YQ==', mime_type: 'image/png', model_source: 'owner',
    }))))
    const preview = await requestCoverForScope(2, scope, { prompt: '星空' }, 'owner', new AbortController().signal)
    if (!preview) throw new Error('同一授权下应得到封面预览')
    const upload = vi.spyOn(projectsApi, 'uploadCover').mockResolvedValue(project())
    await useProjectStore.getState().refresh()
    expect(await saveCoverForScope(2, scope, preview)).toEqual(project())
    expect(upload).toHaveBeenCalledOnce()
  })

  it('协作者显示真实来源且不会返回私人模型细节，显式本人配置不能意外回退', async () => {
    vi.spyOn(projectApi, 'get').mockResolvedValue(project())
    await useProjectStore.getState().load(2)
    const scope = captureProjectScope(2)
    const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ image_base64: 'YQ==', mime_type: 'image/png',
      model_source: 'owner', config_id: 999, provider: 'secret', model: 'private' })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ image_base64: 'YQ==', mime_type: 'image/png', model_source: 'owner' })))
    vi.stubGlobal('fetch', fetch)
    expect(await requestCoverForScope(2, scope, { prompt: '星空' }, 'owner', new AbortController().signal))
      .toEqual({ image_base64: 'YQ==', mime_type: 'image/png', model_source: 'owner' })
    await expect(requestCoverForScope(2, scope, { prompt: '星空', config_id: 2 }, 'actor', new AbortController().signal))
      .rejects.toThrow('模型来源')
    expect(fetch).toHaveBeenCalledTimes(2)
  })

  it('本人模型不可用时只发送一次本人配置，不重试或改用作者费用', async () => {
    vi.spyOn(projectApi, 'get').mockResolvedValue(project())
    await useProjectStore.getState().load(2)
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: '模型配置不可用' }), { status: 400 }))
    vi.stubGlobal('fetch', fetch)
    await expect(requestCoverForScope(2, captureProjectScope(2), { prompt: '星空', config_id: 2 },
      'actor', new AbortController().signal)).rejects.toThrow('模型配置不可用')
    expect(fetch).toHaveBeenCalledOnce()
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ prompt: '星空', config_id: 2 })
  })

  it('旧登录的响应不能进入退出后重新登录的同一账号', async () => {
    vi.spyOn(projectApi, 'get').mockResolvedValue(project())
    await useProjectStore.getState().load(2)
    const scope = captureProjectScope(2)
    const pending = deferred<Response>()
    const fetch = vi.fn(() => pending.promise)
    vi.stubGlobal('fetch', fetch)
    const generating = requestCoverForScope(2, scope, { prompt: '星空' }, 'owner', new AbortController().signal)
    useAuthStore.getState().clear()
    useAuthStore.setState({ status: 'authenticated', user })
    await useProjectStore.getState().load(2)
    pending.resolve(new Response(JSON.stringify({ image_base64: 'YQ==', mime_type: 'image/png', model_source: 'owner' })))
    expect(await generating).toBeNull()
    expect(await saveCoverForScope(2, scope, { image_base64: 'YQ==', mime_type: 'image/png' })).toBeNull()
    expect(fetch).toHaveBeenCalledOnce()
  })

  it('上传进行中授权变化时丢弃旧响应，也不再次上传', async () => {
    vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project()).mockResolvedValueOnce(project({ ...editor, authorization_version: 'grant-2' }))
    await useProjectStore.getState().load(2)
    const scope = captureProjectScope(2)
    const pending = deferred<Project>()
    const upload = vi.spyOn(projectsApi, 'uploadCover').mockReturnValue(pending.promise)
    const saving = saveCoverForScope(2, scope, { image_base64: 'YQ==', mime_type: 'image/png' })
    await useProjectStore.getState().refresh()
    pending.resolve(project())
    expect(await saving).toBeNull()
    expect(upload).toHaveBeenCalledOnce()
    expect(useProjectStore.getState().project?.access.authorization_version).toBe('grant-2')
  })

  it('协作者无实际来源时失败关闭，旧作者响应缺少来源时保持兼容', async () => {
    const owner = project({ ...editor, role: 'owner', is_collaborator: false, authorization_version: null })
    owner.owner = { id: user.id, username: user.username }
    vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project()).mockResolvedValueOnce(owner)
    const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ image_base64: 'YQ==', mime_type: 'image/png' })))
    vi.stubGlobal('fetch', fetch)
    await useProjectStore.getState().load(2)
    await expect(requestCoverForScope(2, captureProjectScope(2), { prompt: '星空', config_id: 2 },
      'actor', new AbortController().signal)).rejects.toThrow('模型来源')
    await useProjectStore.getState().load(2)
    expect(await requestCoverForScope(2, captureProjectScope(2), { prompt: '星空', config_id: 2 },
      'actor', new AbortController().signal)).toEqual({ image_base64: 'YQ==', mime_type: 'image/png' })
  })
})
