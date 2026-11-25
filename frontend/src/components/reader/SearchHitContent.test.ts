import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import type { ReaderBlock } from '../../api/reader'
import SearchHitContent from './SearchHitContent'

const hash = 'a'.repeat(64)
const text = '🙂乙\r\n𠮷地乙'
const secondBlock: ReaderBlock = { kind: 'heading', level: 2, start: 4, end: 7, runs: [
  { start: 4, end: 7, bold: true, italic: false },
] }
const blocks: ReaderBlock[] = [
  { kind: 'paragraph', start: 0, end: 2, level: null, runs: [{ start: 0, end: 2, bold: false, italic: false }] },
  secondBlock,
]
function render(start: number, end: number, options: { rich?: boolean; contentHash?: string; content?: string; activeStart?: number } = {}) {
  return renderToStaticMarkup(createElement(MemoryRouter, {}, createElement(SearchHitContent, {
    projectId: 7, text: options.content ?? text, blocks: options.rich ? blocks : null, contentHash: options.contentHash ?? hash,
    activeStart: options.activeStart, anchor: { start, end, content_hash: hash, origin: 'book-search' },
  })))
}

describe('全书搜索的阅读器定位', () => {
  it('精确高亮第二处未登记词，保留换行与补充平面字符', () => {
    const page = render(6, 7)
    expect(page.replace(/<[^>]*>/g, '')).toBe(text)
    expect(page).toMatch(/data-mention-focus="true"[^>]*>乙<\/mark>/)
    expect(page).not.toContain('正在识别提及')
  })
  it('富文档的跨段命中保留换行与标题、粗体，单独的换行也有焦点', () => {
    const page = render(1, 5, { rich: true })
    const focused = [...page.matchAll(/<mark[^>]*data-mention-focus[^>]*>([\s\S]*?)<\/mark>/g)]
      .map((match) => match[1].replace(/<[^>]*>/g, '')).join('')
    expect(focused).toBe('乙\r\n𠮷')
    expect(page).toContain('<strong>𠮷</strong>')
    expect(page).toContain('<h2')
    expect(render(2, 4, { rich: true })).toMatch(/aria-label="命中的换行"[^>]*>\r\n<\/mark>/)
  })
  it.each([false, true])('朗读与跨行搜索分别标记，只有换行的搜索仍可定位（富格式 %s）', (rich) => {
    const page = render(1, 5, { rich, activeStart: 4 })
    expect(page.match(/data-read-aloud-active=/g)).toHaveLength(1)
    expect(page).toMatch(rich ? /<h2[^>]+data-read-aloud-active/ : /<span data-read-aloud-active/)
    const marks = (html: string) => [...html.matchAll(/<mark[^>]*data-mention-focus[^>]*>([\s\S]*?)<\/mark>/g)]
      .map((m) => m[1].replace(/<[^>]*>/g, '')).join('')
    expect(marks(page)).toBe('乙\r\n𠮷')
    expect(marks(render(2, 4, { rich, activeStart: 4 }))).toBe('\r\n')
  })
  it('指纹变化、内容清空或范围越界时不高亮旧位置，提供重新搜索入口', () => {
    for (const page of [render(0, 2, { contentHash: 'b'.repeat(64) }), render(0, 2, { content: '' }), render(0, 8)]) {
      expect(page).toContain('搜索结果的位置已失效')
      expect(page).toContain('href="/projects/7/book-search"')
      expect(page).not.toContain('data-mention-focus')
    }
  })
})
