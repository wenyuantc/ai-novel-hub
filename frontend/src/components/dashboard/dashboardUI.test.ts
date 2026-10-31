import { App } from 'antd'
import { createElement, type ReactElement } from 'react'
import { renderToString } from 'react-dom/server'
import { StaticRouter } from 'react-router-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AdminDashboardHistory, AdminDashboardOverview, AdminDashboardTasks, User } from '../../api/types'
import AdminDashboardPage from '../../pages/AdminDashboardPage'
import WorkshopPage from '../../pages/WorkshopPage'
import { useAuthStore } from '../../stores/auth'
import AppLayout from '../AppLayout'
import DashboardHistory from './DashboardHistory'
import { DashboardController } from './dashboardController'
import type { useAdminDashboard } from './useAdminDashboard'

const chartMeasure = vi.hoisted(() => ({ width: 0 }))
vi.mock('./useAdminDashboard', () => ({ useAdminDashboard: () => view }))
vi.mock('../../hooks/useElementWidth', () => ({ useElementWidth: () => [{ current: null }, chartMeasure.width] as const }))
vi.mock('../../stores/auth', async (original) => {
  const actual = await original<typeof import('../../stores/auth')>()
  const hook = (selector: (state: ReturnType<typeof actual.useAuthStore.getState>) => unknown) => selector(actual.useAuthStore.getState())
  return { ...actual, useAuthStore: Object.assign(hook, actual.useAuthStore) }
})

const admin: User = { id: 1, username: 'root', is_admin: true, created_at: '' }
const meta = { as_of: '2026-10-31T10:00:00Z', tracking_since: '2026-10-28T08:00:00Z', timezone: 'Asia/Shanghai' as const }
const overview: AdminDashboardOverview = { ...meta, users: 12, projects: 7, chapters: 30, total_words: 500, pending_submissions: 2 }
const tasks: AdminDashboardTasks = { ...meta, baseline_tasks: 10, new_tasks: 5, queued: 0, running: 1, paused: 0, succeeded: 2, failed: 1, cancelled: 0, interrupted: 0, failed_24h: 1, interrupted_24h: 0, removed_unfinished: 1 }
const history: AdminDashboardHistory = {
  ...meta, days: 7, start_date: '2026-10-25', end_date: '2026-10-31',
  points: [
    { day: '2026-10-28', captured_at: '2026-10-28T12:00:00Z', totals: { users: 0, projects: 0, chapters: 0, total_words: 0, pending_submissions: 0 }, tasks: { created: 0, succeeded: 0, failed: 0, cancelled: 0, interrupted: 0 }, partial: true },
    { day: '2026-10-29', captured_at: null, totals: null, tasks: null, partial: true },
    { day: '2026-10-30', captured_at: '2026-10-30T12:00:00Z', totals: { users: 4, projects: 0, chapters: 0, total_words: 0, pending_submissions: 0 }, tasks: { created: 2, succeeded: 0, failed: 1, cancelled: 0, interrupted: 0 }, partial: false },
    { day: '2026-10-31', captured_at: meta.as_of, totals: overview, tasks: { created: 1, succeeded: 2, failed: 0, cancelled: 0, interrupted: 0 }, partial: true },
  ],
  comparison: { from_day: '2026-10-25', to_day: '2026-10-31', users: null, projects: null, chapters: null, total_words: null },
}
function initialView(): ReturnType<typeof useAdminDashboard> {
  const controller = new DashboardController(1)
  return { allowed: true, controller, state: { ...controller.getSnapshot(),
    overview: { data: overview, loading: false, error: null }, tasks: { data: tasks, loading: false, error: null }, history: { data: history, loading: false, error: null },
  } }
}
let view = initialView()
function render(element: ReactElement, path = '/admin/dashboard') {
  return renderToString(createElement(App, null, createElement(StaticRouter, { location: path }, element))).replaceAll('<!-- -->', '')
}
afterEach(() => { view = initialView(); chartMeasure.width = 0; useAuthStore.setState({ user: null, status: 'anonymous' }) })

