import { App, type UploadProps } from 'antd'
import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import { Route, Routes } from 'react-router-dom'
import { StaticRouter } from 'react-router-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as projects from '../../api/projects'
import type { Project, ProjectAccess, User } from '../../api/types'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'
import { deferred } from '../../components/systemSettings/systemSettingsTestFixtures'
import AppLayout from '../../components/AppLayout'
import ProjectLayout from './ProjectLayout'
import OverviewPanel from './OverviewPanel'
import CoverPanel from '../../components/project/CoverPanel'

const view = vi.hoisted(() => ({ beforeUpload: undefined as UploadProps['beforeUpload'] }))
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
vi.mock('antd', async (original) => {
  const actual = await original<typeof import('antd')>()
  const upload = (props: UploadProps) => {
    view.beforeUpload = props.beforeUpload
    return createElement(actual.Upload, props)
  }
  return { ...actual, Upload: Object.assign(upload, { LIST_IGNORE: actual.Upload.LIST_IGNORE, Dragger: actual.Upload.Dragger }) }
})
vi.mock('../../components/agent/AgentDock', () => ({ default: () => createElement('div', { 'data-testid': 'agent-dock' }) }))
vi.mock('../../components/TaskPanel', () => ({ default: () => createElement('div', { 'data-testid': 'task-panel' }) }))
vi.mock('../../components/collaboration/MemberPanel', () => ({ default: () => createElement('div', { 'data-testid': 'member-panel' }) }))

const actor = (id: number, admin = false): User => ({ id, username: `用户${id}`, is_admin: admin, is_super_admin: admin,
  effective_permissions: [], authorization_revision: 1, roles: [], created_at: '' })
const access: ProjectAccess = {
  role: 'viewer', is_collaborator: true, authorization_version: 'grant-1', allow_owner_ai: false, can_read: true, can_edit_content: false,
  can_export: true, can_use_ai: false, can_manage_collaborators: false, can_recycle: false, can_backup: false,
  can_write_reading_progress: false,
}
const project = (snapshot: ProjectAccess): Project => ({
  id: 9, owner: { id: 2, username: '作者' }, access: snapshot, title: '星河', genre: '', premise: '',
  status: 'writing', created_at: '', updated_at: '', chapter_count: 0, total_words: 0, cover_updated_at: null,
  auto_analyze: true, wizard_progress: { done: [], skipped: [], current: null }, default_style_id: null,
  default_style: null, synopsis: '', theme: '', tags: [], selling_points: '', audience: '', target_words: null,
  last_edited_chapter_id: null, reading: null,
})

async function load(current: Project, user: User) {
  useAuthStore.setState({ status: 'authenticated', user })
  vi.spyOn(projects.projectApi, 'get').mockResolvedValue(current)
  await useProjectStore.getState().load(current.id)
}

function renderWorkspace() {
  return renderToString(createElement(App, null, createElement(StaticRouter, { location: '/projects/9/overview' },
    createElement(Routes, null,
      createElement(Route, { path: '/projects/:pid/*', element: createElement(AppLayout) },
        createElement(Route, { element: createElement(ProjectLayout) },
          createElement(Route, { path: 'overview', element: createElement(OverviewPanel) })),
      ),
    ),
  )))
}

afterEach(() => {
  useProjectStore.getState().reset()
  useAuthStore.getState().clear()
  view.beforeUpload = undefined
  vi.restoreAllMocks()
})

