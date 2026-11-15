import { App, type ButtonProps, type ModalProps } from 'antd'
import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { projectApi } from '../../api/projects'
import type { AIConfig, Project, ProjectAccess, User } from '../../api/types'
import { useAIConfigStore } from '../../stores/aiConfigs'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'
import CoverGenerateModal, { CoverModelSource } from './CoverGenerateModal'
import CoverPanel from './CoverPanel'

const callbacks = vi.hoisted(() => ({ generate: undefined as (() => void) | undefined }))

// Node 下通过真实 store 的当前快照渲染组件和 hook。
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
vi.mock('../../stores/aiConfigs', async (original) => {
  const actual = await original<typeof import('../../stores/aiConfigs')>()
  const hook = (selector: (state: ReturnType<typeof actual.useAIConfigStore.getState>) => unknown) => selector(actual.useAIConfigStore.getState())
  return { ...actual, useAIConfigStore: Object.assign(hook, actual.useAIConfigStore) }
})
vi.mock('../../hooks/useLoader', () => ({ useLoader: () => ({
  data: { prompt: '星空', warning: null }, loading: false, error: null, reload: () => {},
}) }))
vi.mock('../AIConfigSelect', () => ({ default: () => createElement('span', null, '本人图片模型选择器') }))
vi.mock('antd', async (original) => {
  const actual = await original<typeof import('antd')>()
  return {
    ...actual,
    Modal: (props: ModalProps) => createElement('section', null, Array.isArray(props.footer) ? props.footer : null, props.children),
    Button: (props: ButtonProps) => {
      if (props.children === '生成封面') callbacks.generate = () => props.onClick?.({} as never)
      return createElement('button', { disabled: props.disabled }, props.children)
    },
  }
})

const user: User = { id: 1, username: '编辑者', created_at: '', is_admin: false, is_super_admin: false,
  effective_permissions: [], roles: [], authorization_revision: 1 }
const editor: ProjectAccess = { role: 'editor', is_collaborator: true, authorization_version: 'grant-1', allow_owner_ai: true,
  can_read: true, can_edit_content: true, can_use_ai: true, can_export: true, can_manage_collaborators: false,
  can_recycle: false, can_backup: false, can_write_reading_progress: false }
function project(access = editor): Project {
  return { id: 2, owner: { id: 9, username: '作者' }, access, title: '作品', genre: '', premise: '', status: 'planning',
    created_at: '', updated_at: '', chapter_count: 0, total_words: 0, cover_updated_at: null, auto_analyze: true,
    wizard_progress: { done: [], skipped: [], current: null }, default_style_id: null, default_style: null,
    synopsis: '', theme: '', tags: [], selling_points: '', audience: '', target_words: null, last_edited_chapter_id: null, reading: null }
}

async function load(value: Project) {
  vi.spyOn(projectApi, 'get').mockResolvedValue(value)
  await useProjectStore.getState().load(value.id)
}
function renderPanel(value: Project) { return renderToString(createElement(App, null, createElement(CoverPanel, { project: value }))) }
function renderModal() { return renderToString(createElement(App, null, createElement(CoverGenerateModal, {
  projectId: 2, title: '作品', onClose: () => {}, onSaved: () => {},
}))) }

beforeEach(() => {
  useProjectStore.getState().reset()
  useAIConfigStore.getState().reset()
  useAuthStore.setState({ status: 'authenticated', user })
  callbacks.generate = undefined
})
afterEach(() => { useProjectStore.getState().reset(); useAIConfigStore.getState().reset(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('封面生成界面和费用来源', () => {
  it('编辑者可以看到入口；即使拥有管理能力，查看者和管理员也不可生图', async () => {
    const value = project()
    await load(value)
    expect(renderPanel(value)).toContain('AI 生成')
    const owner = { ...project({ ...editor, role: 'owner', is_collaborator: false, authorization_version: null }),
      owner: { id: user.id, username: user.username } }
    await load(owner)
    expect(renderPanel(owner)).toContain('AI 生成')
    const privileged: User = { ...user, is_admin: true, effective_permissions: ['projects.read', 'projects.edit'] }
    useAuthStore.setState({ status: 'authenticated', user: privileged })
    const viewer = project({ ...editor, role: 'viewer', can_edit_content: true, can_use_ai: true })
    await load(viewer)
    expect(renderPanel(viewer)).not.toContain('AI 生成')
    const admin = project({ ...editor, role: 'admin', is_collaborator: false, authorization_version: null })
    await load(admin)
    expect(renderPanel(admin)).not.toContain('AI 生成')
  })

  it('只有无本人配置且已获授权时提示作者费用并允许生成', async () => {
    await load(project())
    useAIConfigStore.setState({ loaded: true, configs: [] })
    const fallback = renderModal()
    expect(fallback).toContain('可能使用作者密钥并产生作者费用')
    expect(fallback).not.toContain('本人图片模型选择器')
    expect(fallback).toMatch(/<button>生成封面<\/button>/)

    vi.mocked(projectApi.get).mockResolvedValue(project({ ...editor, allow_owner_ai: false, authorization_version: 'grant-2' }))
    await useProjectStore.getState().refresh()
    const missing = renderModal()
    expect(missing).not.toContain('可能使用作者密钥并产生作者费用')
    expect(missing).toContain('本人图片模型选择器')
    expect(missing).toMatch(/<button disabled="">生成封面<\/button>/)
  })

  it('按照服务端实际来源显示提示，旧响应不猜测且不展示配置细节', () => {
    const render = (source: 'actor' | 'owner' | undefined) => renderToString(createElement(CoverModelSource, { source }))
    expect(render('actor')).toContain('本次使用你的图片模型')
    expect(render('owner')).toContain('本次使用作者授权的图片模型')
    expect(render(undefined)).toBe('')
  })

  it('用户看到回退提示时才省略配置，已有本人模型时显式发送本人配置', async () => {
    await load(project())
    useAIConfigStore.setState({ loaded: true, configs: [] })
    const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify({
      image_base64: 'YQ==', mime_type: 'image/png', model_source: 'actor',
    })))
    vi.stubGlobal('fetch', fetch)
    expect(renderModal()).toContain('可能使用作者密钥并产生作者费用')
    callbacks.generate?.()
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ prompt: '星空' })

    useAIConfigStore.setState({ configs: [{ id: 2, model_type: 'image', is_default: true } as AIConfig] })
    const actor = renderModal()
    expect(actor).not.toContain('可能使用作者密钥并产生作者费用')
    expect(actor).toContain('本人图片模型选择器')
    callbacks.generate?.()
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ prompt: '星空', config_id: 2 })
  })

  it('显示期间配置变更或授权轮次变更，旧按钮不能发出新选择的请求', async () => {
    await load(project())
    useAIConfigStore.setState({ loaded: true, configs: [] })
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    renderModal()
    const fallbackClick = callbacks.generate
    useAIConfigStore.setState({ configs: [{ id: 2, model_type: 'image', is_default: true } as AIConfig] })
    fallbackClick?.()
    expect(fetch).not.toHaveBeenCalled()

    renderModal()
    const actorClick = callbacks.generate
    useAIConfigStore.setState({ configs: [] })
    actorClick?.()
    expect(fetch).not.toHaveBeenCalled()

    renderModal()
    const staleClick = callbacks.generate
    vi.mocked(projectApi.get).mockResolvedValue(project({ ...editor, authorization_version: 'grant-2' }))
    await useProjectStore.getState().refresh()
    staleClick?.()
    expect(fetch).not.toHaveBeenCalled()
  })
})
