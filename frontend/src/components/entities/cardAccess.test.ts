import { describe, expect, it } from 'vitest'
import { ApiError } from '../../api/client'
import { cardAccess, cardNeedsAccessRefresh } from './cardAccess'

describe('卡片权限与 AI 能力分离', () => {
  it('作者、协作者和管理员交集与卡片端点一致', () => {
    expect(cardAccess('owner', false, true, true)).toEqual({ canPreview: true, canImport: true })
    expect(cardAccess('viewer', true, true, false)).toEqual({ canPreview: true, canImport: false })
    expect(cardAccess('editor', true, true, true)).toEqual({ canPreview: true, canImport: true })
    expect(cardAccess('admin', false, true, true)).toEqual({ canPreview: false, canImport: false })
    expect(cardAccess('viewer', true, true, true)).toEqual({ canPreview: true, canImport: true })
    expect(cardAccess(null, false, false, false)).toEqual({ canPreview: false, canImport: false })
  })

  it('只有当前作用域收到明确 403/404 才刷新权限，旧请求和普通冲突不得影响新会话', () => {
    expect(cardNeedsAccessRefresh(new ApiError(403, '禁止'), true)).toBe(true)
    expect(cardNeedsAccessRefresh(new ApiError(404, '不存在'), true)).toBe(true)
    expect(cardNeedsAccessRefresh(new ApiError(404, '旧请求'), false)).toBe(false)
    expect(cardNeedsAccessRefresh(new ApiError(409, '版本冲突'), true)).toBe(false)
    expect(cardNeedsAccessRefresh(new Error('断网'), true)).toBe(false)
  })
})
