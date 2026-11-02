import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/client'
import type { BackupOverview, BackupRunList, BackupRunOut, BackupSettingsOut } from '../../api/types'
import { AdminBackupsController } from './adminBackupsController'
import { backupChanges, backupDraftError } from './backupSettings'
import { backupOverview, backupRun, backupSettings, deferred } from './backupTestFixtures'

function setup() {
  let read = true, configure = true, run = true, download = true
  const api = {
    overview: vi.fn().mockResolvedValue(backupOverview()), settings: vi.fn().mockResolvedValue(backupSettings()),
    update: vi.fn().mockResolvedValue(backupSettings(2)), list: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    get: vi.fn().mockResolvedValue(backupRun()), run: vi.fn().mockResolvedValue(backupRun()), download: vi.fn().mockResolvedValue(true),
  }
  const id = vi.fn().mockReturnValue('b6c73f91-c9cc-47aa-b2c2-22ebbc35d1fb')
  const controller = new AdminBackupsController(api, { read: () => read, configure: () => configure, run: () => run, download: () => download }, id)
  controller.activate()
  return { api, controller, id, readOnly: () => { configure = run = download = false }, deny: () => { read = false }, loseConfigure: () => { configure = false }, loseDownload: () => { download = false } }
}

describe('备份配置与秘密草稿', () => {
  it('空秘密省略、不trim，null才清除；普通false和独立份数完整保存', async () => {
    const { api, controller } = setup()
    await controller.refreshSettings()
    controller.edit('backup_password', '  private password  ')
    controller.edit('smtp_password', '')
    controller.edit('s3_secret_key', null)
    controller.edit('encryption_enabled', false)
    controller.editNumber('local_keep', 3); controller.editNumber('s3_keep', 9)
    await controller.save()
    expect(api.update).toHaveBeenCalledExactlyOnceWith({ revision: 1, changes: {
      backup_password: '  private password  ', s3_secret_key: null, encryption_enabled: false, local_keep: 3, s3_keep: 9,
    } }, expect.any(AbortSignal))
    expect(controller.getSnapshot()).toMatchObject({ changes: {}, saving: false, settings: { data: { revision: 2 } } })
  })
  it('读取配置不向只有查看权限的角色发请求，不能保存或执行或下载', async () => {
    const { api, controller, readOnly } = setup(); readOnly()
    await controller.refreshSettings(); await controller.refreshOverview()
    controller.edit('backup_password', 'not permitted password')
    await controller.save(); await controller.startBackup(); await controller.download(1, 'local')
    expect(api.overview).toHaveBeenCalledOnce()
    for (const key of ['settings', 'update', 'run', 'download'] as const) expect(api[key]).not.toHaveBeenCalled()
    expect(controller.getSnapshot().changes).toEqual({})
  })
  it('刷新保留秘密草稿，版本变化暂停保存且必须明确重载', async () => {
    const { api, controller } = setup(); await controller.refreshSettings()
    controller.edit('backup_password', 'private draft password')
    api.settings.mockResolvedValue(backupSettings(4))
    await controller.refreshSettings()
    expect(controller.getSnapshot()).toMatchObject({ changes: { backup_password: 'private draft password' }, reviewRequired: true, settings: { data: { revision: 1 } } })
    await controller.save(); expect(api.update).not.toHaveBeenCalled()
    await controller.reloadSettings()
    expect(controller.getSnapshot()).toMatchObject({ changes: {}, reviewRequired: false, settings: { data: { revision: 4 } } })
  })
  it.each([0, 409, 503])('保存%s保持草稿、不自动重发或虚构成功', async (status) => {
    const { api, controller } = setup(); await controller.refreshSettings()
    controller.edit('local_keep', 4)
    api.update.mockRejectedValue(new ApiError(status, '拒绝'))
    await controller.save(); await controller.save(); await controller.refreshSettings()
    expect(api.update).toHaveBeenCalledTimes(1)
    expect(controller.getSnapshot()).toMatchObject({ reviewRequired: true, changes: { local_keep: 4 }, settings: { data: { revision: 1 } } })
  })
  it('非法份数、端口和密码不能提交；修正后才允许', async () => {
    const { api, controller } = setup(); await controller.refreshSettings()
    controller.edit('local_enabled', false)
    for (const bad of [null, 0, -1, 1.5, 1001]) { controller.editNumber('local_keep', bad); await controller.save() }
    expect(api.update).not.toHaveBeenCalled()
    controller.editNumber('local_keep', 4); controller.edit('backup_password', 'too short')
    await controller.save(); expect(api.update).not.toHaveBeenCalled()
    controller.edit('backup_password', 'valid private password'); await controller.save()
    expect(api.update).toHaveBeenCalledOnce()
    expect(backupChanges({ smtp_username: '', backup_password: '', s3_access_key: null })).toEqual({ s3_access_key: null })
    expect(backupDraftError({ backup_password: 'invalid\npassword\n' })).not.toBeNull()
  })
  it('失配置权限清除已加载连接参数和秘密；迟到保存不能复活', async () => {
    const { api, controller, loseConfigure } = setup(); await controller.refreshSettings()
    const save = deferred<BackupSettingsOut>(); api.update.mockReturnValue(save.promise)
    controller.edit('smtp_password', 'private draft password'); const pending = controller.save()
    loseConfigure(); controller.revokeConfigure()
    save.resolve(backupSettings(2)); await pending
    expect(controller.getSnapshot()).toMatchObject({ settings: { data: null }, changes: {}, saving: false })
  })
})