describe('共享作品的工作区能力', () => {
  it('viewer 从书架返回，可导出、可看本人任务面板与只读智能体，但不显示备份、协作者或其他 AI', async () => {
    await load(project(access), actor(3))
    const html = renderWorkspace()
    expect(html).toContain('当前为查看者')
    expect(html).toContain('全部作品')
    expect(html).toContain('href="/projects"')
    expect(html).toContain('导出 TXT / Markdown / Word / EPUB')
    // 降为查看者后仍可读取本人已结束的任务记录（后端只返回本人任务）
    expect(html).toContain('data-testid="task-panel"')
    // 裁决 4：查看者可只读查看本人智能体会话（面板内不能新建 / 发送）。
    expect(html).toContain('项目智能体')
    for (const text of ['备份项目', '删除项目', '协作者</button>', '创作向导', '章节分析', '管理员正在管理']) {
      expect(html).not.toContain(text)
    }
  })

  it('editor 可用已开放的 AI（向导、章节分析、智能体）与任务面板，灵感入口仍关闭', async () => {
    await load(project({ ...access, role: 'editor', can_edit_content: true, can_use_ai: true }), actor(3))
    const html = renderWorkspace()
    expect(html).toContain('data-testid="task-panel"')
    expect(html).toContain('创作向导')
    expect(html).toContain('章节分析')
    expect(html).toContain('由作者管理')
    expect(html).toContain('项目智能体')
    for (const text of ['用灵感模式补全', '备份项目', '删除项目', '协作者</button>']) {
      expect(html).not.toContain(text)
    }
  })

  it('管理员兼 viewer 的人工编辑能力有效，仍走书架且不挂载可写 AI；智能体只读本人会话；任务面板只列本人任务', async () => {
    await load(project({ ...access, can_edit_content: true, can_recycle: true }), actor(3, true))
    const html = renderWorkspace()
    expect(html).toContain('全部作品')
    expect(html).toContain('删除项目')
    expect(html).not.toContain('当前为查看者')
    expect(html).not.toContain('管理员正在管理')
    expect(html).toContain('data-testid="task-panel"')
    expect(html).toContain('项目智能体')
    expect(html).not.toContain('创作向导')
    expect(html).toContain('模型与创作资源')
  })

  it('仅管理员保留管理入口，作者保留私有功能与成员管理', async () => {
    const admin = { ...access, role: 'admin' as const, is_collaborator: false, authorization_version: null, can_edit_content: true, can_export: false }
    await load(project(admin), actor(3, true))
    let html = renderWorkspace()
    expect(html).toContain('管理员正在管理')
    expect(html).toContain('href="/admin/projects"')
    expect(html).not.toContain('导出 TXT / Markdown / Word / EPUB')
    expect(html).not.toContain('data-testid="task-panel"')
    // 管理员人工 projects.edit 不并入智能体。
    expect(html).not.toContain('项目智能体')
    const owner = { ...access, role: 'owner' as const, is_collaborator: false, authorization_version: null, can_edit_content: true,
      can_use_ai: true, can_manage_collaborators: true, can_recycle: true, can_backup: true, can_write_reading_progress: true }
    await load(project(owner), actor(2))
    html = renderWorkspace()
    expect(html).toContain('项目智能体')
    expect(html).toContain('协作者')
    expect(html).toContain('备份项目')
    expect(html).toContain('data-testid="task-panel"')
  })
})

describe('封面上传的请求作用域', () => {
  it('查看者即使调用旧上传回调，也不会发送写入请求', async () => {
    const shared = project(access)
    await load(shared, actor(3))
    const upload = vi.spyOn(projects, 'uploadCover')
    renderToString(createElement(App, null, createElement(CoverPanel, { project: shared })))
    const file = new File(['图片'], 'cover.png', { type: 'image/png' })
    Reflect.apply(view.beforeUpload!, undefined, [file, [file]])
    expect(upload).not.toHaveBeenCalled()
  })

  it('同账号退出并重新打开同一作品后，迟到封面响应不能覆盖新会话', async () => {
    const owner = { ...access, role: 'owner' as const, is_collaborator: false, authorization_version: null, can_edit_content: true,
      can_use_ai: true, can_manage_collaborators: true, can_recycle: true, can_backup: true, can_write_reading_progress: true }
    const original = project(owner)
    await load(original, actor(2))
    const pending = deferred<Project>()
    const upload = vi.spyOn(projects, 'uploadCover').mockReturnValue(pending.promise)
    renderToString(createElement(App, null, createElement(CoverPanel, { project: original })))
    expect(view.beforeUpload).toBeDefined()
    const file = new File(['图片'], 'cover.png', { type: 'image/png' })
    Reflect.apply(view.beforeUpload!, undefined, [file, [file]])
    expect(upload).toHaveBeenCalledOnce()
    useAuthStore.getState().clear()
    const fresh = { ...original, title: '新会话标题', cover_updated_at: '2026-11-09T10:00:00Z' }
    await load(fresh, actor(2))
    pending.resolve({ ...original, cover_updated_at: '2026-11-09T09:00:00Z' })
    await pending.promise
    await Promise.resolve()
    expect(useProjectStore.getState().project).toMatchObject({ title: '新会话标题', cover_updated_at: '2026-11-09T10:00:00Z' })
  })
})
