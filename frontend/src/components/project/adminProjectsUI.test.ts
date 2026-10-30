import { App } from 'antd'
import { createElement, type ReactElement } from 'react'
import { renderToString } from 'react-dom/server'
import { Route, Routes } from 'react-router-dom'
import { StaticRouter } from 'react-router-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Project, User } from '../../api/types'
import AppLayout from '../AppLayout'
import ProjectLayout from '../../pages/project/ProjectLayout'
import WorldPanel from '../../pages/project/WorldPanel'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'

// Zustand 的服务端快照固定为初始化状态；此测试用真实 store 当前状态作为静态渲染输入。
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

const admin: User = { id: 1, username: 'root', is_admin: true, created_at: '' }
const project: Project = { id: 9, owner: { id: 2, username: 'alice' }, title: '星河', genre: '', premise: '', status: 'writing', created_at: '', updated_at: '', chapter_count: 0, total_words: 0, cover_updated_at: null, auto_analyze: true, wizard_progress: { done: [], skipped: [], current: null }, default_style_id: null, default_style: null, synopsis: '', theme: '', tags: [], selling_points: '', audience: '', target_words: null, last_edited_chapter_id: null, reading: null }

function render(element: ReactElement, path = '/projects/9/world') {
  return renderToString(createElement(App, null, createElement(StaticRouter, { location: path }, createElement(Routes, null, createElement(Route, { path: '/projects/:pid/*', element }))))).replaceAll('<!-- -->', '')
}

afterEach(() => {
  useProjectStore.getState().reset()
  useAuthStore.setState({ user: null, status: 'anonymous' })
})

describe('管理员项目界面', () => {
  it('他人项目持续显示归属和返回管理入口，人工世界观仍可新增，AI入口不挂载', () => {
    useAuthStore.setState({ user: admin, status: 'authenticated' })
    useProjectStore.setState({ project })
    const shell = render(createElement(ProjectLayout))
    expect(shell).toContain('管理员正在管理「alice」的项目')
    expect(shell).toContain('href="/admin/projects"')
    expect(shell).not.toContain('项目智能体')
    const world = render(createElement(WorldPanel))
    expect(world).toContain('新增条目')
    expect(world).not.toContain('AI 重新生成')
  })

  it('管理员自己的项目继续显示智能体和正常AI写作入口', () => {
    useAuthStore.setState({ user: admin, status: 'authenticated' })
    useProjectStore.setState({ project: { ...project, owner: { id: admin.id, username: admin.username } } })
    expect(render(createElement(ProjectLayout))).toContain('项目智能体')
    expect(render(createElement(WorldPanel))).toContain('AI 重新生成')
  })

  it('全局管理入口仅对管理员展示', () => {
    const global = () => renderToString(createElement(App, null, createElement(StaticRouter, { location: '/projects' }, createElement(AppLayout))))
    useAuthStore.setState({ user: admin, status: 'authenticated' })
    expect(global()).toContain('href="/admin/projects"')
    useAuthStore.setState({ user: { ...admin, id: 2, is_admin: false } })
    expect(global()).not.toContain('href="/admin/projects"')
  })
})
