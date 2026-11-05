import { describe, expect, it } from 'vitest'
import type { Character } from '../../api/types'
import {
  characterBaselineMatches,
  characterToBaselineForm,
  createCharacterDraftCache,
  createCharacterRequestGate,
  formToCharacterPatch,
} from './characterForm'

const latest: Character = {
  id: 17,
  project_id: 3,
  name: '林远',
  role_type: '主角',
  profile: '修士',
  relations_text: '',
  aliases: ['小远'],
  age: '二十岁',
  gender: '男',
  appearance: '一身青衣',
  personality: '谨慎',
  background: '离开故乡',
  current_state: '金丹期',
  life_status: 'dead',
  always_in_context: true,
  sort: 5,
}

const baseline = {
  age: '十七岁',
  gender: '男',
  appearance: '一身布衣',
  personality: '谨慎',
  background: '尚在故乡',
  profile: '少年',
  current_state: '练气期',
  life_status: 'alive',
}

describe('角色基础设定表单', () => {
  it('使用故事初始值而非角色卡里的最新状态', () => {
    const form = characterToBaselineForm(latest, baseline)
    expect(form).toMatchObject({
      name: '林远',
      age: '十七岁',
      appearance: '一身布衣',
      background: '尚在故乡',
      current_state: '练气期',
      life_status: 'alive',
    })
    expect(latest.current_state).toBe('金丹期')
  })

  it('只改姓名时不提交任何状态字段或未经修改的资料', () => {
    const initial = characterToBaselineForm(latest, baseline)
    const edited = { ...initial, name: ' 林远山 ' }
    expect(formToCharacterPatch(edited, initial)).toEqual({ name: '林远山' })
  })

  it('作者将初始值改成恰好等于最新值时仍然提交这次修正', () => {
    const initial = characterToBaselineForm(latest, baseline)
    expect(formToCharacterPatch({ ...initial, current_state: '金丹期' }, initial)).toEqual({
      current_state: '金丹期',
    })
  })

  it('比较规范化后的别名，等价输入不会产生无意义修改', () => {
    const initial = characterToBaselineForm(latest, baseline)
    expect(formToCharacterPatch({ ...initial, aliases: [' 小远 ', ''] }, initial)).toEqual({})
    expect(formToCharacterPatch({ ...initial, aliases: ['小远', '林师兄'] }, initial)).toEqual({
      aliases: ['小远', '林师兄'],
    })
  })

  it('不会把后端忽略的空排序作为可保存变更', () => {
    const initial = characterToBaselineForm(latest, baseline)
    expect(formToCharacterPatch({ ...initial, sort: null }, initial)).toStrictEqual({})
  })

  it('基础快照缺少字段或状态非法时拒绝用最新值冒充初始值', () => {
    expect(() => characterToBaselineForm(latest, { ...baseline, current_state: undefined })).toThrow()
    expect(() => characterToBaselineForm(latest, { ...baseline, life_status: 'unknown' })).toThrow()
  })

  it('只有基础值未被其他会话修改时，才可接受变化面板刷新后的版本', () => {
    const initial = characterToBaselineForm(latest, baseline)
    expect(characterBaselineMatches(initial, baseline)).toBe(true)
    expect(characterBaselineMatches(initial, { ...baseline, current_state: '筑基期' })).toBe(false)
    expect(() => characterBaselineMatches(initial, { ...baseline, age: undefined })).toThrow()
  })

  it('切换角色、重新打开和关闭抽屉都会使旧请求失效', () => {
    const gate = createCharacterRequestGate()
    const first = gate.open(3, 17)
    const oldLoad = gate.beginLoad(first)
    const newerLoad = gate.beginLoad(first)
    expect(oldLoad && gate.isCurrentLoad(oldLoad)).toBe(false)
    expect(newerLoad && gate.isCurrentLoad(newerLoad)).toBe(true)

    const second = gate.open(3, 18)
    expect(gate.isCurrent(first)).toBe(false)
    expect(newerLoad && gate.isCurrentLoad(newerLoad)).toBe(false)
    expect(gate.isCurrent(second)).toBe(true)

    const reopened = gate.open(3, 18)
    expect(gate.isCurrent(second)).toBe(false)
    expect(gate.beginLoad(second)).toBeNull()
    gate.close()
    expect(gate.isCurrent(reopened)).toBe(false)
  })

  it('切换角色后按目标恢复未保存草稿，数据不会共享引用', () => {
    const cache = createCharacterDraftCache()
    const initial = characterToBaselineForm(latest, baseline)
    const edited = { ...initial, name: '林远山', aliases: ['远山'] }
    cache.remember(latest.id, initial, edited)
    cache.remember(18, initial, { ...initial, current_state: '筑基期' })
    edited.aliases.push('被外部改动')

    const recalled = cache.recall(latest.id)
    expect(recalled?.values.name).toBe('林远山')
    expect(recalled?.values.aliases).toEqual(['远山'])
    expect(cache.recall(18)?.values.current_state).toBe('筑基期')
    recalled?.values.aliases.push('被读取方改动')
    expect(cache.recall(latest.id)?.values.aliases).toEqual(['远山'])
  })

  it('无变更、保存成功和会话变更时可清理对应草稿', () => {
    const cache = createCharacterDraftCache()
    const initial = characterToBaselineForm(latest, baseline)
    cache.remember(null, initial, { ...initial, name: '新角色草稿' })
    cache.remember(latest.id, initial, { ...initial, name: '修改' })
    cache.remember(latest.id, initial, initial)
    expect(cache.recall(latest.id)).toBeNull()
    expect(cache.recall(null)?.values.name).toBe('新角色草稿')
    cache.forget(null)
    expect(cache.recall(null)).toBeNull()
    cache.remember(18, initial, { ...initial, name: '另一角色' })
    cache.clear()
    expect(cache.recall(18)).toBeNull()
  })
})
