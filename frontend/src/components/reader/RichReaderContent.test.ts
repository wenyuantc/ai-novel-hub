import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { ReaderBlock } from '../../api/reader'
import RichReaderContent from './RichReaderContent'

const text = '张🙂三\n<script>x</script>\n列表一\n列表二'
function position(value: string) { return Array.from(text.slice(0, text.indexOf(value))).length }
function block(value: string, kind: ReaderBlock['kind']): ReaderBlock {
  const start = position(value), end = start + Array.from(value).length
  return { kind, start, end, level: null, runs: [{ start, end, bold: false, italic: false }] }
}
const name = block('张🙂三', 'paragraph')
name.runs = [
  { start: 0, end: 1, bold: true, italic: false },
  { start: 1, end: 2, bold: true, italic: true },
  { start: 2, end: 3, bold: true, italic: false },
]
const blocks = [name, block('<script>x</script>', 'paragraph'), block('列表一', 'bullet_list'), block('列表二', 'bullet_list')]

describe('阅读器正式正文排版', () => {
  it('按码点渲染粗斜体与列表，文字不能成为 HTML', () => {
    const html = renderToStaticMarkup(createElement(RichReaderContent, { projectId: 1, text, blocks }))
    expect(html).toContain('<strong><em>🙂</em></strong>')
    expect(html).toContain('&lt;script&gt;x&lt;/script&gt;')
    expect(html).not.toContain('<script>')
    expect(html).toMatch(/<ul[^>]*>.*列表一.*列表二.*<\/ul>/)
    expect(html).toContain('张')
  })

  it('提及仍是同一个可聚焦按钮，内部保留格式与码点位置', () => {
    const html = renderToStaticMarkup(createElement(RichReaderContent, {
      projectId: 1, text, blocks,
      mentions: { spans: [{ start: 0, end: 3, text: '张🙂三', entities: [{ kind: 'character', id: 2,
        category: 'character', name: '张🙂三', aliases: [] }] }],
      focus: { start: 0, end: 3, content_hash: 'hash' } },
    }))
    expect(html).toContain('data-mention-start="0"')
    expect(html).toContain('data-mention-focus="true"')
    expect(html).toContain('<strong><em>🙂</em></strong>')
    expect(html.match(/data-mention-start=/g)).toHaveLength(1)
  })
})
