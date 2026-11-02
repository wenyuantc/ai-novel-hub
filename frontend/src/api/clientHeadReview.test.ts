import { afterEach, describe, expect, it, vi } from 'vitest'
import { startBackupDownload } from './adminBackups'
import { invalidateRequestGeneration, rawRequest, setPermissionDeniedHandler, setUnauthorizedHandler } from './client'

afterEach(() => {
  vi.unstubAllGlobals()
  setPermissionDeniedHandler(null)
  setUnauthorizedHandler(null)
})

describe('HEAD附件预检的能力刷新边界', () => {
  it('同actor被撤销下载能力，空正文403也立即刷新授权且不退出或重发', async () => {
    const refresh = vi.fn(), logout = vi.fn()
    setPermissionDeniedHandler(refresh)
    setUnauthorizedHandler(logout)
    const fetch = vi.fn().mockResolvedValue(new Response(null, { status: 403 }))
    vi.stubGlobal('fetch', fetch)
    await expect(startBackupDownload(8, 'local', () => true)).rejects.toMatchObject({ status: 403 })
    expect(refresh).toHaveBeenCalledOnce()
    expect(logout).not.toHaveBeenCalled()
    expect(fetch).toHaveBeenCalledOnce()
  })

  it('GET403继续要求permission_denied代码，功能关闭不触发能力刷新', async () => {
    const refresh = vi.fn()
    setPermissionDeniedHandler(refresh)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(null, { status: 403 }))
      .mockResolvedValueOnce(new Response('{"detail":"权限不足","code":"permission_denied"}', { status: 403 })))
    await expect(rawRequest('/admin/backups')).rejects.toMatchObject({ status: 403 })
    expect(refresh).not.toHaveBeenCalled()
    await expect(rawRequest('/admin/backups')).rejects.toMatchObject({ status: 403 })
    expect(refresh).toHaveBeenCalledOnce()
  })

  it('旧session的HEAD403不能刷新后来登录的session', async () => {
    const refresh = vi.fn()
    setPermissionDeniedHandler(refresh)
    let resolve!: (response: Response) => void
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(new Promise<Response>((res) => { resolve = res })))
    const old = rawRequest('/admin/backups/runs/8/download', { method: 'HEAD' })
    invalidateRequestGeneration()
    resolve(new Response(null, { status: 403 }))
    await expect(old).rejects.toMatchObject({ status: 403 })
    expect(refresh).not.toHaveBeenCalled()
  })
})
