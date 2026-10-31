import { App } from 'antd'
import { createElement, type ReactElement } from 'react'
import { renderToString } from 'react-dom/server'
import { StaticRouter } from 'react-router-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { User } from '../../api/types'
import AdminTasksPage from '../../pages/AdminTasksPage'
import { useAuthStore } from '../../stores/auth'
import AppLayout from '../AppLayout'
import { AdminTaskDetailContent, AdminTaskResults } from './AdminTaskViews'
import { AdminTasksController } from './adminTasksController'
import { TASK_KEY, detail, list, task } from './adminTasksTestFixtures'
import type { useAdminTasks } from './useAdminTasks'
import cancelSource from './AdminTaskCancelModal.tsx?raw'
import pageSource from '../../pages/AdminTasksPage.tsx?raw'
import routerSource from '../../router.tsx?raw'

const viewport = vi.hoisted(() => ({ mobile: false }))
vi.mock('./useAdminTasks', () => ({ useAdminTasks: () => view }))
vi.mock('../../hooks/useIsMobile', () => ({ useIsMobile: () => viewport.mobile, useModalWidth: (width: number | string) => viewport.mobile ? 'calc(100vw - 24px)' : width }))
vi.mock('../../stores/auth', async (original) => {
  const actual = await original<typeof import('../../stores/auth')>()
  const hook = (selector: (state: ReturnType<typeof actual.useAuthStore.getState>) => unknown) => selector(actual.useAuthStore.getState())
  return { ...actual, useAuthStore: Object.assign(hook, actual.useAuthStore) }
})
const admin: User = { id: 1, username: '当前管理员', is_admin: true, created_at: '' }
function initialView(): ReturnType<typeof useAdminTasks> {
  const controller = new AdminTasksController(1)
  return { controller, state: { ...controller.getSnapshot(), list: { data: list(), loading: false, error: null } } }
}
let view = initialView()
const noop = () => {}
function render(element: ReactElement, path = '/admin/tasks') {
  return renderToString(createElement(App, null, createElement(StaticRouter, { location: path }, element))).replaceAll('<!-- -->', '')
}
afterEach(() => { viewport.mobile = false; view = initialView(); useAuthStore.setState({ user: null, status: 'anonymous' }) })

describe('安全任务元数据结构', () => {
  it('专用详情不渲染原始title/error/input/output/私有配置，仅白名单类型与错误', () => {
    const unsafe = { ...detail({ title: 'SECRET_INSTRUCTION', error_code: 'execution_failed', error_message: 'SECRET_KEY https://private' }), input: 'SECRET_INPUT', output_text: 'SECRET_BODY', checkpoint: { plugin: 'SECRET_PLUGIN' }, error: 'SECRET_ORIGINAL_ERROR' }
    const html = render(createElement(AdminTaskDetailContent, { task: unsafe }))
    for (const text of ['项目智能体 #42', '作者甲', '测试作品', '调用工具', '执行失败', '当前轮次时长', '不提供生成正文', '已提交内容或已执行的效果']) expect(html).toContain(text)
    for (const secret of ['SECRET_INSTRUCTION', 'SECRET_KEY', 'SECRET_INPUT', 'SECRET_BODY', 'SECRET_PLUGIN', 'SECRET_ORIGINAL_ERROR']) expect(html).not.toContain(secret)
    for (const action of ['读取正文', '继续此任务', '重试此任务', '接管此任务']) expect(html).not.toContain(action)
    expect(html).toContain(TASK_KEY)
  })
  it('旧任务本轮时间未记录，待审核不占并发，回收站/删除协调明确标识', () => {
    const html = render(createElement(AdminTaskDetailContent, { task: detail({ status: 'queued', queued_at: null, queued_seconds: null, started_at: null, project_lifecycle: 'recycled' }) }))
    expect(html).toContain('本轮排队：未记录')
    expect(html).toContain('回收站')
    expect(html).toContain('旧记录缺少可靠起点')
    const paused = render(createElement(AdminTaskDetailContent, { task: detail({ status: 'paused', project_lifecycle: 'deleting' }) }))
    expect(paused).toContain('不占执行并发')
    expect(paused).toContain('删除协调中')
  })
  it('桌面紧凑表格操作固定右侧，移动卡片不依赖横向大表格', () => {
    const items = [task({ owner_username: '长用户名'.repeat(40), project_title: '长作品'.repeat(80), cancellation_pending: true }), task({ id: 43, task_key: null, status: 'unknown', kind: 'unknown', can_cancel: false })]
    const desktop = render(createElement(AdminTaskResults, { items, loading: false, mobile: false, onDetail: noop, onCancel: noop }))
    expect(desktop).toContain('<table')
    expect(desktop).toContain('ant-table-cell-fix-right')
    expect(desktop).toContain('取消中')
    const mobile = render(createElement(AdminTaskResults, { items, loading: false, mobile: true, onDetail: noop, onCancel: noop }))
    expect(mobile).not.toContain('<table')
    expect(mobile).toContain('admin-task-mobile-item')
    expect(mobile).toContain('其他任务 #43')
    expect(mobile).toContain('未知状态')
    expect(mobile).toContain('未记录执行标识')
    expect(mobile).not.toMatch(/(?:min-)?width:\s*\d{3,}px/)
    expect(mobile).not.toContain('申请取消任务 #42')
  })
  it('待核对的取消按钮disabled，避免网络不确定期间重新请求', () => {
    const html = render(createElement(AdminTaskResults, { items: [task()], loading: false, mobile: true, onDetail: noop, onCancel: noop, blockedCancelKey: TASK_KEY }))
    expect(html).toContain('核对中')
    expect(html).toMatch(/aria-label="申请取消任务 #42"[^>]*disabled=""/)
  })
})

