import { createElement, type ReactNode } from 'react'
import { renderToString } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Task } from '../api/types'
import { useAuthStore } from '../stores/auth'
import { snapshotTaskAction } from '../utils/taskSnapshot'
import TaskActionConfirm from './TaskActionConfirm'

const view = vi.hoisted(() => ({ open: null as null | ((open: boolean) => void), confirm: null as null | (() => unknown), warning: vi.fn() }))
vi.mock('antd', async (original) => {
  const actual = await original<typeof import('antd')>()
  return { ...actual, App: Object.assign(({ children }: { children: ReactNode }) => children, { useApp: () => ({ message: { warning: view.warning } }) }),
    Popconfirm: ({ children, onOpenChange, onConfirm }: { children: ReactNode; onOpenChange: (open: boolean) => void; onConfirm: () => unknown }) => {
      view.open = onOpenChange; view.confirm = onConfirm
      return children
    } }
})
afterEach(() => { useAuthStore.getState().clear(); view.open = null; view.confirm = null; view.warning.mockClear() })

describe('任务确认快照', () => {
  it('任务 ID 复用或审核轮次改变时仍保留弹窗打开时的身份', () => {
    const shown = { id: 7, task_key: '00000000-0000-4000-8000-000000000007', rich_review: {
      task_key: '00000000-0000-4000-8000-000000000007', run_key: 'original-run', token: 'original-token', chapter_id: 3, phase: 'awaiting_review',
    } } as Task
    const captured = snapshotTaskAction(shown)
    shown.id = 8
    shown.task_key = '00000000-0000-4000-8000-000000000008'
    if (shown.rich_review) { shown.rich_review.run_key = 'new-run'; shown.rich_review.token = 'new-token' }
    expect(captured).toMatchObject({ id: 7, task_key: '00000000-0000-4000-8000-000000000007',
      rich_review: { run_key: 'original-run', token: 'original-token' } })
  })

  it('弹窗打开后同账号退出再登录，旧确认不能发出任务操作', () => {
    const user = { id: 1, username: '作者', created_at: '', is_admin: false, is_super_admin: false,
      effective_permissions: [], roles: [], authorization_revision: 1 }
    useAuthStore.setState({ status: 'authenticated', user })
    const task = { id: 7, task_key: '00000000-0000-4000-8000-000000000007', project_id: null } as Task
    const action = vi.fn()
    renderToString(createElement(TaskActionConfirm, { task, title: '取消任务？', onConfirm: action }, createElement('button')))
    view.open?.(true)
    useAuthStore.getState().clear()
    useAuthStore.setState({ status: 'authenticated', user })
    view.confirm?.()
    expect(action).not.toHaveBeenCalled()
    expect(view.warning).toHaveBeenCalledOnce()
  })
})
