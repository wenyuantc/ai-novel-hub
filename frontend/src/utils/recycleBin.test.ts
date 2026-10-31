import { describe, expect, it } from 'vitest'
import { canPurgeProject, canRestoreProject, createRecycleRequestGate, PROJECT_DELETE_NOTICE, recycleValidPage } from './recycleBin'

describe('作品回收站交互规则', () => {
  it('只允许服务器认定可恢复且未到期的记录；恰到期即拒绝', () => {
    const project = { restorable: true, expires_at: '2026-11-30T08:00:00Z' }
    const expiry = Date.parse(project.expires_at)
    expect(canRestoreProject(project, expiry - 1)).toBe(true)
    expect(canRestoreProject(project, expiry)).toBe(false)
    expect(canRestoreProject(project, expiry + 1)).toBe(false)
    expect(canRestoreProject({ ...project, restorable: false }, expiry - 1)).toBe(false)
    expect(canRestoreProject({ ...project, expires_at: 'bad-time' }, expiry - 1)).toBe(false)
    expect(canRestoreProject({ ...project, expires_at: '2026-11-30T16:00:00+08:00' }, expiry - 1)).toBe(true)
  })
  it('不可逆清理必须逐字输入完整名称，不 trim 或忽略大小写', () => {
    expect(canPurgeProject('星河A', '星河A')).toBe(true)
    for (const input of ['', '星河', '星河a', ' 星河A', '星河A ']) expect(canPurgeProject(input, '星河A')).toBe(false)
    expect(canPurgeProject('', '')).toBe(false)
  })
  it('末页变化回到最后有效页，空列表仍显示第一页', () => {
    expect(recycleValidPage(4, 21, 20)).toBe(2)
    expect(recycleValidPage(4, 0, 20)).toBe(1)
    expect(recycleValidPage(2, 80, 20)).toBe(2)
  })
  it('关闭/换目标后迟到响应失效，后续请求代次独立', async () => {
    const gate = createRecycleRequestGate()
    let resolve!: () => void
    const old = new Promise<void>((done) => { resolve = done })
    const ticket = gate.begin()
    let changed = false
    const completion = old.then(() => { if (gate.isCurrent(ticket)) changed = true })
    gate.invalidate()
    const next = gate.begin()
    resolve()
    await completion
    expect(changed).toBe(false)
    expect(gate.isCurrent(next)).toBe(true)
    gate.invalidate()
    expect(gate.isCurrent(next)).toBe(false)
  })
  it('所有普通删除入口共享保留、管理员恢复和任务失败说明', () => {
    for (const text of ['30 天', '仅管理员', '自动取消', '失败或超时', '作品不会删除', '不会自动恢复']) expect(PROJECT_DELETE_NOTICE).toContain(text)
  })
})
