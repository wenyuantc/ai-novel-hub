import { describe, expect, it } from 'vitest'
import { buildDocumentRequest, updateSelection } from './selection'

describe('组合导出选择', () => {
  it('不同页全选不清除上一页且跳过禁用项', () => {
    const page1 = [{ key: '1', label: '第一章' }, { key: '2', label: '生成中', disabled: true }]
    const page2 = [{ key: '3', label: '第三章' }]
    const selected = updateSelection(updateSelection([], page1, true), page2, true)
    expect(selected.map((item) => item.key)).toEqual(['1', '3'])
    expect(updateSelection(selected, page1, false).map((item) => item.key)).toEqual(['3'])
  })

  it('重复条目不会在全选后产生重复请求身份', () => {
    expect(updateSelection([], [{ key: '1', label: '章' }, { key: '1', label: '章' }], true)).toEqual([
      { key: '1', label: '章' },
    ])
  })

  it('自选为空不能转换成全部', () => {
    expect(() => buildDocumentRequest({ format: 'docx', scope: 'selected', unit: 'chapter', content: 'prose',
      chapters: [], scenes: [], settings: [], conversations: [], notes: [] })).toThrow('请选择')
  })

  it('仅资料导出使用 none 并保留具体条目', () => {
    expect(buildDocumentRequest({ format: 'docx', scope: 'none', unit: 'scene', content: 'both',
      chapters: [{ key: '3', label: '章节' }], scenes: [{ key: '8', label: '场景' }],
      settings: [{ key: 'character:4', label: '小明' }, { key: 'world:4', label: '长安' }],
      conversations: [{ key: '7', label: '聊天' }], notes: [{ key: '9', label: '笔记' }] })).toEqual({
      format: 'docx', story: { scope: 'none', unit: 'scene', content: 'both', ids: [] },
      materials: { settings: [{ kind: 'character', id: 4 }, { kind: 'world', id: 4 }], conversation_ids: [7], note_ids: [9] },
    })
  })

  it('none 且没有资料时不产生空文档请求', () => {
    expect(() => buildDocumentRequest({ format: 'txt', scope: 'none', unit: 'chapter', content: 'prose',
      chapters: [], scenes: [], settings: [], conversations: [], notes: [] })).toThrow('请选择')
  })
})
