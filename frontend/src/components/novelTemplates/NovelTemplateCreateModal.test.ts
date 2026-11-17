import { createElement, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readCreationIntent, storeCreationIntent, type TemplateCreationIntent } from '../../utils/novelTemplateCreation'
import NovelTemplateCreateModal from './NovelTemplateCreateModal'

const harness = vi.hoisted(() => ({
  submit: null as (() => void) | null,
  actorCurrent: true,
  createProject: vi.fn(),
}))

vi.mock('../../hooks/usePenNames', () => ({ capturePenNameActor: () => () => harness.actorCurrent }))
vi.mock('../../api/novelTemplates', async (original) => {
  const actual = await original<typeof import('../../api/novelTemplates')>()
  return { ...actual, novelTemplatesApi: { ...actual.novelTemplatesApi, createProject: harness.createProject } }
})
vi.mock('../shelf/PenNameSelect', () => ({ default: () => null }))
vi.mock('../ProjectInfoFields', () => ({ default: () => null }))
vi.mock('antd', () => {
  const container = ({ children }: { children?: ReactNode }) => createElement('div', null, children)
  const form = Object.assign(container, { Item: container, useForm: () => [{ validateFields: async () => ({}), isFieldTouched: () => false }] })
  return {
    Alert: ({ message, description }: { message?: ReactNode; description?: ReactNode }) => createElement('div', null, message, description),
    App: { useApp: () => ({ message: { success: vi.fn() } }) },
    Button: container, Collapse: () => null, Form: form,
    Input: Object.assign(() => null, { TextArea: () => null }),
    Modal: ({ children, onOk }: { children: ReactNode; onOk: () => void }) => {
      harness.submit = onOk
      return createElement('div', null, children)
    },
    Pagination: () => null, Popconfirm: container, Radio: { Group: () => null }, Select: () => null,
    Space: container, Typography: { Text: container },
  }
})

function memoryStorage(): Storage {
  const entries = new Map<string, string>()
  return { get length() { return entries.size }, clear: () => entries.clear(), getItem: (key) => entries.get(key) ?? null,
    key: (index) => [...entries.keys()][index] ?? null, removeItem: (key) => { entries.delete(key) },
    setItem: (key, value) => { entries.set(key, value) } }
}

const intent: TemplateCreationIntent = { version: 1, actor_id: 7, template_id: 12, template_name: '星河模板', request: {
  expected_revision: 3, intent_key: 'a1234567-1111-4111-8111-123456789abc', title: '新故事', pen_name_id: 5,
} }

function render(onCreated = vi.fn()) {
  return renderToStaticMarkup(createElement(NovelTemplateCreateModal, { actorId: 7, onClose: vi.fn(), onSwitchBlank: vi.fn(), onCreated }))
}

beforeEach(() => {
  vi.stubGlobal('sessionStorage', memoryStorage())
  harness.actorCurrent = true
  harness.submit = null
  harness.createProject.mockReset()
})
afterEach(() => vi.unstubAllGlobals())

describe('模板建书弹窗的重试', () => {
  it('网络结果未知后重开仍显示模板名称，以同一请求找到作品', async () => {
    storeCreationIntent(intent)
    harness.createProject.mockRejectedValueOnce(new Error('网络中断')).mockResolvedValueOnce({ id: 20 })
    expect(render()).toContain('待确认的创建请求：星河模板 · 版本 3')
    harness.submit?.()
    await vi.waitFor(() => expect(harness.createProject).toHaveBeenCalledTimes(1))
    await vi.waitFor(() => expect(readCreationIntent(7)).toEqual(intent))
    const created = vi.fn()
    expect(render(created)).toContain('待确认的创建请求：星河模板 · 版本 3')
    harness.submit?.()
    await vi.waitFor(() => expect(created).toHaveBeenCalledExactlyOnceWith(20))
    expect(harness.createProject.mock.calls[0][1]).toEqual(intent.request)
    expect(harness.createProject.mock.calls[1][1]).toEqual(intent.request)
    expect(readCreationIntent(7)).toBeNull()
  })

  it('旧会话没有名称时显示通用描述，不暴露内部编号', () => {
    const legacy: TemplateCreationIntent = { version: 1, actor_id: intent.actor_id, template_id: intent.template_id, request: intent.request }
    storeCreationIntent(legacy)
    const html = render()
    expect(html).toContain('待确认的创建请求：所选模板 · 版本 3')
    expect(html).not.toContain('模板 #12')
  })

  it('同账号已有新意图时，旧响应不会导航或覆盖新意图', async () => {
    storeCreationIntent(intent)
    let release!: (project: { id: number }) => void
    harness.createProject.mockReturnValueOnce(new Promise((resolve) => { release = resolve }))
    const created = vi.fn()
    render(created)
    harness.submit?.()
    await vi.waitFor(() => expect(harness.createProject).toHaveBeenCalledTimes(1))
    const next = { ...intent, request: { ...intent.request, intent_key: 'b1234567-1111-4111-8111-123456789abc', title: '另一本书' } }
    storeCreationIntent(next)
    release({ id: 20 })
    await new Promise<void>((resolve) => setImmediate(resolve))
    expect(created).not.toHaveBeenCalled()
    expect(readCreationIntent(7)).toEqual(next)
    harness.submit?.()
    await new Promise<void>((resolve) => setImmediate(resolve))
    expect(harness.createProject).toHaveBeenCalledTimes(1)
    expect(readCreationIntent(7)).toEqual(next)
  })

  it('身份失效后迟到的成功不导航，并保留重试请求', async () => {
    storeCreationIntent(intent)
    let release!: (project: { id: number }) => void
    harness.createProject.mockReturnValueOnce(new Promise((resolve) => { release = resolve }))
    const created = vi.fn()
    render(created)
    harness.submit?.()
    await vi.waitFor(() => expect(harness.createProject).toHaveBeenCalledTimes(1))
    harness.actorCurrent = false
    release({ id: 20 })
    await new Promise<void>((resolve) => setImmediate(resolve))
    expect(created).not.toHaveBeenCalled()
    expect(readCreationIntent(7)).toEqual(intent)
  })
})
