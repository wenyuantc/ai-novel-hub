import { isValidElement, type ReactElement, type ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import SettingThumbnailGenerateModal from './SettingThumbnailGenerateModal'

const test = vi.hoisted(() => ({
  states: [] as unknown[], stateIndex: 0,
  refs: [] as Array<{ current: unknown }>, refIndex: 0, effectRun: false,
  load: vi.fn(), generate: vi.fn(), apply: vi.fn(),
  success: vi.fn(),
}))

vi.mock('react', async (original) => ({
  ...await original<typeof import('react')>(),
  useState: <T,>(initial: T | (() => T)) => {
    const index = test.stateIndex++
    if (index >= test.states.length) test.states[index] = typeof initial === 'function' ? (initial as () => T)() : initial
    return [test.states[index], (value: T | ((previous: T) => T)) => {
      test.states[index] = typeof value === 'function' ? (value as (previous: T) => T)(test.states[index] as T) : value
    }]
  },
  useRef: <T,>(initial: T) => {
    const index = test.refIndex++
    if (index >= test.refs.length) test.refs[index] = { current: initial }
    return test.refs[index]
  },
  useEffect: (effect: () => void) => { if (!test.effectRun) { test.effectRun = true; effect() } },
}))
vi.mock('antd', async (original) => ({
  ...await original<typeof import('antd')>(), App: { useApp: () => ({ message: { success: test.success } }) },
}))
vi.mock('../../api/settingThumbnailAi', () => ({
  loadThumbnailPrompt: test.load, generateThumbnailAI: test.generate, applyThumbnailAI: test.apply,
}))
vi.mock('../../stores/aiConfigs', () => {
  const state = { configs: [{ id: 3, model_type: 'image', is_default: true }], loaded: true }
  return { useAIConfigStore: Object.assign((selector: (value: typeof state) => unknown) => selector(state),
    { getState: () => state }) }
})
vi.mock('../../stores/project', () => {
  const state = { project: { access: { role: 'owner' } } }
  return { useProjectStore: Object.assign((selector: (value: typeof state) => unknown) => selector(state),
    { getState: () => state }) }
})

const flush = () => new Promise((resolve) => setTimeout(resolve, 0))
const target = { scope: 'project' as const, projectId: 1, kind: 'character' as const, entryId: 2 }

function find(node: ReactNode, predicate: (element: ReactElement<Record<string, unknown>>) => boolean): ReactElement<Record<string, unknown>> | undefined {
  if (Array.isArray(node)) {
    for (const child of node) { const match = find(child, predicate); if (match) return match }
    return undefined
  }
  if (!isValidElement<Record<string, unknown>>(node)) return undefined
  if (predicate(node)) return node
  return find(node.props.children as ReactNode, predicate)
}

beforeEach(() => {
  test.states = []; test.stateIndex = 0; test.refs = []; test.refIndex = 0; test.effectRun = false
  test.load.mockReset().mockResolvedValue({ prompt: '默认提示词', prompt_token: 'prompt-token' })
  test.generate.mockReset().mockResolvedValue({ image_base64: 'YWJj', mime_type: 'image/webp', model_source: 'actor', preview_token: 'preview-token' })
  test.apply.mockReset(); test.success.mockReset()
})

describe('设定生图冲突时的草稿', () => {
  it('模板设置在新标签打开，超长 Unicode 输入保留原文并阻止生成', async () => {
    const props = { target, name: '角色', version: null, current: () => true, onConflict: vi.fn(),
      onClose: vi.fn(), onApplied: vi.fn() }
    const render = () => { test.stateIndex = 0; test.refIndex = 0; return SettingThumbnailGenerateModal(props) }
    render(); await flush()
    let view = render()
    const link = find(view, (element) => element.props.href === '/prompt-templates?key=setting.thumbnail')!
    expect(link.props.target).toBe('_blank')
    expect(link.props.rel).toBe('noopener noreferrer')
    const input = find(view, (element) => element.props.id === 'setting-image-prompt')!
    const original = `${'🦊'.repeat(1999)}尾`
    ;(input.props.onChange as (event: unknown) => void)({ target: { value: original } })
    view = render()
    expect((find(view, (element) => element.props.id === 'setting-image-prompt-count')?.props.children as ReactNode[])[0]).toBe(2000)
    const atLimit = (view.props.footer as ReactElement<{ disabled: boolean }>[]).find((item) => item.key === 'generate')!
    expect(atLimit.props.disabled).toBe(false)
    const edited = `新${original}`
    const updatedInput = find(view, (element) => element.props.id === 'setting-image-prompt')!
    ;(updatedInput.props.onChange as (event: unknown) => void)({ target: { value: edited } })
    view = render()
    expect(find(view, (element) => element.props.id === 'setting-image-prompt')?.props.value).toBe(edited)
    expect(find(view, (element) => element.props.id === 'setting-image-prompt')?.props['aria-invalid']).toBe(true)
    expect(find(view, (element) => element.props.id === 'setting-image-prompt-count')?.props.children)
      .toEqual([2001, ' / 2000', '，请缩短后生成'])
    const generate = (view.props.footer as ReactElement<{ onClick: () => void; disabled: boolean }>[])
      .find((item) => item.key === 'generate')!
    expect(generate.props.disabled).toBe(true)
    generate.props.onClick(); await flush()
    expect(test.generate).not.toHaveBeenCalled()
  })

  it.each([new ApiError(409, '设定已变化'), new ApiError(503, '结果未知')])('确认失败后保留提示词，关闭时才要求刷新：$status', async (failure) => {
    const onConflict = vi.fn(), onClose = vi.fn()
    const props = { target, name: '角色', version: null, current: () => true, onConflict, onClose, onApplied: vi.fn() }
    const render = () => { test.stateIndex = 0; test.refIndex = 0; return SettingThumbnailGenerateModal(props) }
    const button = (view: ReturnType<typeof render>, key: string) =>
      (view.props.footer as ReactElement<{ onClick: () => void; disabled: boolean }>[]).find((item) => item.key === key)!

    render(); await flush()
    let view = render()
    const input = find(view, (element) => element.props.id === 'setting-image-prompt')!
    ;(input.props.onChange as (event: unknown) => void)({ target: { value: '用户编辑的提示词' } })
    view = render(); button(view, 'generate').props.onClick(); await flush()
    expect(test.generate.mock.calls[0][1].prompt).toBe('用户编辑的提示词')

    test.apply.mockRejectedValue(failure)
    view = render(); button(view, 'apply').props.onClick(); await flush()
    view = render()
    expect(find(view, (element) => element.props.id === 'setting-image-prompt')?.props.value).toBe('用户编辑的提示词')
    expect(button(view, 'generate').props.disabled).toBe(true)
    expect(button(view, 'apply').props.disabled).toBe(true)
    expect(onConflict).not.toHaveBeenCalled()
    button(view, 'cancel').props.onClick()
    expect(onConflict).toHaveBeenCalledWith(failure)
    expect(onClose).not.toHaveBeenCalled()
    expect(test.apply).toHaveBeenCalledOnce()
  })
})
