import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { ProjectThumbnailEditor, SettingThumbnail, ThumbnailControls } from './SettingThumbnail'

const target = { scope: 'project' as const, projectId: 4, kind: 'character' as const, entryId: 8 }
const thumbnail = { version: 'picture-v1', mime_type: 'image/webp', width: 200, height: 200 }

describe('缩略图展示', () => {
  it('图片使用本书授权路径、版本和名称，可延迟加载', () => {
    const html = renderToStaticMarkup(createElement(SettingThumbnail, { target, thumbnail, name: '林远' }))
    expect(html).toContain('/api/v1/projects/4/settings/character/8/thumbnail?v=picture-v1')
    expect(html).toContain('林远的缩略图')
    expect(html).toContain('loading="lazy"')
  })
  it('有图时提供可用键盘操作的预览按钮，无图占位不可点击', () => {
    const withPicture = renderToStaticMarkup(createElement(SettingThumbnail, { target, thumbnail, name: '林远' }))
    expect(withPicture).toMatch(/<button type="button" aria-label="预览林远的缩略图"/)
    const empty = renderToStaticMarkup(createElement(SettingThumbnail, { target, name: '林远' }))
    expect(empty).not.toContain('<button')
  })
  it('无图展示可访问占位，查看权限没有修改按钮', () => {
    const html = renderToStaticMarkup(createElement(ThumbnailControls, { target, name: '林远', editable: false, busy: false,
      error: null, onFile: vi.fn(), onDelete: vi.fn() }))
    expect(html).toContain('林远暂无缩略图')
    expect(html).not.toContain('上传图片')
    expect(html).not.toContain('删除图片')
  })
  it('图片操作区在可生图时显示 AI 入口，忙碌时禁用，查看者不显示', () => {
    const props = { target, name: '林远', busy: false, error: null, onFile: vi.fn(), onDelete: vi.fn(), onGenerate: vi.fn() }
    expect(renderToStaticMarkup(createElement(ThumbnailControls, { ...props, editable: true }))).toContain('AI 生成图片')
    expect(renderToStaticMarkup(createElement(ThumbnailControls, { ...props, editable: false }))).not.toContain('AI 生成图片')
    const busy = renderToStaticMarkup(createElement(ThumbnailControls, { ...props, editable: true, busy: true }))
    expect(busy).toMatch(/<button[^>]*disabled[^>]*>[^]*AI 生成图片/)
  })
  it.each(['character', 'world'] as const)('%s：作品可编辑但图片权限被隐藏时，有图只显示图片、无图也不能上传', (kind) => {
    const sharedProjection = { ...target, kind }
    for (const picture of [thumbnail, null]) {
      const html = renderToStaticMarkup(createElement(ProjectThumbnailEditor, { target: sharedProjection, thumbnail: picture,
        name: '共享条目', editable: true, thumbnailEditable: false, onChange: vi.fn() }))
      expect(html).toContain(picture ? '共享条目的缩略图' : '共享条目暂无缩略图')
      expect(html).not.toContain('上传图片')
      expect(html).not.toContain('替换图片')
      expect(html).not.toContain('删除图片')
    }
  })
  it.each(['character', 'world'] as const)('%s：图片权限缺失时保守只读，显式可写且作品可编辑时才提供操作', (kind) => {
    const entryTarget = { ...target, kind }
    const render = (editable: boolean, thumbnailEditable?: boolean, picture = thumbnail) => renderToStaticMarkup(createElement(ProjectThumbnailEditor, {
      target: entryTarget, thumbnail: picture, name: '条目', editable, thumbnailEditable, onChange: vi.fn(),
    }))
    expect(render(true)).not.toContain('替换图片')
    expect(render(false, true)).not.toContain('替换图片')
    expect(render(true, true)).toContain('替换图片')
    expect(render(true, true)).toContain('删除图片')
    expect(renderToStaticMarkup(createElement(ProjectThumbnailEditor, { target: entryTarget, name: '新条目', editable: true,
      thumbnailEditable: true, onChange: vi.fn() }))).toContain('上传图片')
  })
})
