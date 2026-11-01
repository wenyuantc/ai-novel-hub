import type { ModalFuncProps } from 'antd'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useSettingsConfirmation as runConfirmationHook } from './useSettingsConfirmation'

// node环境不模拟DOM，只执行真实hook注册的生命周期和确认句柄所有权。
const hooks = vi.hoisted(() => ({
  owned: { current: null as { destroy: () => void } | null },
  setup: null as (() => (() => void)) | null,
  deps: [] as unknown[],
}))
vi.mock('react', () => ({
  useRef: () => hooks.owned,
  useEffect: (setup: () => (() => void), deps: unknown[]) => { hooks.setup = setup; hooks.deps = deps },
}))

beforeEach(() => { hooks.owned.current = null; hooks.setup = null; hooks.deps = [] })

function setup(denied = false) {
  const handles: Array<{ destroy: ReturnType<typeof vi.fn> }> = []
  const options: ModalFuncProps[] = []
  const modal = { confirm: vi.fn((option: ModalFuncProps) => {
    options.push(option)
    const handle = { destroy: vi.fn() }
    handles.push(handle)
    return handle
  }) }
  const confirm = runConfirmationHook(modal, denied)
  if (!hooks.setup) throw new Error('确认框清理生命周期未注册')
  const effect = hooks.setup
  const cleanup = effect()
  return { confirm, modal, handles, options, effect, cleanup }
}

describe('系统设置确认框的页面所有权', () => {
  it('离页销毁本页确认框且清空句柄，不调用全局销毁', () => {
    const view = setup()
    view.confirm({ content: '部署值：4' })
    view.cleanup()
    expect(view.handles[0].destroy).toHaveBeenCalledTimes(1)
    expect(hooks.owned.current).toBeNull()
    expect(view.modal).not.toHaveProperty('destroyAll')
  })

  it('denied变化清理旧确认框，拒绝创建新的管理确认框', () => {
    const view = setup()
    view.confirm({ title: '恢复部署值？' })
    expect(hooks.deps).toEqual([false])
    view.cleanup()
    const denied = setup(true)
    denied.confirm({ title: '不可再次展示' })
    expect(view.handles[0].destroy).toHaveBeenCalledTimes(1)
    expect(denied.modal.confirm).not.toHaveBeenCalled()
    expect(hooks.deps).toEqual([true])
    denied.cleanup()
  })

  it('StrictMode清理重装不遗留句柄，新确认框仍正常拥有和销毁', () => {
    const view = setup()
    view.cleanup()
    const cleanup = view.effect()
    view.confirm({ title: '重新挂载后确认' })
    expect(view.handles[0].destroy).not.toHaveBeenCalled()
    cleanup()
    expect(view.handles[0].destroy).toHaveBeenCalledTimes(1)
    expect(hooks.owned.current).toBeNull()
  })

  it('替换旧确认框后，迟到的afterClose不能清除新句柄', () => {
    const view = setup()
    const afterClose = vi.fn()
    view.confirm({ title: '旧确认', afterClose })
    view.confirm({ title: '新确认' })
    expect(view.handles[0].destroy).toHaveBeenCalledTimes(1)
    view.options[0].afterClose?.()
    expect(afterClose).toHaveBeenCalledTimes(1)
    expect(hooks.owned.current).toBe(view.handles[1])
    view.cleanup()
    expect(view.handles[1].destroy).toHaveBeenCalledTimes(1)
  })
})
