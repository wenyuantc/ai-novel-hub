import { describe, expect, it } from 'vitest'
import { fieldChoices } from './fields'
import { effectiveRows } from './preview'
import type { SettingEffective } from '../../api/types'

const effective: SettingEffective = {
  timeline_id: 3,
  position: { chapter_id: 4, chapter_no: 5, boundary: 'start' },
  fields: { current_state: '受伤', life_status: 'alive' },
  custom_fields: { '24': '伤口愈合' },
  applied_event_ids: [7], orphaned_event_ids: [], baseline_status: 'confirmed', timeline_revision: 1,
}

describe('按章设定预览', () => {
  it('只展示服务端返回的实际字段并按字段稳定 ID 匹配自定义内容', () => {
    const choices = fieldChoices('character', [{ id: 24, timeline_id: 3, label: '伤势', base_value: '无', sort: 0 }])
    expect(effectiveRows(effective, choices)).toEqual([
      { key: 'builtin:current_state', label: '当前状态', value: '受伤' },
      { key: 'builtin:life_status', label: '存活状态', value: '存活' },
      { key: 'custom:24', label: '伤势', value: '伤口愈合' },
    ])
  })

  it('空文本明确显示为空，不回退成最新设定', () => {
    expect(effectiveRows({ ...effective, fields: { content: '' }, custom_fields: {} }, fieldChoices('world', [])))
      .toEqual([{ key: 'builtin:content', label: '内容', value: '（空）' }])
  })
})
