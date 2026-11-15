import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { StaticRouter } from 'react-router-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Project, ProjectAccess } from '../api/types'
import ProjectListPage from './ProjectListPage'

const state = vi.hoisted(() => ({
  projects: [] as Project[], continued: null as Project | null, onChanged: null as (() => void) | null, reload: vi.fn(),
}))
vi.mock('../hooks/useLoader', () => ({
  useLoader: () => ({ data: state.projects, loading: false, error: null, reload: state.reload }),
}))
vi.mock('../components/collaboration/Invitations', () => ({
  default: ({ onChanged }: { onChanged: () => void }) => {
    state.onChanged = onChanged
    return createElement('section', null, '待处理邀请')
  },
}))
vi.mock('../components/shelf/ContinueWriting', () => ({
  default: ({ project }: { project: Project }) => {
    state.continued = project
    return createElement('span', null, `继续写作：${project.title}`)
  },
}))
vi.mock('../components/shelf/CreateProjectModal', () => ({ default: () => null }))

const access: ProjectAccess = {
  role: 'viewer', is_collaborator: true, authorization_version: 'grant-1', allow_owner_ai: false,
  can_read: true, can_edit_content: false,
  can_export: true, can_use_ai: false, can_manage_collaborators: false, can_recycle: false, can_backup: false,
  can_write_reading_progress: false,
}
const project: Project = {
  id: 1, access, owner: { id: 8, username: '作者甲' }, title: '共享试读', genre: '', premise: '', status: 'writing',
  created_at: '2026-01-01T00:00:00Z', updated_at: '2026-09-01T00:00:00Z', chapter_count: 1, total_words: 20,
  cover_updated_at: null, auto_analyze: true, wizard_progress: { done: [], skipped: [], current: null },
  default_style_id: null, default_style: null, synopsis: '', theme: '', tags: [], selling_points: '', audience: '',
  target_words: null, last_edited_chapter_id: 11, reading: null,
}

beforeEach(() => { state.projects = []; state.continued = null; state.onChanged = null; state.reload.mockReset() })

describe('作品书架的协作入口', () => {
  it('展示待处理邀请与共享身份，只从可编辑作品选择继续创作', () => {
    state.projects = [project, {
      ...project, id: 2, title: '我的作品', updated_at: '2026-08-01T00:00:00Z', owner: { id: 2, username: '作者乙' },
      access: { ...access, role: 'owner', is_collaborator: false, authorization_version: null,
        can_edit_content: true, can_backup: true },
    }]
    const html = renderToStaticMarkup(createElement(StaticRouter, { location: '/projects' }, createElement(ProjectListPage)))
    expect(html).toContain('待处理邀请')
    expect(html).toContain('共享试读')
    expect(html).toContain('查看者')
    expect(html).toContain('作者甲')
    expect(state.continued?.id).toBe(2)
    state.onChanged?.()
    expect(state.reload).toHaveBeenCalledOnce()
  })

  it('不完整或没有读取能力的项目不会显示在书架与统计中', () => {
    state.projects = [project, {
      ...project, id: 2, title: '旧授权作品', access: { ...access, authorization_version: undefined } as unknown as ProjectAccess,
    }, {
      ...project, id: 3, title: '不可读作品', access: { ...access, can_read: false, can_edit_content: true },
    }]
    const html = renderToStaticMarkup(createElement(StaticRouter, { location: '/projects' }, createElement(ProjectListPage)))
    expect(html).toContain('共享试读')
    expect(html).not.toContain('旧授权作品')
    expect(html).not.toContain('不可读作品')
    expect(html).toContain('1 部作品')
    expect(state.continued).toBeNull()
  })
})
