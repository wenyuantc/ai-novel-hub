import { App } from 'antd'
import { createElement, type ReactElement } from 'react'
import { renderToString } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AuditCleanupPreview, AuditLogDetail } from '../../api/types'

const state = vi.hoisted(() => ({ mobile: true, admin: true, loading: false, error: null as string | null, items: [] as import('../../api/types').AuditLogBrief[] }))
vi.mock('../../hooks/useIsMobile', () => ({ useIsMobile: () => state.mobile, useModalWidth: (width: number | string) => state.mobile ? 'calc(100vw - 24px)' : width }))
vi.mock('../../hooks/useLoader', () => ({ useLoader: () => ({ data: { items: state.items, total: state.items.length, page: 1 }, loading: state.loading, error: state.error, reload: () => {} }) }))
vi.mock('../../stores/auth', () => ({ useAuthStore: (select: (s: unknown) => unknown) => select({ status: 'authenticated', user: { id: 7, username: '真实管理员', is_admin: state.admin, is_super_admin: state.admin, effective_permissions: [], authorization_revision: 1, roles: [] }, mcpEnabled: false, logout: async () => {} }) }))
vi.mock('../../stores/project', () => ({ useProjectStore: (select: (s: unknown) => unknown) => select({ project: null }) }))
vi.mock('../../stores/tasks', () => ({ useTasksStore: (select: (s: unknown) => unknown) => select({ active: [], loaded: true }) }))
vi.mock('../TaskPanel', () => ({ default: () => null }))
vi.mock('../ChangePasswordModal', () => ({ default: () => null }))
vi.mock('../../theme/ThemeSwitch', () => ({ default: () => null }))

const { AuditLogDetailContent } = await import('./AuditLogDetailDrawer')
const { AuditCleanupPreviewContent } = await import('./AuditCleanupModal')
const { default: AdminAuditLogsPage, AuditLogResults } = await import('../../pages/AdminAuditLogsPage')
const { default: AppLayout } = await import('../AppLayout')
const { default: RequireAdmin } = await import('../RequireAdmin')
const noop = () => {}
const render = (element: ReactElement) => renderToString(createElement(App, null, element)).replaceAll('<!-- -->', '')
const log = (over: Partial<AuditLogDetail> = {}): AuditLogDetail => ({
  id: 31, operation_id: 'operation-31', occurred_at: '2026-10-31T01:00:00Z', actor_user_id: 7, actor_username: '已注销的管理员', action: 'chapter.update', result: 'success', target_type: 'chapter', target_id: 12, target_label: '已删除的旧标题', owner_user_id: 8, owner_username: '原所有者', project_id: 9, project_title: '旧作品名', chapter_id: 12, error_code: null, error_message: null,
  summary: { changed_fields: ['content'], changes: [], word_count_before: 20, word_count_after: 28, body_chars_before: null, body_chars_after: null, reference_count_before: null, reference_count_after: null, affected_count: null, affected_ids: [], ids_truncated: false, chapter_count: null, version_count: null, from_at: null, to_at: null, expected_count: null, deleted_count: null, high_water_id: null },
  version_ref: { project_id: 9, chapter_id: 12, version_id: 4, seq: 2, content_sha256: 'a'.repeat(64) }, version_status: 'available', ...over,
})

afterEach(() => { state.mobile = true; state.admin = true; state.items = []; state.loading = false; state.error = null })

