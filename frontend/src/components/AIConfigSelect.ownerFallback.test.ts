import { App } from 'antd'
import { createElement, type DependencyList, type EffectCallback, type ReactElement } from 'react'
import { renderToString } from 'react-dom/server'
import { Route, Routes } from 'react-router-dom'
import { StaticRouter } from 'react-router-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AIConfig, Project, ProjectAccess, User } from '../api/types'
import { useAIConfigStore } from '../stores/aiConfigs'
import { useAuthStore } from '../stores/auth'
import { useProjectStore } from '../stores/project'
import AIConfigSelect from './AIConfigSelect'
import WizardGenerator from './WizardGenerator'

// SSR 不执行 effect：记录下来由测试显式运行真实闭包。
const effects = vi.hoisted(() => [] as EffectCallback[])
vi.mock('react', async (original) => {
  const actual = await original<typeof import('react')>()
  return { ...actual, useEffect: (setup: EffectCallback, deps?: DependencyList) => { effects.push(setup); actual.useEffect(setup, deps) } }
})

// SSR 下 zustand 取初始快照；改为读取当前状态，才能按测试设置的作品与配置渲染。
vi.mock('../stores/auth', async (original) => {
  const actual = await original<typeof import('../stores/auth')>()
  const hook = (selector: (state: ReturnType<typeof actual.useAuthStore.getState>) => unknown) => selector(actual.useAuthStore.getState())
  return { ...actual, useAuthStore: Object.assign(hook, actual.useAuthStore) }
})
vi.mock('../stores/project', async (original) => {
  const actual = await original<typeof import('../stores/project')>()
  const hook = (selector?: (state: ReturnType<typeof actual.useProjectStore.getState>) => unknown) => selector ? selector(actual.useProjectStore.getState()) : actual.useProjectStore.getState()
  return { ...actual, useProjectStore: Object.assign(hook, actual.useProjectStore) }
})
vi.mock('../stores/aiConfigs', async (original) => {
  const actual = await original<typeof import('../stores/aiConfigs')>()
  const hook = (selector?: (state: ReturnType<typeof actual.useAIConfigStore.getState>) => unknown) => selector ? selector(actual.useAIConfigStore.getState()) : actual.useAIConfigStore.getState()
  return { ...actual, useAIConfigStore: Object.assign(hook, actual.useAIConfigStore) }
})
vi.mock('../hooks/useTaskStream', () => ({ useTaskStream: () => ({
  running: false, task: null, text: '', error: null, reconnecting: false, follow: vi.fn(), cancel: async () => {},
}) }))

const actor: User = { id: 1, username: '用户1', is_admin: false, is_super_admin: false,
  effective_permissions: [], authorization_revision: 1, roles: [], created_at: '' }
const none: ProjectAccess = {
  role: null, is_collaborator: false, authorization_version: null, allow_owner_ai: false, can_read: false, can_edit_content: false,
  can_export: false, can_use_ai: false, can_manage_collaborators: false, can_recycle: false, can_backup: false,
  can_write_reading_progress: false,
}
const editor: ProjectAccess = { ...none, role: 'editor', is_collaborator: true, authorization_version: 'grant-1', can_read: true,
  can_export: true, can_edit_content: true, can_use_ai: true }
const granted: ProjectAccess = { ...editor, allow_owner_ai: true }
const owner: ProjectAccess = { ...none, role: 'owner', can_read: true, can_edit_content: true, can_export: true, can_use_ai: true,
  can_manage_collaborators: true, can_recycle: true, can_backup: true, can_write_reading_progress: true }
const project = (snapshot: ProjectAccess): Project => ({
  id: 9, owner: { id: snapshot.role === 'owner' ? 1 : 2, username: '作者' }, access: snapshot, title: '星河', genre: '', premise: '',
  status: 'writing', created_at: '', updated_at: '', chapter_count: 0, total_words: 0, cover_updated_at: null,
  auto_analyze: true, wizard_progress: { done: [], skipped: [], current: null }, default_style_id: null,
  default_style: null, synopsis: '', theme: '', tags: [], selling_points: '', audience: '', target_words: null,
  last_edited_chapter_id: null, reading: null,
})
const textConfig = { id: 5, name: '本人文本', provider: 'openai', model: 'gpt', model_type: 'text', is_default: true } as unknown as AIConfig

function setup(snapshot: ProjectAccess, configs: AIConfig[]) {
  useAuthStore.setState({ status: 'authenticated', user: actor })
  useProjectStore.setState({ project: project(snapshot) })
  useAIConfigStore.setState({ configs, loaded: true, loading: false, load: async () => {} })
}

