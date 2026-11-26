import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import SerializationManuscript from './SerializationManuscript'
import { beijingMinute, publicationRequest } from './serializationForm'

const now = Date.parse('2026-11-19T00:30:00+08:00')

describe('连载登记预览与重试', () => {
  it('以北京时间跨日，服务端无时区后缀按 UTC 解读', () => {
    expect(beijingMinute('2026-11-18T15:59:00')).toBe('2026-11-18T23:59')
    expect(beijingMinute('2026-11-18T16:00:00Z')).toBe('2026-11-19T00:00')
    expect(beijingMinute('invalid')).toBe('')
    expect(publicationRequest(null, 1, 2, '番茄', '2026-11-19T00:01', now).published_at)
      .toBe('2026-11-18T16:01:00.000Z')
    expect(publicationRequest(null, 1, 2, '转载', '1991-06-01T12:34', now).published_at)
      .toBe('1991-06-01T03:34:00.000Z')
    expect(() => publicationRequest(null, 1, 2, '转载', '1991-04-14T02:15', now)).toThrow('日期和时间')
  })

  it('仅在完全相同的登记重试时复用原始请求 ID', () => {
    const first = publicationRequest(null, 7, 9, ' 起点 ', '2026-11-18T22:00', now)
    expect(publicationRequest(first, 7, 9, '起点', '2026-11-18T22:00', now)).toBe(first)
    for (const next of [
      publicationRequest(first, 7, 9, '番茄', '2026-11-18T22:00', now),
      publicationRequest(first, 8, 10, '起点', '2026-11-18T22:00', now),
      publicationRequest(first, 7, 9, '起点', '2026-11-18T21:00', now),
    ]) expect(next.request_id).not.toBe(first.request_id)
  })

  it('拒绝无平台、无效日期或未来的实际发布时间', () => {
    expect(() => publicationRequest(null, 0, 1, ' ', '2026-11-18T22:00', now)).toThrow('平台')
    expect(publicationRequest(null, 0, 1, '🌟'.repeat(100), '2026-11-18T22:00', now).channel).toHaveLength(200)
    expect(() => publicationRequest(null, 0, 1, '🌟'.repeat(101), '2026-11-18T22:00', now)).toThrow('最多 100')
    for (const invalid of ['', '2026-02-30T00:30', '2026-11-18T24:30'])
      expect(() => publicationRequest(null, 0, 1, '起点', invalid, now)).toThrow('日期和时间')
    expect(() => publicationRequest(null, 0, 1, '起点', '2026-11-19T00:31', now)).toThrow('不能晚于')
  })

  it('历史预览按快照保留旧标题和读取格式，正确处理 Unicode 正文', () => {
    const html = renderToStaticMarkup(createElement(SerializationManuscript, { projectId: 1, manuscript: {
      chapter_no: 3, title: '旧章名', content: '河🙂', blocks: [
        { start: 0, end: 2, kind: 'paragraph', level: null, runs: [{ start: 0, end: 2, bold: true, italic: false }] },
      ],
    } }))
    expect(html).toContain('第 3 章 · 旧章名')
    expect(html).toContain('<strong>河🙂</strong>')
  })
})

it('章名与正文均按码点定位，保留跨样式和跨段的命中', () => {
  const manuscript = { chapter_no: 8, title: '乙🙂乙', content: '🙂乙\n𠮷乙', blocks: [
    { kind: 'paragraph' as const, level: null, start: 0, end: 2, runs: [{ start: 0, end: 2, bold: false, italic: false }] },
    { kind: 'heading' as const, level: 2, start: 3, end: 5, runs: [
      { start: 3, end: 4, bold: true, italic: false }, { start: 4, end: 5, bold: false, italic: true },
    ] },
  ] }
  const render = (field: 'title' | 'content', term: string, start: number, end: number, plain = false) => {
    const hit = { field, term, start, end, context: '' }
    return renderToStaticMarkup(createElement(SerializationManuscript, {
      projectId: 1, manuscript: plain ? { ...manuscript, blocks: null } : manuscript, hit,
    }))
  }
  const title = render('title', '乙', 2, 3)
  expect(title).toMatch(/乙🙂<mark[^>]*>乙<\/mark>/)
  const rich = render('content', '乙\n𠮷乙', 1, 5)
  const covered = [...rich.matchAll(/<mark[^>]*>([\s\S]*?)<\/mark>/g)].map((m) => m[1].replace(/<[^>]*>/g, '')).join('')
  expect(covered).toBe('乙\n𠮷乙')
  expect(rich).toContain('<strong>𠮷</strong><em>乙</em>')
  expect(render('content', '乙', 4, 5, true)).toMatch(/🙂乙\n𠮷<mark[^>]*>乙<\/mark>/)
})
