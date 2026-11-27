import { createElement, type ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import MediaImage from './MediaImage'
import MediaVideo from './MediaVideo'

it('图片与缩略图使用带编码的同源地址，保留替代文字', () => {
  const html = renderToStaticMarkup(createElement(MediaImage, { objectId: 'a/b', alt: '角色肖像', variant: 'thumb' }))
  expect(html).toContain('src="/api/v1/media/a%2Fb?variant=thumb"')
  expect(html).toContain('alt="角色肖像"')
})
it('视频使用原生播放器、元数据预载与海报派生地址', () => {
  const html = renderToStaticMarkup(createElement(MediaVideo, { objectId: 'v' }))
  expect(html).toContain('src="/api/v1/media/v"')
  expect(html).toContain('controls=""')
  expect(html).toContain('poster="/api/v1/media/v?variant=poster"')
  expect(html).toContain('preload="metadata"')
})
it('未提供对象时展示可读提示', () => {
  expect(renderToStaticMarkup(createElement(MediaVideo, { objectId: '' }))).toContain('文件暂不可用')
  expect(renderToStaticMarkup(createElement(MediaImage, { objectId: '', alt: '角色肖像' }))).toContain('文件暂不可用')
})

it.each(['image', 'video'])('%s 加载错误后以状态提示替换损坏的媒体', kind => {
  const element = kind === 'image' ? MediaImage({ objectId: 'missing', alt: '肖像' }) : MediaVideo({ objectId: 'missing' })
  // SSR 没有 DOM；在同一个 React render 中触发媒体元素的实际错误处理器。
  function FailedLoad() {
    const content: ReactElement<{ onError?: () => void }> = element.type(element.props)
    content.props.onError?.()
    return content
  }
  const html = renderToStaticMarkup(createElement(FailedLoad))
  expect(html).toContain('role="status"')
  expect(html).toContain('文件暂不可用')
  expect(html).not.toMatch(/<(img|video)\b/)
})
