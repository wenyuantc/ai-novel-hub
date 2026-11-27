import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { adminMediaStorageApi } from '../api/adminMediaStorage'
import { ApiError } from '../api/client'
import type { MediaLocationInput, MediaStorageOverview, MediaStoragePatch, User } from '../api/types'
import { useAuthStore } from '../stores/auth'
import { adminDestinations } from '../utils/permissions'
import AdminMediaStoragePage from './AdminMediaStoragePage'
import { locationInput } from '../components/media/storageDraft'

const data: MediaStorageOverview = {
  revision: 4, default_quota_bytes: 5 * 1024 ** 3, total_bytes: 1024, volume_mismatch: true,
  locations: [{ id: 1, kind: 's3', state: 'active', configuration: { bucket: 'files', endpoint: 'https://s3.example.test' },
    has_access_key: true, has_secret_key: true, file_count: 2, size_bytes: 1024 }], volume_reports: [],
}
const view = { data, loading: false, error: null, reload: vi.fn(), setData: vi.fn() }
const confirm = vi.fn()
let active = true
let save: (patch: MediaStoragePatch) => void
let testLocation: (location: MediaLocationInput) => void
vi.mock('../hooks/useLoader', () => ({ useLoader: () => view }))
vi.mock('../hooks/useAuthorizationScope', () => ({ useAuthorizationScope: () => ({ allowed: () => active, begin: () => 1, current: () => active }) }))
vi.mock('../components/systemSettings/useSettingsConfirmation', () => ({ useSettingsConfirmation: () => confirm }))
vi.mock('../components/media/StorageSettingsForm', async (original) => {
  const actual = await original<typeof import('../components/media/StorageSettingsForm')>()
  return { ...actual, default: (props: Parameters<typeof actual.default>[0]) => {
    save = props.onSave
    testLocation = props.onTest
    return createElement(actual.default, props)
  } }
})
vi.mock('../stores/auth', async (original) => {
  const actual = await original<typeof import('../stores/auth')>()
  const hook = (select: (state: ReturnType<typeof actual.useAuthStore.getState>) => unknown) => select(actual.useAuthStore.getState())
  return { useAuthStore: Object.assign(hook, actual.useAuthStore) }
})
const admin: User = { id: 1, username: '管理', created_at: '', effective_permissions: ['media_storage.read', 'media_storage.configure'], authorization_revision: 1, roles: [], is_super_admin: false, is_admin: false }
const render = () => renderToString(createElement(AdminMediaStoragePage))
beforeEach(() => {
  active = true
  useAuthStore.setState({ user: admin, status: 'authenticated' })
})
afterEach(() => { vi.restoreAllMocks(); vi.clearAllMocks() })

it('只读管理者可进入并查看卷不一致诊断，表单和操作禁用', () => {
  const reader: User = { ...admin, effective_permissions: ['media_storage.read'] }
  useAuthStore.setState({ user: reader })
  expect(adminDestinations(reader).map(d => d.path)).toContain('/admin/media-storage')
  const html = render()
  expect(html).toContain('各实例的媒体目录不是同一个共享卷')
  expect(html).toMatch(/<input[^>]*disabled/)
  expect(html).toContain('测试连接')
  useAuthStore.setState({ user: { ...admin, effective_permissions: [] } })
  expect(render()).toBe('')
})
it('省略留空凭据，切回本地不提交隐藏的 S3 字段', () => {
  const values = { kind: 's3' as const, endpoint: 'https://s3.example.test', bucket: 'files', access_key: '', secret_key: '' }
  const body = locationInput(values)
  expect(body).not.toHaveProperty('access_key')
  expect(body).not.toHaveProperty('secret_key')
  expect(locationInput({ ...values, kind: 'local', secret_key: 'draft' })).toEqual({ kind: 'local' })
})
it('确认切换影响之后提交，单独测试连接不保存', async () => {
  const patch = vi.spyOn(adminMediaStorageApi, 'update').mockResolvedValue(data)
  const probe = vi.spyOn(adminMediaStorageApi, 'test').mockResolvedValue({ ok: true })
  render()
  await testLocation({ kind: 'local' })
  expect(probe).toHaveBeenCalledOnce()
  save({ revision: 4, location: { kind: 'local' } })
  expect(patch).not.toHaveBeenCalled()
  const options = confirm.mock.calls[0][0]
  expect(options.content).toMatch(/新文件.*旧文件.*可读/)
  await options.onOk()
  expect(view.setData).toHaveBeenCalledOnce()
})
it('修订冲突后重读一次，不重发修改', async () => {
  const update = vi.spyOn(adminMediaStorageApi, 'update').mockRejectedValue(new ApiError(409, '版本冲突'))
  render()
  await save({ revision: 4, default_quota_bytes: 1024 })
  expect(view.reload).toHaveBeenCalledOnce()
  expect(update).toHaveBeenCalledOnce()
})
it('离页后的成功响应无法写回，迟到的冲突也不刷新', async () => {
  const update = vi.spyOn(adminMediaStorageApi, 'update').mockImplementation(async () => { active = false; return data })
  render()
  await save({ revision: 4, default_quota_bytes: 1024 })
  expect(view.setData).not.toHaveBeenCalled()
  active = true
  update.mockImplementation(async () => { active = false; throw new ApiError(409, '版本冲突') })
  render()
  await save({ revision: 4, default_quota_bytes: 1024 })
  expect(view.reload).not.toHaveBeenCalled()
})
