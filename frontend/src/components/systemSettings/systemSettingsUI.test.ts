import { App } from 'antd'
import { createElement, type ReactElement } from 'react'
import { renderToString } from 'react-dom/server'
import { StaticRouter } from 'react-router-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AuditSummary, User } from '../../api/types'
import { AUDIT_ACTION, AUDIT_TARGET } from '../../api/auditLogs'
import AdminSystemSettingsPage, { SystemSettingRow } from '../../pages/AdminSystemSettingsPage'
import { useAuthStore } from '../../stores/auth'
import { auditSummaryRows } from '../../utils/auditLogs'
import { publicConfigFromSettings, SYSTEM_SETTING_LABELS } from '../../utils/systemSettings'
import AppLayout from '../AppLayout'
import { SystemSettingsController } from './systemSettingsController'
import type { useSystemSettings } from './useSystemSettings'
import { boolItem, settings } from './systemSettingsTestFixtures'
import routerSource from '../../router.tsx?raw'
import appSource from '../../App.tsx?raw'
import pageSource from '../../pages/AdminSystemSettingsPage.tsx?raw'

vi.mock('./useSystemSettings', () => ({ useSystemSettings: () => view }))
vi.mock('../../stores/auth', async (original) => {
  const actual = await original<typeof import('../../stores/auth')>()
  const hook = (selector: (state: ReturnType<typeof actual.useAuthStore.getState>) => unknown) => selector(actual.useAuthStore.getState())
  return { ...actual, useAuthStore: Object.assign(hook, actual.useAuthStore) }
})
const admin: User = { id: 1, username: '管理员', is_admin: true, created_at: '' }
function initialView(): ReturnType<typeof useSystemSettings> {
  const controller = new SystemSettingsController()
  return { controller, state: { ...controller.getSnapshot(), data: settings() } }
}
let view = initialView()
const noop = () => {}
function render(element: ReactElement) {
  return renderToString(createElement(App, null, createElement(StaticRouter, { location: '/admin/system-settings' }, element))).replaceAll('<!-- -->', '')
}
afterEach(() => { view = initialView(); useAuthStore.setState({ user: null, status: 'anonymous' }) })

describe('系统设置页面与安全投影', () => {
  it('紧凑分组显示恰好六项、来源/默认/恢复及即时生效边界', () => {
    useAuthStore.setState({ user: admin, status: 'authenticated' })
    const html = render(createElement(AdminSystemSettingsPage))
    for (const label of Object.values(SYSTEM_SETTING_LABELS)) expect(html).toContain(label)
    for (const text of ['系统设置', '注册与外部能力', '任务与自动分析', '开放 API', '部署值', '代码默认', '恢复部署值', '保存设置', '每 30 秒同步', '无需重启', '不会取消运行中的任务', '不清空本分钟计数', '不能撤回已有外部副作用']) expect(html).toContain(text)
    expect(html.match(/class="system-setting-row"/g)).toHaveLength(6)
    expect(html).not.toContain('<table')
    expect(html).not.toMatch(/(?:min-)?width:\s*\d{3,}px/)
  })
  it('恢复加入待保存修改，不把null当立即关闭；当前有效来源仍保留', () => {
    const html = render(createElement(SystemSettingRow, { item: boolItem('registration_enabled', false, true), changes: { registration_enabled: null }, invalid: false, disabled: false, onBoolean: noop, onNumber: noop, onRestore: noop }))
    expect(html).toContain('待恢复，尚未保存')
    expect(html).toContain('当前有效：关闭')
    expect(html).toContain('ant-switch-checked')
    expect(html).toContain('后台覆盖')
  })
  it('失败/未加载不把默认值当有效配置，权限失效清空管理内容', () => {
    useAuthStore.setState({ user: admin, status: 'authenticated' })
    view.state.data = null; view.state.error = '离线'
    expect(render(createElement(AdminSystemSettingsPage))).toContain('尚未加载有效设置')
    view.state.denied = true; view.state.data = settings()
    const html = render(createElement(AdminSystemSettingsPage))
    expect(html).toContain('系统设置访问已停止')
    expect(html).not.toContain('配置版本')
    expect(html).not.toContain('system-setting-row')
    useAuthStore.setState({ user: { ...admin, is_admin: false } })
    expect(render(createElement(AdminSystemSettingsPage))).toContain('需要管理员权限')
  })
  it('管理导航/面包屑/懒路由齐备，公开根只引入小轮询工具', () => {
    useAuthStore.setState({ user: admin, status: 'authenticated' })
    const html = render(createElement(AppLayout))
    expect(html).toContain('href="/admin/system-settings"')
    expect(html).toContain('系统设置')
    expect(html).toContain('管理后台')
    expect(routerSource).toMatch(/path: 'admin\/system-settings',[\s\S]*?RequireAdmin[\s\S]*?import\('\.\/pages\/AdminSystemSettingsPage'\)/)
    expect(appSource).toContain('startPublicConfigPolling')
    expect(appSource).not.toContain('dashboardController')
    expect(pageSource).toContain('放弃未保存修改并重新载入')
    expect(pageSource).toContain('加入待恢复修改')
    expect(pageSource).toContain('useSettingsConfirmation(modal, state.denied)')
    expect(pageSource).not.toContain('modal.confirm(')
    useAuthStore.setState({ user: { ...admin, is_admin: false } })
    expect(render(createElement(AppLayout))).not.toContain('href="/admin/system-settings"')
  })
  it('只从管理投影提取公开bool，不泄露版本/并发/来源', () => {
    expect(publicConfigFromSettings(settings(4, false, false))).toEqual({ registration_enabled: false, mcp_enabled: false })
    expect(() => publicConfigFromSettings({ ...settings(), items: [] })).toThrow('缺少公开开关')
  })
  it('系统设置审计显示覆盖与有效值/来源，其他动作忽略该局部摘要', () => {
    const summary = {
      changed_fields: [], changes: [], word_count_before: null, word_count_after: null, body_chars_before: null, body_chars_after: null, reference_count_before: null, reference_count_after: null, affected_count: null, affected_ids: [], ids_truncated: false, chapter_count: null, version_count: null, from_at: null, to_at: null, expected_count: null, deleted_count: null, high_water_id: null,
      settings_changes: [{ key: 'registration_enabled', before_override: false, after_override: null, before_effective: false, after_effective: true, before_source: 'admin', after_source: 'deployment' }],
    } satisfies AuditSummary
    const rows = auditSummaryRows(summary, 'system_settings.update')
    expect(AUDIT_ACTION['system_settings.update']).toBe('修改系统设置')
    expect(AUDIT_TARGET.system_settings).toBe('系统设置')
    expect(rows).toContainEqual(expect.objectContaining({ label: '开放自主注册', value: expect.stringContaining('关闭（后台覆盖） → 开启（部署配置）') }))
    expect(rows).toContainEqual(expect.objectContaining({ value: expect.stringContaining('后台覆盖：关闭 → 沿用部署值') }))
    expect(auditSummaryRows(summary, 'user.update').some((row) => row.label === '开放自主注册')).toBe(false)
  })
})
