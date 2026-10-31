import dayjs from 'dayjs'
import { describe, expect, it } from 'vitest'
import type { AdminTaskBrief, AdminTaskError, AdminTaskStatus } from '../api/types'
import { task } from '../components/adminTasks/adminTasksTestFixtures'
import { adminTaskCancellationOutcome, adminTaskCancelReason, adminTaskCurrentDuration, adminTaskDuration, adminTaskError, adminTaskFilterQuery, adminTaskInitialQuery, adminTaskKind, adminTaskStatus, adminTaskTitle, isAdminTaskKey } from './adminTasks'

describe('任务监控安全展示', () => {
  it('未知类型、状态与错误固定回退，原始标题和错误从不参与展示', () => {
    const data = task({ kind: 'unknown', title: 'SECRET INSTRUCTION', status: 'unknown', error_code: 'execution_failed', error_message: 'https://secret?api_key=token' })
    expect(adminTaskTitle(data)).toBe('其他任务 #42')
    expect(adminTaskError(data)).toBe('执行失败')
    expect(adminTaskStatus(data.status).label).toBe('未知状态')
    expect(adminTaskKind('constructor' as AdminTaskBrief['kind'])).toBe('其他任务')
    expect(adminTaskStatus('constructor' as AdminTaskStatus).label).toBe('未知状态')
    expect(adminTaskError({ error_code: 'constructor' as AdminTaskError })).toBe('执行失败')
  })
  it('时长仅当前状态使用，不把null伪装零或暂停计作执行', () => {
    expect(adminTaskDuration(null)).toBe('未记录')
    expect(adminTaskDuration(0)).toBe('0 秒')
    expect(adminTaskDuration(59)).toBe('59 秒')
    expect(adminTaskDuration(61)).toBe('1 分 1 秒')
    expect(adminTaskDuration(3661)).toBe('1 小时 1 分')
    expect(adminTaskDuration(90000)).toBe('1 天 1 小时')
    expect(adminTaskDuration(-1)).toBe('未记录')
    expect(adminTaskCurrentDuration(task({ status: 'queued', queued_seconds: null, running_seconds: 9 }))).toBe('本轮排队：未记录')
    expect(adminTaskCurrentDuration(task({ status: 'paused', running_seconds: 9 }))).toBe('审核等待，不占执行并发')
    expect(adminTaskCurrentDuration(task({ status: 'succeeded' }))).toBe('—')
  })
  it('受理/取消中与真实完成、待审核、中断、失败分别描述', () => {
    expect(adminTaskCancellationOutcome(task({ cancellation_pending: true }))).toContain('尚未确认任务已取消')
    expect(adminTaskCancellationOutcome(task({ status: 'cancelled' }))).toContain('任务已取消；已保存内容')
    expect(adminTaskCancellationOutcome(task({ status: 'succeeded' }))).toContain('已提交内容未撤销')
    expect(adminTaskCancellationOutcome(task({ status: 'paused' }))).toContain('未被取消')
    expect(adminTaskCancellationOutcome(task({ status: 'interrupted' }))).toContain('实际已中断')
    expect(adminTaskCancellationOutcome(task({ status: 'failed' }))).toContain('实际执行失败')
    expect(adminTaskCancellationOutcome(task())).toBeNull()
  })
})
describe('任务监控筛选与取消原因', () => {
  it('日期使用本地日历转UTC半开区间，组合条件保留', () => {
    const query = adminTaskFilterQuery({ q: '  %_作品 ', owner_user_id: 2, project_id: 8, kind: 'agent.turn', status: ['failed', 'interrupted'], fromDate: '2026-10-30', toDate: '2026-10-31' })
    expect(query).toMatchObject({ q: '%_作品', owner_user_id: 2, project_id: 8, status: ['failed', 'interrupted'], page: 1 })
    expect(query.from_at).toBe(dayjs('2026-10-30').startOf('day').toISOString())
    expect(query.to_at).toBe(dayjs('2026-11-01').startOf('day').toISOString())
    expect(() => adminTaskFilterQuery({ fromDate: '2026-10-31' })).toThrow()
    expect(() => adminTaskFilterQuery({ fromDate: '2026-10-32', toDate: '2026-11-01' })).toThrow()
    expect(() => adminTaskFilterQuery({ fromDate: '2026-11-01', toDate: '2026-10-31' })).toThrow()
  })
  it('首页状态深链接和UUID白名单，不把整数编号当异步身份', () => {
    expect(adminTaskInitialQuery(new URLSearchParams('status=failed&status=interrupted&status=bad&status=failed&project_id=8&task_id=-1&owner_user_id=2&kind=agent.turn&sort=queue_longest'))).toMatchObject({ status: ['failed', 'interrupted'], project_id: 8, task_id: undefined, owner_user_id: 2, kind: 'agent.turn', sort: 'queue_longest' })
    expect(adminTaskInitialQuery(new URLSearchParams('kind=secret&sort=secret&owner_user_id=1e2&project_id=9007199254740992'))).toMatchObject({ kind: undefined, sort: 'created_desc', owner_user_id: undefined, project_id: undefined })
    expect(isAdminTaskKey('00000000-0000-4000-8000-000000000001')).toBe(true)
    for (const value of [null, '', '42', '../tasks/42', 'NOT-UUID']) expect(isAdminTaskKey(value)).toBe(false)
  })
  it('trim后1–200码点，拒绝控制字符但不错误拒绝200个emoji', () => {
    expect(adminTaskCancelReason('  人工排查  ')).toBe('人工排查')
    expect(adminTaskCancelReason('🐈'.repeat(200))).toHaveLength(400)
    for (const value of ['', ' \n ', '🐈'.repeat(201), 'x\ny', 'x\u0000y', 'x\u200by']) expect(() => adminTaskCancelReason(value)).toThrow('1–200')
  })
})
