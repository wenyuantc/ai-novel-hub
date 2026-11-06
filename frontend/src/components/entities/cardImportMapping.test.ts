import { describe, expect, it } from 'vitest'
import { cardMetadata, cardTagImportSummary, cleanCardMappings, referenceTypes, withoutInternalMapping } from './cardImportMapping'

const data = { format: 'ainovel.cards', version: 4,
  characters: [{ ref: 'local-role', name: '林远', fields: [{ definition_ref: 'home', value: { entry_ref: 'outside-place' } }] }],
  organizations: [{ ref: 'local-org', name: '青云宗', fields: [] }], careers: [],
  field_definitions: [{ ref: 'home', field_type: 'reference', reference_types: ['world.place'] }],
  external_references: [{ ref: 'outside-place', kind: 'world', type: 'world.place', name: '青山' }],
}
describe('卡片局部身份与显式映射', () => {
  it('v5标签摘要区分新建/改名/跳过，v1–v4不解释未来标签也不丢typed引用', () => {
    const metadata = cardMetadata({ ...data, version: 5, characters: [{ ...data.characters[0], setting_tags: ['同盟', '待整理'] }], careers: [{ ref: 'career', name: '剑修', setting_tags: ['修行'] }] })
    expect(metadata.usages).toHaveLength(1)
    const preview = { characters: [{ name: '林远', status: 'conflict' as const, rename_to: '林远（导入）' }], organizations: [{ name: '青云宗', status: 'new' as const, rename_to: null }], careers: [{ name: '剑修', status: 'reuse' as const, stages_added: [] }] }
    expect(cardTagImportSummary(metadata, { characters: { 林远: 'skip' }, organizations: {} }, preview)).toEqual([
      { kind: 'character', name: '林远', tags: ['同盟', '待整理'], action: 'skipped' },
      { kind: 'organization', name: '青云宗', tags: [], action: 'created' },
      { kind: 'career', name: '剑修', tags: ['修行'], action: 'skipped' },
    ])
    expect(cardTagImportSummary(metadata, { characters: { 林远: 'rename' }, organizations: {} }, preview)[0].action).toBe('renamed')
    expect(cardTagImportSummary(cardMetadata({ ...data, characters: [{ ...data.characters[0], setting_tags: ['未来标签'] }] }), { characters: {}, organizations: {} }, preview)).toEqual([])
  })
  it('仅v4读取局部身份；旧版夹带未来引用不能激活新语义', () => {
    expect(cardMetadata(data).entries).toContainEqual({ ref: 'local-role', name: '林远', kind: 'character' })
    expect(cardMetadata({ ...data, version: 3 })).toEqual({ version: 3, entries: [], usages: [], external: [] })
  })
  it('包内同名改为新建时清除旧显式目标，其他包外映射和原输入保留', () => {
    const mappings = { references: { 'local-org': { kind: 'organization' as const, id: 8 }, 'outside-place': { kind: 'world' as const, id: 9 } }, definitions: {} }
    expect(withoutInternalMapping(mappings, cardMetadata(data), 'organizations', '青云宗').references).toEqual({ 'outside-place': { kind: 'world', id: 9 } })
    expect(mappings.references['local-org']).toEqual({ kind: 'organization', id: 8 })
  })
  it('引用选择按实际导入主体的允许范围求交，跳过内容不限制其他引用', () => {
    const metadata = cardMetadata(data)
    expect(referenceTypes('outside-place', 'world', metadata, { characters: {}, organizations: {} }, { characters: [{ name: '林远', status: 'new', rename_to: null }], organizations: [], careers: [] })).toEqual(['world.place'])
    expect(referenceTypes('unused', 'world', metadata, { characters: {}, organizations: {} }, { characters: [], organizations: [], careers: [] })).toHaveLength(6)
  })
  it('v6 链接限定精确包外子类，仅来源被创建时约束映射', () => {
    const source = { ...data, characters: [{ ...data.characters[0], fields: [], ai_link_refs: ['outside-place', 'local-org'] }] }
    const preview = { characters: [{ name: '林远', status: 'conflict' as const, rename_to: '林远（导入）' }], organizations: [], careers: [] }
    const metadata = cardMetadata({ ...source, version: 6 })
    expect(referenceTypes('outside-place', 'world', metadata, { characters: { 林远: 'rename' }, organizations: {} }, preview)).toEqual(['world.place'])
    expect(referenceTypes('outside-place', 'world', metadata, { characters: { 林远: 'skip' }, organizations: {} }, preview)).toHaveLength(6)
    expect(cardMetadata({ ...source, version: 5 }).usages).toEqual([])
  })
  it('改名空白由后端生成，选项映射保持显式多对一；不推断同名映射', () => {
    expect(cleanCardMappings({ references: {}, definitions: { rank: { action: 'rename', label: '  ', option_mappings: {} }, other: { action: 'reuse', target_definition_id: 7, option_mappings: { a: 9, b: 9 } } } })).toEqual({ references: {}, definitions: { rank: { action: 'rename', option_mappings: {} }, other: { action: 'reuse', target_definition_id: 7, option_mappings: { a: 9, b: 9 } } } })
  })
})
