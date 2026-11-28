import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, it, expect, vi } from 'vitest'
import { PictureHistory } from './LookPictures'

vi.mock('../../../hooks/useLoader', () => ({
  useLoader: () => ({
    data: [
      { id: 3, status: 'pending', media_object_id: null, prompt: '常服立绘' },
      { id: 2, status: 'failed', media_object_id: null, prompt: '常服立绘', error_message: '厂商拒绝本次请求' },
      { id: 1, status: 'ready', media_object_id: 'ready-media', prompt: '青衣持扇', is_selected: true },
      { id: 4, status: 'ready', media_object_id: 'other-media', prompt: '秋衫' },
    ],
  }),
}))
describe('造型历史', () => {
  it('展示持久化进度、失败原因、选定缩略图与本次提示词，查看者只读', () => {
    const html = renderToStaticMarkup(
      createElement(PictureHistory, {
        lookId: 1,
        label: '小云 · 常服',
        canEdit: false,
        canEditNow: () => false,
        onChange: vi.fn(),
      }),
    )
    for (const text of [
      '生成中',
      '厂商拒绝本次请求',
      '当前选用',
      '/api/v1/media/ready-media?variant=thumb',
      '青衣持扇',
    ])
      expect(html).toContain(text)
    expect(html).not.toMatch(/>选\s*用</)
    expect(html).not.toMatch(/>删\s*除</)
  })
  it('编辑者可以选用已完成的图片、删除非进行中记录', () => {
    const html = renderToStaticMarkup(
      createElement(PictureHistory, {
        lookId: 1,
        label: '小云 · 常服',
        canEdit: true,
        canEditNow: () => true,
        onChange: vi.fn(),
      }),
    )
    expect((html.match(/>选\s*用</g) ?? []).length).toBe(1)
    expect((html.match(/>删\s*除</g) ?? []).length).toBe(3)
  })
})
