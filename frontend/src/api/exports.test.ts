import { afterEach, describe, expect, it, vi } from 'vitest'
import { downloadDocument, downloadEpub, type DocumentExportRequest } from './exports'
import { setPermissionDeniedHandler, setProjectFailureCapture, setUnauthorizedHandler } from './client'
import { noteApi } from './notes'

const body: DocumentExportRequest = {
  format: 'docx', story: { scope: 'selected', unit: 'scene', content: 'both', ids: [8] },
  materials: { settings: [], conversation_ids: [], note_ids: [] },
}

afterEach(() => {
  setUnauthorizedHandler(null)
  setPermissionDeniedHandler(null)
  setProjectFailureCapture(null)
  vi.unstubAllGlobals()
})

describe('组合文档下载', () => {
  it('以 POST 发送准确选择并保存服务端指定文件名', async () => {
    let request: { input: string; init?: RequestInit } | undefined
    vi.stubGlobal('fetch', async (input: string, init?: RequestInit) => {
      request = { input, init }
      return new Response('document', { headers: { 'Content-Disposition': "attachment; filename*=UTF-8''%E4%BD%9C%E5%93%81.docx" } })
    })
    const saved: string[] = []
    const result = await downloadDocument(42, body, { isCurrent: () => true, save: (_, name) => saved.push(name) })
    expect(request?.input).toBe('/api/v1/projects/42/export-document')
    expect(request?.init?.method).toBe('POST')
    expect(request?.init?.credentials).toBe('include')
    expect(JSON.parse(String(request?.init?.body))).toEqual(body)
    expect(saved).toEqual(['作品.docx'])
    expect(result?.filename).toBe('作品.docx')
  })

  it('blob 返回前作用域失效不会触发浏览器下载', async () => {
    let release!: (blob: Blob) => void
    const pendingBlob = new Promise<Blob>((resolve) => { release = resolve })
    vi.stubGlobal('fetch', async () => ({ ok: true, headers: new Headers(), blob: () => pendingBlob }))
    let current = true
    const saved: string[] = []
    const result = downloadDocument(42, body, { isCurrent: () => current, save: (_, name) => saved.push(name) })
    await Promise.resolve()
    current = false
    release(new Blob(['private']))
    expect(await result).toBeNull()
    expect(saved).toEqual([])
  })

  it('关闭弹窗后的旧 401 不会触发全局登出', async () => {
    let release!: (body: string) => void
    const pendingText = new Promise<string>((resolve) => { release = resolve })
    vi.stubGlobal('fetch', async () => ({ ok: false, status: 401, text: () => pendingText }))
    const logout = vi.fn()
    setUnauthorizedHandler(logout)
    let current = true
    const result = downloadDocument(42, body, { isCurrent: () => current }).catch(() => null)
    await Promise.resolve()
    current = false
    release('{"detail":"未登录"}')
    await result
    expect(logout).not.toHaveBeenCalled()
  })

  it('当前弹窗收到 401 仍执行原有登出处理', async () => {
    vi.stubGlobal('fetch', async () => new Response('{"detail":"未登录"}', { status: 401 }))
    const logout = vi.fn()
    setUnauthorizedHandler(logout)
    await downloadDocument(42, body, { isCurrent: () => true }).catch(() => null)
    expect(logout).toHaveBeenCalledTimes(1)
  })

  it('EPUB 延续原 GET 参数及空选导出全部', async () => {
    let path = ''
    let init: RequestInit | undefined
    vi.stubGlobal('fetch', async (input: string, options?: RequestInit) => {
      path = input
      init = options
      return new Response('epub', { headers: { 'Content-Disposition': 'attachment; filename="novel.epub"' } })
    })
    const saved: string[] = []
    await downloadEpub(42, { chapterIds: [], author: '作者', includeSynopsis: false },
      { isCurrent: () => true, save: (_, name) => saved.push(name) })
    const url = new URL(path, 'http://localhost')
    expect(url.pathname).toBe('/api/v1/projects/42/export')
    expect(url.searchParams.get('format')).toBe('epub')
    expect(url.searchParams.get('author')).toBe('作者')
    expect(url.searchParams.get('include_synopsis')).toBe('false')
    expect(url.searchParams.has('chapter_ids')).toBe(false)
    expect(init?.method).toBe('GET')
    expect(saved).toEqual(['novel.epub'])
  })

  it('关闭并重开同一作品弹窗后旧 403 不会刷新权限', async () => {
    let release!: (body: string) => void
    const pendingText = new Promise<string>((resolve) => { release = resolve })
    vi.stubGlobal('fetch', async () => ({ ok: false, status: 403, text: () => pendingText }))
    const refresh = vi.fn()
    const permission = vi.fn()
    setProjectFailureCapture(() => ({ isCurrent: () => true, refresh }))
    setPermissionDeniedHandler(permission)
    let oldModalOpen = true
    const result = downloadDocument(42, body, { isCurrent: () => oldModalOpen }).catch(() => null)
    await Promise.resolve()
    oldModalOpen = false
    // 新弹窗属于同一作品，但原请求只捕获旧弹窗代次。
    release('{"detail":"无权限","code":"permission_denied"}')
    await result
    expect(refresh).not.toHaveBeenCalled()
    expect(permission).not.toHaveBeenCalled()
  })

  it('目录请求在弹窗关闭后的旧 401 不会退出新会话', async () => {
    let release!: (body: string) => void
    const pendingText = new Promise<string>((resolve) => { release = resolve })
    vi.stubGlobal('fetch', async () => ({ ok: false, status: 401, text: () => pendingText }))
    const logout = vi.fn()
    setUnauthorizedHandler(logout)
    const controller = new AbortController()
    let current = true
    const result = noteApi.list(42, {}, controller.signal, () => current).catch(() => null)
    await Promise.resolve()
    current = false
    controller.abort()
    release('{"detail":"未登录"}')
    await result
    expect(logout).not.toHaveBeenCalled()
  })
})
