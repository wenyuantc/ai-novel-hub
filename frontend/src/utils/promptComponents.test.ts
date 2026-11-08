import { describe, expect, it } from 'vitest'
import { ApiError } from '../api/client'
import {
  componentRef,
  insertSnippet,
  isStaleRevision,
  readFailures,
  readUsages,
  refSlugs,
  summarizeComponentImport,
  usageHref,
  usageLabel,
} from './promptComponents'

describe('组件引用写法与插入', () => {
  it('引用写法与后端正则同口径，去重保序', () => {
    expect(componentRef('banned-words')).toBe('{{> banned-words }}')
    expect(refSlugs('甲{{> a }}乙{{>b}}丙{{>   a   }}{{ x }}{{> Bad }}')).toEqual(['a', 'b'])
    expect(refSlugs('{{ banned }}')).toEqual([])
  })

  it('在光标处插入、替换选区，没有光标时插在末尾，越界夹紧', () => {
    expect(insertSnippet('前后', '{{> a }}', [1, 1])).toEqual({ text: '前{{> a }}后', caret: 9 })
    expect(insertSnippet('前选中后', 'X', [1, 3])).toEqual({ text: '前X后', caret: 2 })
    expect(insertSnippet('正文', '{{> a }}', null)).toEqual({ text: '正文{{> a }}', caret: 10 })
    expect(insertSnippet('ab', 'X', [9, 12])).toEqual({ text: 'abX', caret: 3 })
  })
})

describe('结构化错误与引用方', () => {
  it('只在对应状态码与 code 下识别', () => {
    expect(isStaleRevision(new ApiError(409, 'x', { code: 'stale_revision' }))).toBe(true)
    expect(isStaleRevision(new ApiError(400, 'x', { code: 'stale_revision' }))).toBe(false)
    expect(readUsages(new ApiError(409, 'x', { code: 'other' }))).toBeNull()
    expect(readUsages(new ApiError(409, 'x', { code: 'component_in_use', usages: [{ kind: 'bad' }, { kind: 'skill', id: 1, name: 'S' }] })))
      .toEqual([{ kind: 'skill', id: 1, key: null, name: 'S', enabled: null }])
    expect(readFailures(new ApiError(409, 'x', { code: 'component_breaks_usages' }))).toBeNull()
    expect(readFailures(new ApiError(400, 'x', { code: 'component_breaks_usages', failures: [{ key: 'k', errors: [{ message: 'm' }] }, 3] })))
      .toEqual([{ key: 'k', name: 'k', errors: [{ part: null, line: null, message: 'm' }] }])
  })

  it('引用方跳转地址与类型文案', () => {
    expect(usageHref({ kind: 'skill', id: 7, key: null })).toBe('/skills?skill=7')
    expect(usageHref({ kind: 'override', id: null, key: 'chapter.generate' })).toBe('/prompt-templates?key=chapter.generate')
    expect(usageHref({ kind: 'skill', id: null, key: null })).toBeNull()
    expect(usageLabel({ kind: 'override', enabled: false })).toBe('模板覆盖（已停用）')
    expect(usageLabel({ kind: 'override', enabled: true })).toBe('模板覆盖')
    expect(usageLabel({ kind: 'skill', enabled: null })).toBe('Skill')
  })

  it('导入结果按新建 / 更新 / 不变 / 无效汇总', () => {
    const item = (status: 'created' | 'updated' | 'unchanged' | 'invalid') => ({ slug: 's', status, message: '', failures: [] })
    expect(summarizeComponentImport({ items: [item('invalid'), item('created'), item('created'), item('unchanged')] }))
      .toBe('新建 2 个、不变 1 个、无效 1 个')
    expect(summarizeComponentImport({ items: [] })).toBe('文件中没有组件')
  })
})
