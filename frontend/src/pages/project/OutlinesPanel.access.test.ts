import { App } from 'antd'
import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import { Route, Routes } from 'react-router-dom'
import { StaticRouter } from 'react-router-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as projects from '../../api/projects'
import type { Project, ProjectAccess, User } from '../../api/types'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'
import OutlinesPanel from './OutlinesPanel'

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
const none: ProjectAccess = {
  role: null, is_collaborator: false, authorization_version: null, allow_owner_ai: false, can_read: false, can_edit_content: false,
  can_export: false, can_use_ai: false, can_manage_collaborators: false, can_recycle: false, can_backup: false,
  can_write_reading_progress: false,
}
const viewer: ProjectAccess = { ...none, role: 'viewer', is_collaborator: true, authorization_version: 'grant-1', can_read: true, can_export: true }
const editor: ProjectAccess = { ...viewer, role: 'editor', can_edit_content: true, can_use_ai: true }
const admin: ProjectAccess = { ...none, role: 'admin', can_read: true, can_edit_content: true }
const owner: ProjectAccess = { ...none, role: 'owner', can_read: true, can_edit_content: true, can_export: true, can_use_ai: true,
  can_manage_collaborators: true, can_recycle: true, can_backup: true, can_write_reading_progress: true }
const project = (snapshot: ProjectAccess): Project => ({
  id: 9, owner: { id: snapshot.role === 'owner' ? 1 : 2, username: '作者' }, access: snapshot, title: '星河', genre: '', premise: '',
  status: 'writing', created_at: '', updated_at: '', chapter_count: 0, total_words: 0, cover_updated_at: null,
  auto_analyze: true, wizard_progress: { done: [], skipped: [], current: null }, default_style_id: null,
  default_style: null, synopsis: '', theme: '', tags: [], selling_points: '', audience: '', target_words: null,
  last_edited_chapter_id: null, reading: null,
})

async function render(snapshot: ProjectAccess) {
  useAuthStore.setState({ status: 'authenticated', user: actor })
  vi.spyOn(projects.projectApi, 'get').mockResolvedValue(project(snapshot))
  await useProjectStore.getState().load(9)
  return renderToString(createElement(App, null, createElement(StaticRouter, { location: '/projects/9/outlines' },
    createElement(Routes, null, createElement(Route, { path: '/projects/:pid/outlines', element: createElement(OutlinesPanel) })))))
}

afterEach(() => {
  useProjectStore.getState().reset()
  useAuthStore.getState().clear()
  vi.restoreAllMocks()
})

// 以按钮图标判断（空态文案也会提到「导入」）
const ICON = { 导入: 'aria-label="upload"', 导出: 'aria-label="download"' } as const
const has = (html: string, label: keyof typeof ICON) => html.includes(ICON[label])

describe('大纲导入 / 导出按编辑 / 导出权限显示，与 AI 授权无关', () => {
  it('查看者：只有导出，没有导入与 AI', async () => {
    const html = await render(viewer)
    expect(has(html, '导出')).toBe(true)
    expect(has(html, '导入')).toBe(false)
    expect(html).not.toContain('AI 续写')
  })

  it('编辑者与作者：导入、导出、AI 续写都显示', async () => {
    for (const snapshot of [editor, owner]) {
      const html = await render(snapshot)
      expect(has(html, '导出')).toBe(true)
      expect(has(html, '导入')).toBe(true)
      expect(html).toContain('AI 续写')
      useProjectStore.getState().reset()
    }
  })

  it('只有人工编辑权限的管理员：可导入，不显示 AI 与导出', async () => {
    const html = await render(admin)
    expect(has(html, '导入')).toBe(true)
    expect(has(html, '导出')).toBe(false)
    expect(html).not.toContain('AI 续写')
  })
})
