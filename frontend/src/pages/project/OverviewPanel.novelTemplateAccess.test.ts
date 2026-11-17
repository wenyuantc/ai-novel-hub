import { App } from 'antd'
import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import { StaticRouter } from 'react-router-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as projects from '../../api/projects'
import type { Project, ProjectAccess, User } from '../../api/types'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'
import OverviewPanel from './OverviewPanel'

vi.mock('../../stores/auth', async (original) => {
  const actual = await original<typeof import('../../stores/auth')>()
  const hook = (selector: (state: ReturnType<typeof actual.useAuthStore.getState>) => unknown) => selector(actual.useAuthStore.getState())
  return { ...actual, useAuthStore: Object.assign(hook, actual.useAuthStore) }
})
vi.mock('../../stores/project', async (original) => {
  const actual = await original<typeof import('../../stores/project')>()
  const hook = (selector?: (state: ReturnType<typeof actual.useProjectStore.getState>) => unknown) => selector ? selector(actual.useProjectStore.getState()) : actual.useProjectStore.getState()
  return { ...actual, useProjectStore: Object.assign(hook, actual.useProjectStore) }
})

const actor: User = { id: 1, username: '用户1', is_admin: false, is_super_admin: false,
  effective_permissions: [], authorization_revision: 1, roles: [], created_at: '' }
const base: ProjectAccess = { role: null, is_collaborator: false, authorization_version: null, allow_owner_ai: false,
  can_read: true, can_edit_content: true, can_export: false, can_use_ai: false, can_manage_collaborators: false,
  can_recycle: false, can_backup: false, can_write_reading_progress: false }
const project = (access: ProjectAccess): Project => ({
  id: 9, owner: { id: access.role === 'owner' ? 1 : 2, username: '作者' }, access, title: '星河', genre: '', premise: '',
  status: 'writing', created_at: '', updated_at: '', chapter_count: 0, total_words: 0, cover_updated_at: null,
  auto_analyze: false, wizard_progress: { done: [], skipped: [], current: null }, default_style_id: null,
  default_style: null, synopsis: '', theme: '', tags: [], selling_points: '', audience: '', target_words: null,
  last_edited_chapter_id: null, reading: null,
})

async function render(access: ProjectAccess, user: User = actor) {
  useAuthStore.setState({ status: 'authenticated', user })
  vi.spyOn(projects.projectApi, 'get').mockResolvedValue(project(access))
  await useProjectStore.getState().load(9)
  return renderToString(createElement(App, null, createElement(StaticRouter, { location: '/projects/9' }, createElement(OverviewPanel))))
}

afterEach(() => {
  useProjectStore.getState().reset()
  useAuthStore.getState().clear()
  vi.restoreAllMocks()
})

describe('作品概览的个人模板入口', () => {
  it('只对本作品作者显示，不对协作者或代管管理员显示', async () => {
    expect(await render({ ...base, role: 'owner' })).toContain('保存为小说模板')
    const cases: Array<[ProjectAccess, User]> = [
      [{ ...base, role: 'editor' as const, is_collaborator: true, authorization_version: 'grant-1' }, actor],
      [{ ...base, role: 'viewer' as const, is_collaborator: true, authorization_version: 'grant-2', can_edit_content: false }, actor],
      [{ ...base, role: 'admin' as const }, { ...actor, is_admin: true, effective_permissions: ['projects.read', 'projects.edit'] }],
    ]
    for (const [access, user] of cases) {
      useProjectStore.getState().reset()
      vi.restoreAllMocks()
      const html = await render(access, user)
      expect(html).toContain('作品署名')
      expect(html).not.toContain('保存为小说模板')
    }
  })
})
