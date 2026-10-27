// 移动端渲染（node 环境，用 react-dom/server 渲染成 HTML 断言结构）：校正列表在移动端是卡片、没有表头，操作收进「⋮」菜单。
import { App } from 'antd'
import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import type { BookChapterView, BookImport } from '../../api/types'

const mobile = vi.hoisted(() => ({ value: true }))
vi.mock('../../hooks/useIsMobile', () => ({
  useIsMobile: () => mobile.value,
  useModalWidth: (w: number | string) => (mobile.value ? 'calc(100vw - 24px)' : w),
}))

const { default: ChapterCard } = await import('./ChapterCard')
const { default: CorrectionStep } = await import('./CorrectionStep')

function chapter(index: number, over: Partial<BookChapterView> = {}): BookChapterView {
  return {
    index,
    title: `第${index}章 风起`,
    volume_no: 1,
    volume_title: '第一卷 青云',
    source_no: index,
    chars: 3200,
    first_line: '天色将明，山门外传来钟声。',
    warnings: [],
    ...over,
  }
}

function record(): BookImport {
  const chapters = [chapter(1, { warnings: ['short'] }), chapter(2), chapter(3, { warnings: ['gap'] })]
  return {
    id: 1,
    filename: '书.txt',
    kind: 'txt',
    size: 100,
    book_title: '书',
    split_mode: 'auto',
    split_pattern: null,
    revision: 0,
    notices: ['已去除文件开头的目录区（12 行）'],
    chapters,
    warning_counts: { short: 1, long: 0, dup_title: 0, gap: 1 },
    chapter_count: 3,
    total_chars: 9600,
    too_many: false,
    max_import_chapters: 3000,
    expires_at: '2026-11-03T08:00:00',
    created_at: '2026-10-27T08:00:00',
    updated_at: '2026-10-27T08:00:00',
  }
}

const noop = async () => true

describe('拆书校正：移动端渲染', () => {
  it('卡片：卷首章显示卷名，序号 + 标题、字数与开头一行、提示标签、操作菜单按钮；不写固定宽度', () => {
    const html = renderToString(
      createElement(ChapterCard, {
        chapter: chapter(1, { warnings: ['short', 'gap'] }),
        total: 3,
        volumeStart: true,
        onOpen: () => {},
        onAction: () => {},
      }),
    )
    expect(html).toContain('第一卷 青云')
    expect(html).toContain('第1章 风起')
    expect(html).toContain('3200 字')
    expect(html).toContain('天色将明')
    expect(html).toContain('过短')
    expect(html).toContain('序号断档')
    expect(html).toContain('aria-label="第 1 章操作"')
    expect(html).not.toMatch(/width:\s*\d{3,}px/)
    expect(html).not.toContain('100vw')
  })

  it('非卷首章不重复显示卷名', () => {
    const html = renderToString(
      createElement(ChapterCard, { chapter: chapter(2), total: 3, volumeStart: false, onOpen: () => {}, onAction: () => {} }),
    )
    expect(html).not.toContain('第一卷 青云')
  })

  it('校正步骤：移动端是没有表头的卡片列表（含提示汇总与 notices），桌面是带表头的表格', () => {
    const props = { record: record(), busy: false, onOp: noop, onResplit: noop }
    mobile.value = true
    const m = renderToString(createElement(App, null, createElement(CorrectionStep, props)))
    expect(m).toContain('已去除文件开头的目录区')
    expect(m).toContain('切得不对？用自定义正则重新切章')
    expect(m).toContain('第3章 风起')
    expect(m).toContain('aria-label="第 2 章操作"')
    expect(m).not.toContain('<th')
    expect(m).not.toContain('开头一行')
    expect(m).not.toContain('100vw')

    mobile.value = false
    const d = renderToString(createElement(App, null, createElement(CorrectionStep, props)))
    expect(d).toContain('<th')
    expect(d).toContain('开头一行')
  })
})