describe('管理员首页渲染与权限入口', () => {
  it('完整展示已确认统计口径和真实管理入口，区分累计统计与现存监控入口', () => {
    const html = render(createElement(AdminDashboardPage))
    expect(html).toContain('所有账号，含管理员与禁用账号')
    expect(html).toContain('作品总数（含回收站）')
    expect(html).toContain('正文总字数（含回收站）')
    expect(html).toContain('初始化存量 10 个')
    expect(html).toContain('最近 24 小时失败')
    for (const path of ['/admin/users', '/admin/projects', '/admin/recycle-bin', '/admin/audit-logs', '/workshop?tab=review']) expect(html).toContain(`href="${path}"`)
    expect(html).toContain('href="/admin/tasks"')
    expect(html).toContain('href="/admin/tasks?status=failed"')
    expect(html).toContain('href="/admin/tasks?status=interrupted"')
    expect(html).toContain('现存列表数量不保证与累计结果')
    expect(html).toContain('历史统计时间范围')
    expect(html).toContain('每60秒自动刷新')
  })

  it('区块首次失败不显示假的零，刷新失败明确标记旧数据', () => {
    view.state.overview = { data: null, loading: false, error: '查询失败' }
    const failed = render(createElement(AdminDashboardPage))
    expect(failed).toContain('尚未取得有效数据，不代表数量为零')
    expect(failed).not.toContain('用户总数')
    expect(failed).toContain('全站任务统计')
    view.state.overview = { data: overview, loading: false, error: '网络失败' }
    const stale = render(createElement(AdminDashboardPage))
    expect(stale).toContain('上次成功数据（可能已过期）')
    expect(stale).toContain('当前保留上次成功值')
    expect(stale).toContain('用户总数')
  })

  it('权限失效隐藏原数据，普通用户没有管理员侧栏入口', () => {
    view.state.denied = true
    const denied = render(createElement(AdminDashboardPage))
    expect(denied).toContain('管理统计访问已停止')
    expect(denied).not.toContain('作品总数')
    useAuthStore.setState({ user: admin, status: 'authenticated' })
    expect(render(createElement(AppLayout), '/projects')).toContain('href="/admin/dashboard"')
    expect(render(createElement(AppLayout))).toContain('管理后台')
    useAuthStore.setState({ user: { ...admin, is_admin: false } })
    expect(render(createElement(AppLayout), '/projects')).not.toContain('href="/admin/dashboard"')
  })

  it('图表提供语义说明与可展开数据表，缺失基准不显示假净增', () => {
    const html = render(createElement(DashboardHistory, { history }))
    expect(html).toContain('role="img"')
    expect(html).toContain('用户总量趋势')
    expect(html).toContain('每日任务新建与结束结果')
    expect(html).toContain('查看每日数据表')
    expect(html).toContain('无可比数据')
    expect(html).toContain('缺口不补零、不连线')
    expect(html).toContain('不要求同日相等')
  })

  it('折线和任务图按实测容器宽度设置坐标视口，窄屏轴文字仍为12像素', () => {
    for (const width of [288, 900]) {
      chartMeasure.width = width
      const html = render(createElement(DashboardHistory, { history }))
      expect(html.match(new RegExp(`viewBox="0 0 ${width} 210"`, 'g'))).toHaveLength(2)
      expect(html.match(/<g font-size="12"/g)).toHaveLength(2)
      expect(html.match(/height="210"/g)).toHaveLength(2)
      expect(html).not.toContain('viewBox="0 0 600 210"')
    }
  })

  it('管理员审核深链接落在审核提交，普通用户及非法页签不能进入审核', () => {
    useAuthStore.setState({ user: admin, status: 'authenticated' })
    expect(render(createElement(WorkshopPage), '/workshop?tab=review')).toContain('审核提交')
    expect(render(createElement(WorkshopPage), '/workshop?tab=unknown')).not.toContain('审核提交')
    useAuthStore.setState({ user: { ...admin, is_admin: false } })
    expect(render(createElement(WorkshopPage), '/workshop?tab=review')).not.toContain('审核提交')
  })
})
