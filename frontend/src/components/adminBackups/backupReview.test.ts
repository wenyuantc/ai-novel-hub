import { afterEach, describe, expect, it, vi } from 'vitest'
import type { BackupOverview } from '../../api/types'
import { AdminBackupsController } from './adminBackupsController'
import { backupMailStatus, formatBackupSchedule } from './backupDisplay'
import { backupOverview, backupRun, backupSettings, deferred } from './backupTestFixtures'

afterEach(() => { vi.unstubAllGlobals() })

describe('备份计划时区与保存后的状态一致性', () => {
  it('普通HTTP缺少randomUUID时仍能以安全随机源生成v4受理意图', async () => {
    const random = vi.fn((bytes: Uint8Array) => bytes.fill(0xff))
    vi.stubGlobal('crypto', { getRandomValues: random })
    const api = {
      overview: vi.fn().mockResolvedValue(backupOverview()), settings: vi.fn().mockResolvedValue(backupSettings()),
      update: vi.fn().mockResolvedValue(backupSettings()), list: vi.fn().mockResolvedValue({ items: [], total: 0 }),
      get: vi.fn().mockResolvedValue(backupRun()), run: vi.fn().mockResolvedValue(backupRun()), download: vi.fn().mockResolvedValue(true),
    }
    const controller = new AdminBackupsController(api)
    controller.activate()
    await controller.refreshOverview()
    await controller.startBackup()
    expect(random).toHaveBeenCalledOnce()
    expect(api.run.mock.calls[0][0].request_id).toMatch(/^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/)
  })

  it('成功执行无需失败提醒，异常未配置和真实投递状态仍准确区分', () => {
    const run = backupRun()
    expect(backupMailStatus({ ...run, status: 'succeeded', cleanup_status: 'succeeded', notification_status: 'not_configured' })).toBe('无需失败提醒')
    expect(backupMailStatus({ ...run, status: 'succeeded', cleanup_status: 'failed', notification_status: 'not_configured' })).toBe('未配置通知')
    expect(backupMailStatus({ ...run, status: 'failed', notification_status: 'not_configured' })).toBe('未配置通知')
    expect(backupMailStatus({ ...run, notification_status: 'unknown' })).toBe('投递结果未知')
    expect(backupMailStatus({ ...run, notification_status: 'sent' })).toBe('SMTP 已受理')
  })

  it('计划始终按北京时间显示，并将无后缀服务端时间按UTC解析', () => {
    expect(formatBackupSchedule('2026-11-01T19:00:00Z')).toBe('2026-11-02 03:00（北京时间）')
    expect(formatBackupSchedule('2026-11-01T19:00:00')).toBe('2026-11-02 03:00（北京时间）')
    expect(formatBackupSchedule('2026-11-02T03:00:00+08:00')).toBe('2026-11-02 03:00（北京时间）')
    expect(formatBackupSchedule(null)).toBe('-')
  })

  it('保存成功废弃尚未返回的旧概览，立即取得新配置版本', async () => {
    const old = deferred<BackupOverview>()
    const api = {
      overview: vi.fn().mockReturnValueOnce(old.promise).mockResolvedValue(backupOverview(2)),
      settings: vi.fn().mockResolvedValue(backupSettings()),
      update: vi.fn().mockResolvedValue(backupSettings(2)),
      list: vi.fn().mockResolvedValue({ items: [], total: 0 }),
      get: vi.fn().mockResolvedValue(backupRun()),
      run: vi.fn().mockResolvedValue(backupRun()),
      download: vi.fn().mockResolvedValue(true),
    }
    const controller = new AdminBackupsController(api)
    controller.activate()
    await controller.refreshSettings()
    const pending = controller.refreshOverview()
    controller.edit('local_keep', 4)
    await controller.save()
    expect(api.overview).toHaveBeenCalledTimes(2)
    old.resolve(backupOverview(1))
    await pending
    expect(controller.getSnapshot().overview.data?.revision).toBe(2)
  })
})
