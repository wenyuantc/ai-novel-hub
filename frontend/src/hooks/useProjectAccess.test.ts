import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { projectApi } from '../api/projects'
import type { Project, User } from '../api/types'
import { useAuthStore } from '../stores/auth'
import { useProjectStore } from '../stores/project'
import { useProjectAccess } from './useProjectAccess'

// Node 中渲染真实 hook；Zustand 的 server snapshot 改用当前测试 store。
vi.mock('../stores/auth', async (original) => {
  const actual = await original<typeof import('../stores/auth')>()
  const hook = (selector: (state: ReturnType<typeof actual.useAuthStore.getState>) => unknown) => selector(actual.useAuthStore.getState())
  return { ...actual, useAuthStore: Object.assign(hook, actual.useAuthStore) }
})
vi.mock('../stores/project', async (original) => {
  const actual = await original<typeof import('../stores/project')>()
  const hook = (selector: (state: ReturnType<typeof actual.useProjectStore.getState>) => unknown) => selector(actual.useProjectStore.getState())
  return { ...actual, useProjectStore: Object.assign(hook, actual.useProjectStore) }
})

const user: User = { id: 1, username: '查看者', is_admin: false, is_super_admin: false, effective_permissions: [],
  authorization_revision: 1, roles: [], created_at: '' }
const project: Project = {
  id: 3, owner: { id: 2, username: '作者' }, title: '作品', genre: '', premise: '', status: 'planning',
  created_at: '', updated_at: '', chapter_count: 0, total_words: 0, cover_updated_at: null, auto_analyze: true,
  wizard_progress: { done: [], skipped: [], current: null }, default_style_id: null, default_style: null,
  synopsis: '', theme: '', tags: [], selling_points: '', audience: '', target_words: null,
  last_edited_chapter_id: null, reading: null,
  access: { role: 'editor', is_collaborator: true, authorization_version: 'grant-1', allow_owner_ai: false, can_read: true, can_edit_content: true,
    can_export: true, can_use_ai: true, can_manage_collaborators: false, can_recycle: false, can_backup: false,
    can_write_reading_progress: false },
}

function renderAccess(projectId = 3) {
  let result: ReturnType<typeof useProjectAccess> | undefined
  function Probe() { result = useProjectAccess(projectId); return null }
  renderToString(createElement(Probe))
  if (!result) throw new Error('未渲染 hook')
  return result
}

beforeEach(() => {
  useProjectStore.getState().reset()
  useAuthStore.setState({ status: 'authenticated', user })
})
afterEach(() => { useProjectStore.getState().reset(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('useProjectAccess 作用域', () => {
  it('编辑者可用已开放的 AI 入口，仅作者的智能体/知识库入口仍关闭', async () => {
    vi.spyOn(projectApi, 'get').mockResolvedValue(project)
    await useProjectStore.getState().load(project.id)
    const access = renderAccess()
    expect(access).toMatchObject({ ready: true, role: 'editor', canRead: true, canEdit: true, canExport: true,
      canUseAIEntitled: true, canUseAI: true, canUseOwnerAI: false, canBackup: false, canWriteReadingProgress: false })
    expect(access.canEditNow()).toBe(true)
    expect(access.canUseAINow()).toBe(true)
    expect(access.canUseOwnerAINow()).toBe(false)
    expect(access.canUseAIEntitledNow()).toBe(true)
    // 智能体对可用 AI 的编辑者开放（裁决 1），授权轮次随能力快照提供给键控。
    expect(access).toMatchObject({ canUseAgent: true, canViewAgent: true, authorizationVersion: 'grant-1' })
    expect(access.canUseAgentNow()).toBe(true)
    expect(access.canViewAgentNow()).toBe(true)
    expect(renderAccess(4)).toMatchObject({ ready: false, canRead: false, canEdit: false })
  })

  it('返回的旧异步回调在权限降级、切账号或项目后都失效', async () => {
    const downgraded: Project = { ...project, access: { ...project.access, role: 'viewer',
      can_edit_content: false, can_use_ai: false } }
    vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project).mockResolvedValueOnce(downgraded)
    await useProjectStore.getState().load(project.id)
    const old = renderAccess()
    await useProjectStore.getState().refresh()
    expect(old.canReadNow()).toBe(false)
    expect(old.canEditNow()).toBe(false)
    const latest = renderAccess()
    expect(latest.canReadNow()).toBe(true)
    expect(latest.canEditNow()).toBe(false)
    useAuthStore.setState({ status: 'anonymous', user: null })
    expect(latest.canReadNow()).toBe(false)
    useAuthStore.setState({ status: 'authenticated', user })
    expect(latest.canReadNow()).toBe(false)
  })

  it('同权限的新邀请使旧回调失效，普通刷新保持回调有效', async () => {
    vi.spyOn(projectApi, 'get').mockResolvedValueOnce(project).mockResolvedValueOnce(project)
      .mockResolvedValueOnce({ ...project, access: { ...project.access, authorization_version: 'grant-2' } })
    await useProjectStore.getState().load(project.id)
    const old = renderAccess()
    await useProjectStore.getState().refresh()
    expect(old.canEditNow()).toBe(true)
    await useProjectStore.getState().refresh()
    expect(old.canReadNow()).toBe(false)
    expect(old.canEditNow()).toBe(false)
    expect(renderAccess().canEditNow()).toBe(true)
  })

  it('普通写请求 403 后新权限到达时，真实 hook 立即收紧按钮和 AI 能力，旧回调失效', async () => {
    const owner: Project = { ...project, owner: { id: 1, username: '作者' }, access: {
      ...project.access, role: 'owner', is_collaborator: false, authorization_version: null,
      can_manage_collaborators: true, can_recycle: true, can_backup: true, can_write_reading_progress: true,
    } }
    const restricted: Project = { ...owner, access: { ...owner.access, can_edit_content: false, can_use_ai: false } }
    const get = vi.spyOn(projectApi, 'get').mockResolvedValueOnce(owner).mockResolvedValueOnce(restricted)
    await useProjectStore.getState().load(project.id)
    const old = renderAccess()
    expect(old).toMatchObject({ canRead: true, canEdit: true, canUseAI: true })
    const fetchMock = vi.fn(async () => new Response('{"code":"project_readonly"}', { status: 403 }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(api.patch('/projects/3/characters/7', {})).rejects.toMatchObject({ status: 403 })
    await vi.waitFor(() => expect(useProjectStore.getState().project?.access).toEqual(restricted.access))
    expect(renderAccess()).toMatchObject({ canRead: true, canEdit: false, canUseAI: false })
    expect(old.canEditNow()).toBe(false)
    expect(old.canUseAINow()).toBe(false)
    expect(get).toHaveBeenCalledTimes(2)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})
