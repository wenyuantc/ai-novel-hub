import { afterEach, describe, expect, it, vi } from 'vitest'
import { adminBackupsApi, startBackupDownload } from './adminBackups'
import { ApiError, setUnauthorizedHandler } from './client'

function documentSpy() {
  const click = vi.fn(), remove = vi.fn(), append = vi.fn()
  const link = { href: '', download: '', rel: '', click, remove }
  const create = vi.fn().mockReturnValue(link)
  vi.stubGlobal('document', { createElement: create, body: { appendChild: append } })
  return { create, click, remove, append, link }
}
afterEach(() => { vi.unstubAllGlobals(); setUnauthorizedHandler(null) })

describe('管理员整库备份唯一传输边界', () => {
  it('cookie、路径、分页与PATCH秘密语义原样传输，不读回秘密', async () => {
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(new Response('{}', { status: 200 })))
    vi.stubGlobal('fetch', fetch)
    await adminBackupsApi.list({ status: 'partial', source: 'manual', page: 2, page_size: 10 })
    expect(fetch).toHaveBeenLastCalledWith('/api/v1/admin/backups/runs?status=partial&source=manual&page=2&page_size=10', expect.objectContaining({ credentials: 'include' }))
    await adminBackupsApi.update({ revision: 3, changes: { encryption_enabled: false, smtp_password: null } })
    expect(fetch).toHaveBeenLastCalledWith('/api/v1/admin/backups/settings', expect.objectContaining({ method: 'PATCH', credentials: 'include', body: '{"revision":3,"changes":{"encryption_enabled":false,"smtp_password":null}}' }))
    await adminBackupsApi.run({ request_id: '70fdca40-0ce8-4e10-8cb8-98e35402a970', settings_revision: 3 })
    expect(fetch).toHaveBeenLastCalledWith('/api/v1/admin/backups/runs', expect.objectContaining({ method: 'POST', body: '{"request_id":"70fdca40-0ce8-4e10-8cb8-98e35402a970","settings_revision":3}' }))
  })
  it('下载只预检HEAD，不读取Blob，原生同源附件GET不含token或公开S3地址', async () => {
    const dom = documentSpy()
    const response = new Response(null, { status: 200 })
    const blob = vi.spyOn(response, 'blob')
    const fetch = vi.fn().mockResolvedValue(response); vi.stubGlobal('fetch', fetch)
    expect(await startBackupDownload(7, 's3', () => true)).toBe(true)
    expect(fetch).toHaveBeenCalledExactlyOnceWith('/api/v1/admin/backups/runs/7/download?destination=s3', expect.objectContaining({ method: 'HEAD', credentials: 'include' }))
    expect(blob).not.toHaveBeenCalled()
    expect(dom.link.href).toBe('/api/v1/admin/backups/runs/7/download?destination=s3')
    expect(dom.append).toHaveBeenCalledOnce(); expect(dom.click).toHaveBeenCalledOnce(); expect(dom.remove).toHaveBeenCalledOnce()
  })
  it('HEAD期间撤权或请求中止，不发出附件GET', async () => {
    const dom = documentSpy(); let allowed = true
    let resolve!: (response: Response) => void
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(new Promise<Response>((res) => { resolve = res })))
    const first = startBackupDownload(7, 'local', () => allowed)
    allowed = false; resolve(new Response(null, { status: 200 }))
    expect(await first).toBe(false); expect(dom.create).not.toHaveBeenCalled()
    const abort = new AbortController(); abort.abort()
    expect(await startBackupDownload(7, 'local', () => true, abort.signal)).toBe(false)
    expect(await startBackupDownload(NaN, 'local', () => true)).toBe(false)
  })
  it('预检已过期文件不触发下载，错误与401仍走统一处理', async () => {
    const dom = documentSpy(); const unauthorized = vi.fn(); setUnauthorizedHandler(unauthorized)
    const fetch = vi.fn().mockResolvedValueOnce(new Response(null, { status: 410 })).mockResolvedValueOnce(new Response(null, { status: 401 }))
    vi.stubGlobal('fetch', fetch)
    await expect(startBackupDownload(7, 'local', () => true)).rejects.toMatchObject({ status: 410 })
    await expect(startBackupDownload(7, 'local', () => true)).rejects.toBeInstanceOf(ApiError)
    expect(unauthorized).toHaveBeenCalledOnce(); expect(dom.click).not.toHaveBeenCalled()
  })
})