describe('管理员监控页面口径和入口', () => {
  it('显示当前筛选所有页计数、5秒刷新、时区筛选和安全空值说明', () => {
    useAuthStore.setState({ user: admin, status: 'authenticated' })
    const html = render(createElement(AdminTasksPage))
    for (const text of ['全站任务监控', '当前筛选下的现存任务', '共 1 个', '运行中 1', '不是历史累计', '5 秒自动刷新', '页面隐藏时暂停', '用户名 / 作品标题', '任务状态（可多选）', '创建结束日', 'UTC 半开区间', '起点缺失显示', '时长排序优先对应状态']) expect(html).toContain(text)
    expect(html).toContain('href="/admin/dashboard"')
    expect(html).toContain('每5秒自动刷新任务')
    expect(html).toContain('type="date"')
    expect(html).not.toContain('读取任务输出')
  })
  it('未加载与失败不显示假的零，旧数据失败明确过期及上次成功时间', () => {
    useAuthStore.setState({ user: admin, status: 'authenticated' })
    view.state.list = { data: null, loading: false, error: '网络中断' }
    const unloaded = render(createElement(AdminTasksPage))
    expect(unloaded).toContain('尚未取得有效明细，不代表数量为零')
    expect(unloaded).not.toContain('共 0 个')
    expect(unloaded).not.toContain('没有匹配的现存任务')
    view.state.list = { data: list(), loading: false, error: '网络中断' }
    const stale = render(createElement(AdminTasksPage))
    expect(stale).toContain('上次成功数据（可能已过期）')
    expect(stale).toContain('共 1 个')
  })
  it('375px使用可折叠筛选和换行卡片，移动标题/操作上下排列且排序不挤占卡片标题', () => {
    viewport.mobile = true
    useAuthStore.setState({ user: admin, status: 'authenticated' })
    const html = render(createElement(AdminTasksPage))
    expect(html).toContain('展开 / 收起筛选条件')
    expect(html).toContain('排序方式')
    expect(html).toContain('admin-task-mobile-item')
    expect(html).not.toContain('<table')
    expect(html).not.toMatch(/(?:min-)?width:\s*\d{3,}px/)
    expect(pageSource).toContain('extra={!mobile && sortSelect}')
  })
  it('降权隐藏全部明细与弹窗，管理员菜单和面包屑同一入口，路由RequireAdmin懒加载', () => {
    useAuthStore.setState({ user: admin, status: 'authenticated' })
    view.state.denied = true
    const denied = render(createElement(AdminTasksPage))
    expect(denied).toContain('任务监控访问已停止')
    expect(denied).not.toContain('作者甲')
    expect(denied).not.toContain('取消原因')
    const layout = render(createElement(AppLayout))
    expect(layout).toContain('href="/admin/tasks"')
    expect(layout).toContain('全站任务监控')
    expect(layout).toContain('管理后台')
    useAuthStore.setState({ user: { ...admin, is_admin: false } })
    expect(render(createElement(AdminTasksPage))).toContain('需要管理员权限')
    expect(render(createElement(AppLayout))).not.toContain('href="/admin/tasks"')
    expect(routerSource).toMatch(/path: 'admin\/tasks',[\s\S]*?element: <RequireAdmin><Outlet \/><\/RequireAdmin>,[\s\S]*?import\('\.\/pages\/AdminTasksPage'\)/)
  })
  it('非法详情深链接不退回整数定位；页面不引用个人任务store或输出组件', () => {
    useAuthStore.setState({ user: admin, status: 'authenticated' })
    expect(render(createElement(AdminTasksPage), '/admin/tasks?task_key=42')).toContain('任务执行标识无效')
    for (const forbidden of ['taskApi', 'useTasksStore', 'TaskPanel', 'TaskOutput', 'subscribeTask']) expect(pageSource).not.toContain(forbidden)
  })
  it('取消表单原生输入、二次确认与键盘焦点有明确结构，不以SSR冒充交互验收', () => {
    for (const expected of ['Input.TextArea autoFocus', 'Checkbox autoFocus', '下一步：核对并确认', '明确确认并提交取消请求', '1–200 字', 'disabled={!checked || !validTarget || submitting}', '已保存内容和已执行效果保留']) expect(cancelSource).toContain(expected)
  })
})
