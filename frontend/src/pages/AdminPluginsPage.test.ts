import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { adminPluginsApi, type AdminPlugin } from '../api/adminPlugins'
import { ApiError } from '../api/client'
import type { User } from '../api/types'
import { useAuthStore } from '../stores/auth'
import AdminPluginsPage from './AdminPluginsPage'

const sample: AdminPlugin = { id: 'sample', name: '示例插件', description: '制作示例', version: '1', enabled: false, revision: 0, updated_at: null }
const view = { data: { items: [sample] }, loading: false, error: null, reload: vi.fn(), setData: vi.fn() }
let toggle: () => void
let active: boolean
const confirm = vi.fn()
vi.mock('../hooks/useLoader', () => ({ useLoader: () => view }))
vi.mock('../hooks/useAuthorizationScope', () => ({ useAuthorizationScope: () => ({ begin: () => 1, current: () => active, allowed: () => active }) }))
vi.mock('../components/systemSettings/useSettingsConfirmation', () => ({ useSettingsConfirmation: () => confirm }))
vi.mock('antd', async (original) => {
  const actual = await original<typeof import('antd')>()
  return { ...actual, Switch: (props: { onChange: () => void; disabled: boolean }) => {
    toggle = props.onChange
    return createElement('button', { disabled: props.disabled, role: 'switch' })
  } }
})
vi.mock('../stores/auth', async (original) => {
  const actual = await original<typeof import('../stores/auth')>()
  const hook = (select: (state: ReturnType<typeof actual.useAuthStore.getState>) => unknown) => select(actual.useAuthStore.getState())
  return { useAuthStore: Object.assign(hook, actual.useAuthStore) }
})
const admin: User = { id: 1, username: '管理', created_at: '', effective_permissions: ['plugins.read', 'plugins.update'], authorization_revision: 1, roles: [], is_super_admin: false, is_admin: false }
const render = () => renderToString(createElement(AdminPluginsPage))
beforeEach(() => {
  active = true
  view.data = { items: [{ ...sample }] }
  useAuthStore.setState({ user: admin, status: 'authenticated', enabledPlugins: ['other'] })
})
afterEach(() => { vi.restoreAllMocks(); vi.clearAllMocks(); vi.unstubAllGlobals() })
describe('功能插件管理', () => {
  it('按权限展示开关、插件资料或空状态', () => {
    useAuthStore.setState({ user: { ...admin, effective_permissions: [] } })
    expect(render()).toBe('')
    useAuthStore.setState({ user: { ...admin, effective_permissions: ['plugins.read'] } })
    const html = render()
    expect(html).toContain('制作示例')
    expect(html).toMatch(/<button disabled="" role="switch"/)
    view.data = { items: [] }
    expect(render()).toContain('暂无已登记的功能插件')
  })
  it('停用需确认取消任务且保留数据，启用直接提交并更新本地配置', async () => {
    const update = vi.spyOn(adminPluginsApi, 'setEnabled').mockResolvedValue({ ...sample, enabled: true, revision: 1 })
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ registration_enabled: true, enabled_plugins: ['other', 'sample'] }))))
    view.data.items[0].enabled = true
    render(); toggle()
    expect(confirm.mock.calls[0][0]).toMatchObject({ autoFocusButton: 'cancel', content: expect.stringMatching(/取消.*已有数据保留/) })
    expect(update).not.toHaveBeenCalled()
    view.data.items[0].enabled = false
    render(); toggle()
    await vi.waitFor(() => expect(view.setData).toHaveBeenCalledOnce())
    expect(update).toHaveBeenCalledWith(sample, true, expect.anything())
    expect(useAuthStore.getState().enabledPlugins).toEqual(['other', 'sample'])
  })
  it('409 后重读一次，不重发修改', async () => {
    const update = vi.spyOn(adminPluginsApi, 'setEnabled').mockRejectedValue(new ApiError(409, '过期'))
    render(); toggle()
    await vi.waitFor(() => expect(view.reload).toHaveBeenCalledOnce())
    expect(update).toHaveBeenCalledOnce()
    expect(view.setData).not.toHaveBeenCalled()
  })
  it('离页后迟到的成功结果不能反写配置或列表', async () => {
    let release!: (v: AdminPlugin) => void
    vi.spyOn(adminPluginsApi, 'setEnabled').mockReturnValue(new Promise((res) => { release = res }))
    render(); toggle()
    active = false
    release({ ...sample, enabled: true })
    await Promise.resolve()
    expect(view.setData).not.toHaveBeenCalled()
    expect(useAuthStore.getState().enabledPlugins).toEqual(['other'])
  })
})
