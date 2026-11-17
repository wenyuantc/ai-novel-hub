import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setUnauthorizedHandler } from '../../api/client'
import { downloadEpub } from '../../api/exports'
import { projectApi } from '../../api/projects'
import type { Project, ProjectAccess, User } from '../../api/types'
import { useAuthStore } from '../../stores/auth'
import { isProjectScopeCurrent, useProjectStore } from '../../stores/project'
import { epubAuthorOptions, epubAuthorText, prepareShelfExportScope } from './epub'

const actor: User = {
  id: 1, username: '协作者', created_at: '', is_admin: false, is_super_admin: false,
  effective_permissions: [], roles: [], authorization_revision: 1,
}
const access: ProjectAccess = {
  role: 'viewer', is_collaborator: true, authorization_version: 'invitation-1', allow_owner_ai: false,
  can_read: true, can_edit_content: false, can_export: true, can_use_ai: false,
  can_manage_collaborators: false, can_recycle: false, can_backup: false, can_write_reading_progress: false,
}
const project: Project = {
  id: 42, access, owner: { id: 2, username: '真实作者' }, pen_name: { id: null, name: '作品笔名' },
  title: '共享作品', genre: '', premise: '', status: 'planning', created_at: '', updated_at: '', chapter_count: 1,
  total_words: 5, cover_updated_at: null, auto_analyze: false, wizard_progress: { done: [], skipped: [], current: null },
  default_style_id: null, default_style: null, synopsis: '', theme: '', tags: [], selling_points: '', audience: '',
  target_words: null, last_edited_chapter_id: null, reading: null,
}

beforeEach(() => {
  useProjectStore.getState().reset()
  useAuthStore.setState({ status: 'authenticated', user: actor })
})

afterEach(() => {
  useProjectStore.getState().reset()
  useAuthStore.setState({ status: 'anonymous', user: null })
  setUnauthorizedHandler(null)
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('书架 EPUB 导出作者和授权作用域', () => {
  it('未编辑时显示作品署名但不发送 author；显式修改和清空保留用户意图', async () => {
    expect(epubAuthorText('作品笔名', null)).toBe('作品笔名')
    expect(epubAuthorText('更换的笔名', '作者草稿')).toBe('作者草稿')
    const urls: URL[] = []
    vi.stubGlobal('fetch', async (input: string) => {
      urls.push(new URL(input, 'http://localhost'))
      return new Response('epub', { headers: { 'Content-Disposition': 'attachment; filename="novel.epub"' } })
    })
    const save = vi.fn()
    for (const draft of [null, ' 自定义作者 ', ''] as const) {
      await downloadEpub(42, { ...epubAuthorOptions(draft), includeSynopsis: true }, { isCurrent: () => true, save })
    }
    expect(urls.map((url) => url.pathname)).toEqual(Array(3).fill('/api/v1/projects/42/export'))
    expect(urls[0].searchParams.has('author')).toBe(false)
    expect(urls[1].searchParams.get('author')).toBe('自定义作者')
    expect(urls[2].searchParams.has('author')).toBe(true)
    expect(urls[2].searchParams.get('author')).toBe('')
    expect(save).toHaveBeenCalledTimes(3)
  })

  it('书架入口先加载所选作品的真实授权，再允许下载；离页和切账号使旧作用域失效', async () => {
    vi.spyOn(projectApi, 'get').mockResolvedValue(project)
    let open = true
    const controller = new AbortController()
    const scope = await prepareShelfExportScope(42, { signal: controller.signal, isCurrent: () => open })
    expect(scope).not.toBeNull()
    expect(isProjectScopeCurrent(scope, 'canExport')).toBe(true)
    let requested = ''
    vi.stubGlobal('fetch', async (input: string) => {
      requested = input
      return new Response('epub')
    })
    const save = vi.fn()
    const guard = () => open && isProjectScopeCurrent(scope, 'canExport')
    await downloadEpub(42, epubAuthorOptions(null), { isCurrent: guard, save })
    expect(new URL(requested, 'http://localhost').searchParams.has('author')).toBe(false)
    expect(save).toHaveBeenCalledOnce()
    open = false
    await downloadEpub(42, {}, { isCurrent: guard, save })
    expect(save).toHaveBeenCalledOnce()
    open = true
    useAuthStore.setState({ user: { ...actor, id: 9 } })
    expect(guard()).toBe(false)
  })

  it('加载中关闭书架弹窗时迟到结果不会写回作品状态', async () => {
    let release!: (value: Project) => void
    const response = new Promise<Project>((resolve) => { release = resolve })
    vi.spyOn(projectApi, 'get').mockReturnValue(response)
    let open = true
    const controller = new AbortController()
    const pending = prepareShelfExportScope(42, { signal: controller.signal, isCurrent: () => open })
    open = false
    controller.abort()
    release(project)
    expect(await pending).toBeNull()
    expect(useProjectStore.getState().project).toBeNull()
  })

  it('同作品的授权重读失败时不复用旧缓存开启导出', async () => {
    vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project).mockRejectedValueOnce(new Error('网络故障'))
    expect(await prepareShelfExportScope(42, { isCurrent: () => true })).not.toBeNull()
    expect(await prepareShelfExportScope(42, { isCurrent: () => true })).toBeNull()
    expect(useProjectStore.getState().error).toBe('网络故障')
  })

  it('书架弹窗关闭后迟到的 401 不能退出当前会话', async () => {
    let release!: (response: Response) => void
    const response = new Promise<Response>((resolve) => { release = resolve })
    vi.stubGlobal('fetch', () => response)
    const logout = vi.fn()
    setUnauthorizedHandler(logout)
    let open = true
    const controller = new AbortController()
    const pending = prepareShelfExportScope(42, { signal: controller.signal, isCurrent: () => open })
    open = false
    controller.abort()
    release(new Response('{"detail":"未登录"}', { status: 401 }))
    expect(await pending).toBeNull()
    expect(logout).not.toHaveBeenCalled()
    expect(useProjectStore.getState().project).toBeNull()
  })

  it('导出权限刷新后，已捕获的书架作用域不能继续保存下载', async () => {
    const get = vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project).mockResolvedValueOnce({
      ...project, access: { ...project.access, can_export: false },
    })
    const scope = await prepareShelfExportScope(42, { isCurrent: () => true })
    expect(isProjectScopeCurrent(scope, 'canExport')).toBe(true)
    expect(await useProjectStore.getState().verifyAccess()).toBe('confirmed')
    expect(get).toHaveBeenCalledTimes(2)
    const save = vi.fn()
    await downloadEpub(42, {}, { isCurrent: () => isProjectScopeCurrent(scope, 'canExport'), save })
    expect(save).not.toHaveBeenCalled()
  })
})
