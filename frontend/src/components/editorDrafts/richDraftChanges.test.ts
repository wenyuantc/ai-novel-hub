import { describe, expect, it } from 'vitest'
import type { RichDocument, RichFormatting, RichMarker, RichSection } from '../../api/richDocument'
import { richDraftChanges } from './richDraftChanges'

const formatting: RichFormatting = { version: 1, boundaries: [], inline: [], blocks: [] }
const section: RichSection = { key: 'note', anchor: 2, order: 0, title: '写作提示', content: '秘密', formatting, include_in_ai: false }
const marker: RichMarker = {
  key: 'marker', section_key: section.key, status: 'active', start: 0, end: 2, color: 'yellow',
  excerpt: '秘密', excerpt_truncated: false, last_start: 0,
}
const remote: RichDocument = {
  content: '山间有灯。', formatting, sections: [section], retired_section_keys: [], markers: [marker],
}

describe('草稿富文档比较', () => {
  it('正文完全相同时，仍标出正文格式、区块重命名和标记改绑', () => {
    const local: RichDocument = {
      ...remote, formatting: { ...formatting, inline: [{ kind: 'bold', start: 0, end: 2 }] },
      sections: [{ ...section, title: '伏笔' }], markers: [{ ...marker, section_key: null, excerpt: '山间' }],
    }
    const changes = richDraftChanges(remote, local)
    expect(changes.formatting).toEqual([{ key: '粗体 · 第 1–2 字符', local: '粗体 · 第 1–2 字符' }])
    expect(changes.sections).toEqual([{ key: section.key, remote: section, local: local.sections[0] }])
    expect(changes.markers).toEqual([{ key: marker.key, remote: marker, local: local.markers[0] }])
  })

  it('按区块身份对齐，区分退场与同名新增，并保留已移除标记详情', () => {
    const local: RichDocument = {
      ...remote, sections: [{ ...section, key: 'new-note' }], retired_section_keys: [section.key], markers: [],
    }
    const changes = richDraftChanges(remote, local)
    expect(changes.sections).toEqual([{ key: section.key, remote: section }, { key: 'new-note', local: local.sections[0] }])
    expect(changes.markers).toEqual([{ key: marker.key, remote: marker }])
    expect(changes.retired).toEqual([{ key: section.key, local: section.key }])
  })
})
