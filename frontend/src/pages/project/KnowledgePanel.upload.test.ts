import { App } from 'antd'
import { createElement, type ReactNode } from 'react'
import { renderToString } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { kbApi } from '../../api/kb'
import { projectApi } from '../../api/projects'
import type { KbDocument, Project, ProjectAccess, User } from '../../api/types'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'
import KnowledgePanel from './KnowledgePanel'

const harness = vi.hoisted(() => ({ beforeUpload: undefined as ((file: File) => boolean) | undefined, track: vi.fn(),
  status: undefined as import('../../api/types').IndexStatus | undefined }))
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
vi.mock('../../stores/tasks', () => ({ useTasksStore: (selector: (state: { track: typeof harness.track }) => unknown) => selector({ track: harness.track }) }))
vi.mock('../../hooks/useProjectId', () => ({ useProjectId: () => 2 }))
vi.mock('../../hooks/useLoader', () => ({ useLoader: () => ({ data: undefined, loading: false, error: null, setData: () => {} }) }))
vi.mock('../../hooks/useIndexStatus', () => ({ useIndexStatus: () => ({ status: harness.status, refresh: () => {}, version: 0 }) }))
vi.mock('../../components/kb/IndexStatusBanner', () => ({ default: () => null }))
vi.mock('../../components/kb/KbChunksDrawer', () => ({ default: () => null }))
vi.mock('../../components/kb/KbSearchDebug', () => ({ default: () => null }))
vi.mock('../../components/kb/RetrievalSettingsCard', () => ({ default: () => null }))
vi.mock('../../components/mentions/SourceMentionFocus', () => ({ default: () => null }))
vi.mock('antd', async (original) => {
  const actual = await original<typeof import('antd')>()
  return { ...actual, Upload: { Dragger: (props: { beforeUpload: (file: File) => boolean; children: ReactNode }) => {
    harness.beforeUpload = props.beforeUpload
    return createElement('section', null, props.children)
  } } }
})

const user: User = { id: 1, username: '编辑者', created_at: '', is_admin: false, is_super_admin: false,
  effective_permissions: [], roles: [], authorization_revision: 1 }
const access: ProjectAccess = { role: 'editor', is_collaborator: true, authorization_version: 'grant-1',
  allow_owner_ai: false, can_read: true, can_edit_content: true, can_use_ai: true, can_export: true,
  can_manage_collaborators: false, can_recycle: false, can_backup: false, can_write_reading_progress: false }
function project(value = access): Project {
  return { id: 2, owner: { id: 9, username: '作者' }, access: value, title: '作品', genre: '', premise: '', status: 'planning',
    created_at: '', updated_at: '', chapter_count: 0, total_words: 0, cover_updated_at: null, auto_analyze: true,
    wizard_progress: { done: [], skipped: [], current: null }, default_style_id: null, default_style: null,
    synopsis: '', theme: '', tags: [], selling_points: '', audience: '', target_words: null, last_edited_chapter_id: null, reading: null }
}
const doc: KbDocument = { id: 5, project_id: 2, filename: '资料.md', mime: 'text/markdown', size: 4, sha256: 'sha',
  status: 'ready_keyword_only', error: '', warnings: [], char_count: 4, chunk_count: 1, inject: true, created_at: '', updated_at: '' }

async function load(value: Project) {
  vi.spyOn(projectApi, 'get').mockResolvedValue(value)
  await useProjectStore.getState().load(2)
}
function render() {
  harness.beforeUpload = undefined
  return renderToString(createElement(MemoryRouter, null, createElement(App, null, createElement(KnowledgePanel))))
}

beforeEach(() => {
  harness.status = undefined
  useProjectStore.getState().reset()
  useAuthStore.setState({ status: 'authenticated', user })
  harness.beforeUpload = undefined
  harness.track.mockReset()
})
afterEach(() => { useProjectStore.getState().reset(); vi.restoreAllMocks() })

describe('知识库上传界面', () => {
  it('editor 上传提示以 index-status 为准：未授权仅关键词并说明原因；授权且作者向量可用时说明费用由作者承担', async () => {
    await load(project())
    harness.status = { embedding_model: null, needs_rebuild: false, stale_count: 0, stale: { kb: 0, memory: 0 },
      indexing: false, owner_embedding_configured: true, retrieval_mode: 'keyword', keyword_reason: 'owner_embedding_not_allowed' }
    const keyword = render()
    expect(keyword).toContain('仅关键词检索')
    expect(keyword).toContain('作者未授权使用其模型配置')
    expect(keyword).toContain('不会产生作者向量费用')
    expect(keyword).toContain('点击或拖拽文件到这里上传')
    expect(harness.beforeUpload).toBeDefined()

    vi.mocked(projectApi.get).mockResolvedValue(project({ ...access, allow_owner_ai: true, authorization_version: 'grant-2' }))
    await useProjectStore.getState().refresh()
    harness.status = { ...harness.status, retrieval_mode: 'vector', keyword_reason: null }
    const shared = render()
    expect(shared).toContain('使用作者共享的向量模型，费用由作者承担')
    expect(shared).toContain('点击或拖拽文件到这里上传')
    expect(harness.beforeUpload).toBeDefined()
  })

  it('作者保留上传；viewer 兼管理员不能借人工权限上传', async () => {
    const owner: User = { ...user, id: 9, username: '作者' }
    useAuthStore.setState({ status: 'authenticated', user: owner })
    await load(project({ ...access, role: 'owner', is_collaborator: false, authorization_version: null }))
    expect(render()).toContain('点击或拖拽文件到这里上传')

    const admin: User = { ...user, is_admin: true, effective_permissions: ['projects.read', 'projects.edit'] }
    useAuthStore.setState({ status: 'authenticated', user: admin })
    await load(project({ ...access, role: 'viewer', can_edit_content: true, can_use_ai: true }))
    expect(render()).not.toContain('点击或拖拽文件到这里上传')
    expect(harness.beforeUpload).toBeUndefined()
  })

  it('编辑者真实上传空任务时不登记任务；旧授权下的上传回调不能发请求', async () => {
    await load(project())
    const upload = vi.spyOn(kbApi, 'upload').mockResolvedValue({ document: doc, task: null })
    vi.spyOn(kbApi, 'listDocuments').mockResolvedValue([])
    render()
    const current = harness.beforeUpload
    current?.(new File(['x'], '资料.md'))
    await vi.waitFor(() => expect(upload).toHaveBeenCalledOnce())
    expect(harness.track).not.toHaveBeenCalled()

    render()
    const old = harness.beforeUpload
    vi.mocked(projectApi.get).mockResolvedValue(project({ ...access, authorization_version: 'grant-2' }))
    await useProjectStore.getState().refresh()
    old?.(new File(['x'], '旧资料.md'))
    expect(upload).toHaveBeenCalledOnce()
  })
})