describe('审计详情只读语义', () => {
  const detail = (value: AuditLogDetail) => render(createElement(AuditLogDetailContent, { canReadVersion: true, log: value, version: null, versionError: null, versionLoading: false, onLoadVersion: noop }))
  it('取消原因只投影task.cancel_request，标签明确仅受理且真实actor不被替换', () => {
    const value = log({ action: 'task.cancel_request', target_type: 'task', target_label: '项目智能体 #42', version_ref: null, version_status: null })
    value.summary.task_key = '00000000-0000-4000-8000-000000000001'
    value.summary.task_kind = 'agent.turn'
    value.summary.accepted_from_status = 'running'
    value.summary.cancel_reason = '管理员主动填写的排查说明 <script>仅普通文本</script>'
    const html = detail(value)
    for (const expected of ['已受理', '管理员取消原因', '管理员主动填写的排查说明', '项目智能体', '受理时状态', '运行中', '仅表示取消请求受理', '已注销的管理员', '原所有者', '&lt;script&gt;']) expect(html).toContain(expected)
    expect(html).not.toContain('<script>')
    expect(html).not.toContain('任务已取消')
    expect(detail({ ...value, action: 'chapter.update' })).not.toContain('管理员主动填写的排查说明')
    const result = render(createElement(AuditLogResults, { items: [value], loading: false, mobile: true, onDetail: noop }))
    expect(result).toContain('已受理')
  })
  it('到期自动清理明确标识系统而不是 #null，动作与保留摘要可读', () => {
    const value = log({ action: 'project.expire', actor_user_id: null, actor_username: '系统自动清理', version_ref: null, version_status: null })
    value.summary.lifecycle_action = 'expire'
    value.summary.retention_days = 30
    const html = detail(value)
    for (const text of ['系统自动清理（系统）', '到期自动清理作品', '30 天']) expect(html).toContain(text)
    expect(html).not.toContain('#null')
    const mobile = render(createElement(AuditLogResults, { items: [value], loading: false, mobile: true, onDetail: noop }))
    expect(mobile).toContain('系统自动清理（系统）')
    expect(mobile).not.toContain('#null')
  })
  it('显示真实操作人与历史归属，不把正文或版本备注当成审计全文差异', () => {
    const html = detail(log())
    for (const text of ['已注销的管理员', '原所有者', '旧作品名', '已删除的旧标题', '20 → 28', '不保存正文全文', '不是审计全文快照', '查看关联版本当前正文']) expect(html).toContain(text)
    for (const text of ['恢复此版本', '标星版本', '编辑备注', '删除日志']) expect(html).not.toContain(text)
  })
  it('下架方按管理员或作者展示，不误标为数字操作人 ID', () => {
    const value = log({ action: 'workshop.hide', target_type: 'workshop_item', version_ref: null, version_status: null })
    value.summary.changed_fields = ['hidden_by']
    value.summary.changes = [{ field: 'hidden_by', before: 'author', after: 'admin' }]
    const html = detail(value)
    expect(html).toContain('下架方')
    expect(html).toContain('作者 → 管理员')
    expect(html).not.toContain('下架操作人 ID')
  })
  it('工坊模板正文摘要不误称为章节正文', () => {
    const value = log({ action: 'workshop.publish', target_type: 'workshop_submission', version_ref: null, version_status: null })
    const html = detail(value)
    expect(html).toContain('正文内容')
    expect(html).not.toContain('章节正文')
  })
  it('版本已合并变化时明确提示；版本删除时仍有摘要且不提供正文按钮', () => {
    expect(detail(log({ version_status: 'changed' }))).toContain('关联版本内容已变化')
    const missing = detail(log({ version_status: 'missing' }))
    expect(missing).toContain('关联版本已不可用')
    expect(missing).toContain('20 → 28')
    expect(missing).not.toContain('查看关联版本当前正文')
    expect(detail(log({ version_ref: null, version_status: null }))).toContain('本次操作没有关联业务版本')
  })
  it('主动读取的版本只展示为当前正文，不能视为操作时刻全文', () => {
    const html = render(createElement(AuditLogDetailContent, { canReadVersion: true, log: log({ version_status: 'changed' }), version: { id: 4, chapter_id: 12, seq: 2, source: 'manual', content: '这是独立 API 返回的当前正文', word_count: 16, starred: false, note: '', generation_log_id: null, created_at: '2026-10-31T01:00:00Z', updated_at: '2026-10-31T01:01:00Z' }, versionError: null, versionLoading: false, onLoadVersion: noop }))
    expect(html).toContain('这是独立 API 返回的当前正文')
    expect(html).toContain('以下不是审计保存的正文')
    expect(html).toContain('不能视为本次操作时刻的确定全文')
  })
})

