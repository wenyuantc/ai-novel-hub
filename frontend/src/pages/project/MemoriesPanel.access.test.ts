import { App } from 'antd'
import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { projectApi } from '../../api/projects'
import type { IndexStatus, Project, ProjectAccess, User } from '../../api/types'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'
import MemoriesPanel from './MemoriesPanel'

// E6-e：记忆页协作者视图——精简索引横幅、记忆向量失败的补救说明、检索调试按服务端 index-status 开放。
const harness = vi.hoisted(() => ({
  status: undefined as IndexStatus | undefined,
  indexOptions: undefined as Record<string, unknown> | undefined,
}))
vi.mock('../../stores/auth', async (original) => {
  const actual = await original<typeof import('../../stores/auth')>()
  const hook = (selector: (state: ReturnType<typeof actual.useAuthStore.getState>) => unknown) => selector(actual.useAuthStore.getState())
  return { ...actual, useAuthStore: Object.assign(hook, actual.useAuthStore) }
})
vi.mock('../../stores/project', async (original) => {
  const actual = await original<typeof import('../../stores/project')>()
  const hook = (selector: (state: ReturnType<typeof actual.useProjectStore.getState>) => unknown) => selector(actual.useProjectStore.getState())
  return { ...actual, useProjectStore: Object.assign(hook, actual.useProjectStore) }
})
vi.mock('../../stores/tasks', () => ({ useTasksStore: (selector: (state: { track: () => void }) => unknown) => selector({ track: () => {} }) }))
vi.mock('../../hooks/useProjectId', () => ({ useProjectId: () => 2 }))
vi.mock('../../hooks/useLoader', () => ({
  useLoader: () => ({ data: undefined, loading: false, error: null, reload: () => {}, setData: () => {} }),
}))
vi.mock('../../hooks/useIndexStatus', () => ({
  useIndexStatus: (_pid: number, options: Record<string, unknown>) => {
    harness.indexOptions = options
    return { status: options.enabled ? harness.status : undefined, refresh: () => {}, version: 0 }
  },
}))
vi.mock('../../components/kb/MemoryEditModal', () => ({ default: () => null }))
vi.mock('../../components/kb/RetrievalSettingsCard', () => ({ default: () => null }))

const editorUser: User = { id: 1, username: '编辑者', created_at: '', is_admin: false, is_super_admin: false,
  effective_permissions: [], roles: [], authorization_revision: 1 }
const editor: ProjectAccess = { role: 'editor', is_collaborator: true, authorization_version: 'grant-1',
  allow_owner_ai: true, can_read: true, can_edit_content: true, can_use_ai: true, can_export: true,
  can_manage_collaborators: false, can_recycle: false, can_backup: false, can_write_reading_progress: false }
const viewer: ProjectAccess = { ...editor, role: 'viewer', can_edit_content: false, can_use_ai: false }
const project = (access: ProjectAccess): Project => ({ id: 2, owner: { id: 9, username: '作者' }, access, title: '作品',
  genre: '', premise: '', status: 'planning', created_at: '', updated_at: '', chapter_count: 0, total_words: 0,
  cover_updated_at: null, auto_analyze: true, wizard_progress: { done: [], skipped: [], current: null },
  default_style_id: null, default_style: null, synopsis: '', theme: '', tags: [], selling_points: '', audience: '',
  target_words: null, last_edited_chapter_id: null, reading: null })
const status: IndexStatus = { embedding_model: null, needs_rebuild: true, stale_count: 2, stale: { kb: 0, memory: 2 },
  indexing: false, owner_embedding_configured: true, retrieval_mode: 'vector', keyword_reason: null }

async function render(access: ProjectAccess, value: IndexStatus | undefined) {
  useAuthStore.setState({ status: 'authenticated', user: editorUser })
  vi.spyOn(projectApi, 'get').mockResolvedValue(project(access))
  await useProjectStore.getState().load(2)
  harness.status = value
  return renderToString(createElement(MemoryRouter, null, createElement(App, null, createElement(MemoriesPanel))))
}

beforeEach(() => { useProjectStore.getState().reset(); harness.indexOptions = undefined })
afterEach(() => { useProjectStore.getState().reset(); vi.restoreAllMocks() })

describe('记忆页协作者视图', () => {
  it('授权编辑者：待建向量数、记忆向量失败的补救说明、全书重建与检索调试（含费用说明）', async () => {
    const html = await render(editor, status)
    expect(html).toContain('2 条资料待建向量')
    expect(html).toContain('无法直接重试；可重新编辑此条或由有权限者全书重建')
    expect(html).toContain('<span>全书重建</span>')
    expect(html).toContain('检索调试')
    expect(html).toContain('费用由作者承担')
    expect(html).not.toContain('去模型配置')
    expect(harness.indexOptions).toMatchObject({ enabled: true, foreground: true })
  })

  it('未授权编辑者：仅关键词并说明原因，不显示检索调试与重建', async () => {
    const html = await render({ ...editor, allow_owner_ai: false },
      { ...status, retrieval_mode: 'keyword', keyword_reason: 'owner_embedding_not_allowed' })
    expect(html).toContain('当前只用关键词检索：作者未授权使用其模型配置')
    expect(html).toContain('2 条资料待建向量')
    expect(html).not.toContain('检索调试')
    expect(html).not.toContain('<span>全书重建</span>')
  })

  it('viewer 只读：不读索引状态、无横幅与检索调试', async () => {
    const html = await render(viewer, status)
    expect(harness.indexOptions).toMatchObject({ enabled: false })
    expect(html).not.toContain('待建向量')
    expect(html).not.toContain('检索调试')
  })
})
