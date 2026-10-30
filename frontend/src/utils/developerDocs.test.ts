import { afterEach, describe, expect, it, vi } from 'vitest'
import { copyDeveloperCode } from './developerDocs'

afterEach(() => vi.unstubAllGlobals())

describe('公开开发示例复制', () => {
  it('完整保留多行示例和占位符，不修改 shell 变量', async () => {
    const code = 'API_KEY="YOUR_API_KEY"\n\ncurl -H "X-API-Key: $API_KEY" "$API_BASE/me"'
    const writeText = vi.fn(async () => undefined)
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    await expect(copyDeveloperCode(code)).resolves.toBe(true)
    expect(writeText).toHaveBeenCalledWith(code)
  })

  it('剪贴板不可用时通知界面选择代码供手动复制', async () => {
    vi.stubGlobal('navigator', {})
    await expect(copyDeveloperCode('公开示例')).resolves.toBe(false)
  })

  it('浏览器拒绝权限时保留同一条手动复制路径', async () => {
    vi.stubGlobal('navigator', { clipboard: { writeText: async () => { throw new DOMException('denied', 'NotAllowedError') } } })
    await expect(copyDeveloperCode('公开示例')).resolves.toBe(false)
  })
})