function render(location: string, path: string, element: ReactElement) {
  return renderToString(createElement(App, null, createElement(StaticRouter, { location },
    createElement(Routes, null, createElement(Route, { path, element })))))
}

const select = (location = '/projects/9/chapters') => render(location, location.startsWith('/projects') ? '/projects/:pid/chapters' : '/styles',
  createElement(AIConfigSelect))
const generateButton = (html: string) => {
  const match = html.match(/<button[^>]*>(?:(?!<\/button>).)*AI 生成角色(?:(?!<\/button>).)*<\/button>/s)
  expect(match).not.toBeNull()
  return match![0]
}
const wizard = () => render('/projects/9/wizard', '/projects/:pid/wizard',
  createElement(WizardGenerator, { projectId: 9, kind: 'characters', onDone: () => {} }))

afterEach(() => {
  useProjectStore.setState({ project: null })
  useAuthStore.getState().clear()
  useAIConfigStore.setState({ configs: [], loaded: false, loading: false })
  vi.restoreAllMocks()
})

describe('AIConfigSelect 作者模型回退', () => {
  it('编辑者无本人模型且作者已授权：不显示警告，显示作者费用说明', () => {
    setup(granted, [])
    const html = select()
    expect(html).not.toContain('尚未配置')
    expect(html).toContain('作者授权的默认文本模型')
    expect(html).toContain('作者费用')
  })

  it('独立阅读器复用自己的已核验作品快照判定回退，不沿用工作区的作品授权', () => {
    setup(editor, [])
    const html = render('/projects/9/read/5', '/projects/:pid/read/:cid',
      createElement(AIConfigSelect, { projectSnapshot: project(granted) }))
    expect(html).toContain('作者授权的默认文本模型')
  })

  it('编辑者未获授权、作者本人无配置：仍显示尚未配置', () => {
    for (const snapshot of [editor, owner]) {
      setup(snapshot, [])
      const html = select()
      expect(html).toContain('尚未配置文本模型')
      expect(html).not.toContain('作者费用')
    }
  })

  it('编辑者有本人配置：显示本人配置选择框，不回退', () => {
    setup(granted, [textConfig])
    const html = select()
    expect(html).not.toContain('作者费用')
    expect(html).not.toContain('尚未配置')
    expect(html).toContain('默认：本人文本')
  })

  it('回退作者配置时清除残留的本人配置选择，请求不携带配置 ID', () => {
    for (const [snapshot, configs, cleared] of [[granted, [], true], [granted, [textConfig], false], [editor, [], false]] as const) {
      setup(snapshot, [...configs])
      const onChange = vi.fn()
      effects.length = 0
      render('/projects/9/chapters', '/projects/:pid/chapters', createElement(AIConfigSelect, { value: 5, onChange }))
      for (const setupEffect of effects) setupEffect()
      if (cleared) expect(onChange).toHaveBeenCalledExactlyOnceWith(null)
      else expect(onChange).not.toHaveBeenCalled()
    }
  })

  it('作品外（账号级）页面不回退，行为不变', () => {
    setup(granted, [])
    const html = select('/styles')
    expect(html).toContain('尚未配置文本模型')
    expect(html).not.toContain('作者费用')
  })

  it('明确仅使用本人配置时，即使位于作品路由也不提示作者模型回退', () => {
    setup(granted, [])
    const html = render('/projects/9/chapters', '/projects/:pid/chapters', createElement(AIConfigSelect, { modelType: 'image', ownOnly: true }))
    expect(html).toContain('尚未配置图片模型')
    expect(html).not.toContain('作者费用')
  })

  it('向量模型不在回退范围', () => {
    setup(granted, [])
    const html = render('/projects/9/knowledge', '/projects/:pid/knowledge', createElement(AIConfigSelect, { modelType: 'embedding' }))
    expect(html).toContain('尚未配置向量模型')
  })
})

describe('向导生成按钮按模型来源启用', () => {
  it('编辑者无本人模型且作者已授权：生成按钮可用', () => {
    setup(granted, [])
    expect(generateButton(wizard())).not.toContain('disabled')
  })

  it('编辑者无本人模型且未授权：生成按钮禁用', () => {
    setup(editor, [])
    expect(generateButton(wizard())).toContain('disabled')
  })

  it('作者无本人模型：生成按钮禁用（作者不回退）', () => {
    setup(owner, [])
    expect(generateButton(wizard())).toContain('disabled')
  })
})