describe('受理意图和下载权限', () => {
  it('双击只受理一次；不确定响应手动核实原意图时沿用同一request_id', async () => {
    const { api, controller, id } = setup(); await controller.refreshOverview()
    const accepted = deferred<BackupRunOut>(); api.run.mockReturnValueOnce(accepted.promise)
    const first = controller.startBackup(); await controller.startBackup()
    expect(api.run).toHaveBeenCalledOnce()
    accepted.reject(new ApiError(0, '断线')); await first
    expect(controller.getSnapshot().manualUncertain).toBe(true)
    expect(api.run).toHaveBeenCalledTimes(1)
    await controller.startBackup()
    expect(api.run.mock.calls[0][0]).toEqual(api.run.mock.calls[1][0])
    expect(id).toHaveBeenCalledOnce()
    expect(controller.getSnapshot().notice).toContain('已受理')
    expect(controller.getSnapshot().manualUncertain).toBe(false)
  })
  it('仅在实际发起下载后提示已开始，预检期间撤权不能发起', async () => {
    const { api, controller, loseDownload } = setup()
    const started = deferred<boolean>(); api.download.mockReturnValueOnce(started.promise)
    const first = controller.download(3, 's3')
    await controller.download(3, 's3'); expect(api.download).toHaveBeenCalledOnce()
    const guard = api.download.mock.calls[0][2]
    loseDownload(); controller.revokeActions(); expect(guard()).toBe(false)
    started.resolve(true); await first
    expect(controller.getSnapshot().notice).toBeNull()
    expect(controller.getSnapshot().downloading).toBeNull()
  })
  it('后台不就绪或已有执行时不创建新意图；定时关闭不阻止手动', async () => {
    const { api, controller } = setup()
    api.overview.mockResolvedValueOnce({ ...backupOverview(), ready: false })
    await controller.refreshOverview(); await controller.startBackup(); expect(api.run).not.toHaveBeenCalled()
    api.overview.mockResolvedValueOnce({ ...backupOverview(), active_run: backupRun() })
    await controller.refreshOverview(); await controller.startBackup(); expect(api.run).not.toHaveBeenCalled()
    await controller.refreshOverview(); await controller.startBackup(); expect(api.run).toHaveBeenCalledOnce()
  })
})

describe('请求失效和分页', () => {
  it('StrictMode卸载重挂清空loading并使旧响应失效', async () => {
    const { api, controller } = setup(); const old = deferred<BackupOverview>()
    api.overview.mockReturnValueOnce(old.promise).mockResolvedValueOnce(backupOverview(8))
    const first = controller.refreshOverview(); controller.deactivate(); controller.activate()
    await controller.refreshOverview(); old.resolve(backupOverview(1)); await first
    expect(controller.getSnapshot()).toMatchObject({ overview: { data: { revision: 8 }, loading: false } })
  })
  it('同actor撤权、离页和换账号时清除秘密/弹窗/结果，旧响应不能回填', async () => {
    const { api, controller, deny } = setup(); await controller.refreshSettings()
    controller.edit('backup_password', 'private draft password')
    const old = deferred<BackupRunOut>(); api.get.mockReturnValue(old.promise)
    controller.openDetail(1); deny(); controller.revoke(); old.resolve(backupRun())
    await Promise.resolve(); await Promise.resolve()
    expect(controller.getSnapshot()).toMatchObject({ denied: true, settings: { data: null }, changes: {}, detailId: null, detail: { data: null } })
  })
  it('筛选变化废弃旧列表，页码超出实际范围则回退有效页', async () => {
    const { api, controller } = setup(); const old = deferred<BackupRunList>()
    api.list.mockReturnValueOnce(old.promise)
    const first = controller.refreshList()
    api.list.mockResolvedValueOnce({ items: [], total: 1 }).mockResolvedValueOnce({ items: [backupRun(8)], total: 1 })
    controller.setQuery({ status: 'succeeded', page: 10, page_size: 20 })
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
    old.resolve({ items: [backupRun(1)], total: 50 }); await first
    expect(controller.getSnapshot().list.data?.items[0].id).toBe(8)
    expect(controller.getSnapshot().query).toMatchObject({ status: 'succeeded', page: 1 })
  })
  it('详情403立即移除管理数据，配置403仅撤配置不删除合法概览', async () => {
    const { api, controller } = setup(); await controller.refreshOverview()
    api.settings.mockRejectedValueOnce(new ApiError(403, '权限不足'))
    await controller.refreshSettings()
    expect(controller.getSnapshot().overview.data).not.toBeNull()
    expect(controller.getSnapshot().denied).toBe(false)
    api.get.mockRejectedValueOnce(new ApiError(403, '权限不足'))
    controller.openDetail(1); await Promise.resolve(); await Promise.resolve()
    expect(controller.getSnapshot()).toMatchObject({ denied: true, overview: { data: null } })
  })
})
