import { describe, expect, it, vi } from 'vitest'
import { SeriesAuthorizationScope, validProjectSeriesAccess, validSeriesAccess } from './seriesAuthorization'

describe('系列邀请授权代次', () => {
  it('普通刷新保留草稿，撤销后重新邀请使用新版本会使旧请求立即失效', () => {
    const scope = new SeriesAuthorizationScope()
    scope.adopt('a'.repeat(64))
    const original = scope.capture()
    const listener = vi.fn()
    const unsubscribe = scope.subscribe(listener)
    scope.adopt('a'.repeat(64))
    expect(scope.current(original)).toBe(true)
    expect(listener).not.toHaveBeenCalled()
    scope.adopt('b'.repeat(64))
    expect(scope.current(original)).toBe(false)
    expect(listener).toHaveBeenCalledOnce()
    unsubscribe()
  })

  it('作者的 null 是明确身份，不能将缺少授权版本当成作者', () => {
    const scope = new SeriesAuthorizationScope()
    scope.adopt(null)
    const owner = scope.capture()
    scope.adopt('c'.repeat(64))
    expect(scope.current(owner)).toBe(false)
  })

  it('作品内只接受对应身份的授权版本，异常或旧响应失败关闭', () => {
    expect(validProjectSeriesAccess('owner', null)).toBe(true)
    expect(validProjectSeriesAccess('editor', 'a'.repeat(64))).toBe(true)
    expect(validProjectSeriesAccess('viewer', 'b'.repeat(64))).toBe(true)
    expect(validProjectSeriesAccess('editor', null)).toBe(false)
    expect(validProjectSeriesAccess('owner', undefined)).toBe(false)
    expect(validProjectSeriesAccess(null, 'a'.repeat(64))).toBe(false)
    expect(validSeriesAccess({ is_owner: false, authorization_version: 'x' })).toBe(false)
  })
})
