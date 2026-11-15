import { App } from 'antd'
import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { ApiError } from '../../api/client'
import { contextPreviewDenied } from '../../api/memories'
import ContextPreviewPanel from './ContextPreviewPanel'

// E6-e：context-preview 已对编辑者开放（E6-d）；viewer 的 403 只就地显示「无权限」，不当作错误弹出。
describe('章节注入预览的权限处理', () => {
  it('403 视为无权限；404、500、网络错误仍按错误显示', () => {
    expect(contextPreviewDenied(new ApiError(403, '当前作品没有执行该操作的权限', { code: 'project_readonly' }))).toBe(true)
    expect(contextPreviewDenied(new ApiError(403, '协作授权已变化', { code: 'permission_denied' }))).toBe(true)
    expect(contextPreviewDenied(new ApiError(404, '章节不存在'))).toBe(false)
    expect(contextPreviewDenied(new ApiError(500, '服务器内部错误'))).toBe(false)
    expect(contextPreviewDenied(new Error('network'))).toBe(false)
  })

  it('折叠时不发请求、不显示错误', () => {
    const html = renderToString(createElement(App, null, createElement(ContextPreviewPanel, { chapterId: 3 })))
    expect(html).toContain('本次注入的旧情节和参考资料')
    expect(html).not.toContain('无权限')
    expect(html).not.toContain('ant-alert-error')
  })
})
