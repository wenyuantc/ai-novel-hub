import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { MentionEntity } from '../../api/mentions'
import MentionText from './MentionText'

const character: MentionEntity = { kind: 'character', id: 1, name: '林远', aliases: [], category: 'character' }
describe('提及文字渲染', () => {
  it('以文本节点展示原文，点击设定有键盘按钮且Unicode范围正确', () => {
    const html = renderToStaticMarkup(createElement(MentionText, { projectId: 1, text: '😀林远<script>alert(1)</script>', spans: [{ start: 1, end: 3, text: '林远', entities: [character] }], focus: { start: 1, end: 3 } }))
    expect(html).toContain('aria-label="林远：查看设定"')
    expect(html).toContain('data-mention-start="1"')
    expect(html).toContain('data-mention-focus="true"')
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;')
    expect(html).not.toContain('<script>')
  })
  it('同名候选不自动选定其中一个，保留明确歧义入口', () => {
    const html = renderToStaticMarkup(createElement(MentionText, { projectId: 1, text: '林远', spans: [{ start: 0, end: 2, text: '林远', entities: [character, { ...character, id: 2 }] }] }))
    expect(html).toContain('林远：选择对应设定')
    expect(html).not.toContain('林远：查看设定')
  })
})