describe('清理二次确认说明', () => {
  it('显示独立日期、时区、预计/保护数量、不可恢复和并发说明，但不渲染令牌', () => {
    const preview: AuditCleanupPreview = { from_at: '2026-10-30T00:00:00Z', to_at: '2026-10-31T00:00:00Z', count: 12, protected_count: 3, expires_at: '2026-10-31T01:10:00Z', preview_token: 'do-not-render-token' }
    const html = render(createElement(AuditCleanupPreviewContent, { preview }))
    for (const text of ['UTC', '（不含）', '12 条', '3 条（保留）', '不可恢复', '不继承列表', '成功和失败', '均受保护', '实际删除数与预计不同']) expect(html).toContain(text)
    expect(html).not.toContain(preview.preview_token)
    expect(html).toContain('overflow-wrap')
  })
})

describe('移动端与状态展示', () => {
  it('375px 布局使用可换行列表，无固定大宽度，快照很长也能断行', () => {
    const html = render(createElement(AuditLogResults, { items: [log({ target_label: '长标题'.repeat(80), action: 'audit.cleanup', result: 'failure' })], loading: false, mobile: true, onDetail: noop }))
    expect(html).toContain('查看详情')
    expect(html).toContain('受保护')
    expect(html).toContain('失败')
    expect(html).toContain('overflow-wrap:anywhere')
    expect(html).not.toContain('<table')
    expect(html).not.toMatch(/(?:^|[;"\s])(?:min-)?width:\s*\d{3,}px/)
  })
  it('桌面表格包含真实操作人、目标、归属、结果和只读入口', () => {
    const html = render(createElement(AuditLogResults, { items: [log()], loading: false, mobile: false, onDetail: noop }))
    for (const text of ['真实操作人', '目标快照', '所属用户', '原所有者', '详情']) expect(html).toContain(text)
    expect(html).toContain('<table')
  })
  it('无结果与失败重试可见；375px 筛选控件没有固定最小宽度', () => {
    state.error = '网络连接失败'
    const html = render(createElement(AdminAuditLogsPage))
    for (const text of ['没有匹配的审计日志', '网络连接失败', '历史操作人用户名', '操作人 ID', '目标 ID', '按日期清理']) expect(html).toContain(text)
    expect(html).toMatch(/重\s*试/)
    expect(html).not.toMatch(/min-width:\s*\d{3,}px/)
    expect(html).not.toContain('100vw')
    expect(html).toContain('type="date"')
  })
  it('加载时有可识别加载状态', () => {
    const html = render(createElement(AuditLogResults, { items: [], loading: true, mobile: true, onDetail: noop }))
    expect(html).toContain('ant-spin-spinning')
  })
})

describe('管理员入口与守卫', () => {
  it('普通用户无法渲染日志页或通过 RequireAdmin 查看内容', () => {
    state.admin = false
    expect(render(createElement(AdminAuditLogsPage))).toContain('需要管理员权限')
    expect(render(createElement(MemoryRouter, null, createElement(RequireAdmin, null, createElement('div', null, '私有审计内容'))))).not.toContain('私有审计内容')
  })
  it('AppLayout 桌面入口和面包屑识别操作日志，普通用户不显示入口', () => {
    state.mobile = false
    const layout = () => render(createElement(MemoryRouter, { initialEntries: ['/admin/audit-logs'] }, createElement(AppLayout)))
    const admin = layout()
    expect(admin).toContain('href="/admin/audit-logs"')
    expect(admin).toContain('aria-current="page"')
    expect(admin).toContain('操作日志')
    state.admin = false
    expect(layout()).not.toContain('href="/admin/audit-logs"')
  })
})
