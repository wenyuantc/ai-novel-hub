import { describe, expect, it } from 'vitest'
import { ApiError } from '../api/client'
import type { PromptTemplateBrief } from '../api/types'
import {
  formatIssue,
  groupTemplates,
  issuesForPart,
  issuesFromError,
  lineRange,
  readIssues,
  splitLocked,
  summarizePromptImport,
  templateBadges,
} from './prompts'

function brief(key: string, category: string, label: string, extra: Partial<PromptTemplateBrief> = {}): PromptTemplateBrief {
  return {
    key,
    name: key,
    category,
    category_label: label,
    description: '',
    overridden: false,
    enabled: false,
    outdated: false,
    updated_at: null,
    ...extra,
  }
}

describe('校验错误格式化与定位', () => {
  it('与后端 format_issue 同口径', () => {
    expect(formatIssue({ part: 'user', line: 3, message: '未知变量：x' })).toBe('用户段第 3 行：未知变量：x')
    expect(formatIssue({ part: 'system', line: null, message: '缺少必需变量：style' })).toBe('系统段：缺少必需变量：style')
    expect(formatIssue({ part: null, line: null, message: '模板渲染失败' })).toBe('模板渲染失败')
  })

  it('从 400 响应体读取 errors，过滤非法项', () => {
    const err = new ApiError(400, '模板校验失败：…', {
      detail: '模板校验失败：…',
      errors: [{ part: 'user', line: 2, message: 'a' }, { part: 'x', message: 'bad' }, 'junk', { message: 'b' }],
    })
    expect(issuesFromError(err)).toEqual([
      { part: 'user', line: 2, message: 'a' },
      { part: null, line: null, message: 'b' },
    ])
    expect(issuesFromError(new ApiError(400, 'x'))).toEqual([])
    expect(issuesFromError(new ApiError(409, 'x', { errors: [{ part: 'user', line: 1, message: 'a' }] }))).toEqual([])
    expect(readIssues(null)).toEqual([])
  })

  it('按段筛选并按行排序', () => {
    const list = issuesForPart(
      [
        { part: 'user', line: 5, message: 'c' },
        { part: 'system', line: 1, message: 's' },
        { part: 'user', line: null, message: 'z' },
        { part: 'user', line: 2, message: 'a' },
      ],
      'user',
    )
    expect(list.map((i) => i.message)).toEqual(['a', 'c', 'z'])
  })

  it('行号 → 偏移区间（越界夹到首末行）', () => {
    const text = 'ab\ncde\n\nf'
    expect(lineRange(text, 1)).toEqual([0, 2])
    expect(lineRange(text, 2)).toEqual([3, 6])
    expect(lineRange(text, 3)).toEqual([7, 7])
    expect(lineRange(text, 4)).toEqual([8, 9])
    expect(lineRange(text, 99)).toEqual([8, 9])
    expect(lineRange(text, 0)).toEqual([0, 2])
  })
})

describe('分类分组与徽标', () => {
  it('分类按首次出现排序，组内保持注册顺序', () => {
    const groups = groupTemplates([
      brief('chapter.generate', 'chapter', '章节'),
      brief('analysis.chapter', 'analysis', '分析'),
      brief('chapter.polish', 'chapter', '章节'),
      brief('style.block', 'style', ''),
    ])
    expect(groups.map((g) => [g.label, g.items.map((i) => i.key)])).toEqual([
      ['章节', ['chapter.generate', 'chapter.polish']],
      ['分析', ['analysis.chapter']],
      ['style', ['style.block']],
    ])
  })

  it('已覆盖 / 已停用 / 默认已更新', () => {
    expect(templateBadges({ overridden: false, enabled: false, outdated: false })).toEqual([])
    expect(templateBadges({ overridden: true, enabled: true, outdated: false }).map((b) => b.label)).toEqual(['已覆盖'])
    expect(templateBadges({ overridden: true, enabled: false, outdated: true }).map((b) => b.label)).toEqual([
      '已停用',
      '默认已更新',
    ])
  })
})

describe('预览锁定段切分', () => {
  const locked = [
    { name: 'output_schema', label: '输出结构', value: '{"a": 1}' },
    { name: 'json_rule', label: '只输出 JSON', value: '只输出 JSON。' },
    { name: 'empty', label: '空', value: '' },
  ]
  it('锁定内容单独成段，其余为普通文本', () => {
    const segs = splitLocked('前言\n{"a": 1}\n中间\n只输出 JSON。', locked)
    expect(segs).toEqual([
      { text: '前言\n', locked: null },
      { text: '{"a": 1}', locked: '输出结构' },
      { text: '\n中间\n', locked: null },
      { text: '只输出 JSON。', locked: '只输出 JSON' },
    ])
  })
  it('没有锁定内容时整段原样', () => {
    expect(splitLocked('abc', locked)).toEqual([{ text: 'abc', locked: null }])
    expect(splitLocked('', locked)).toEqual([])
  })
})

describe('导入结果汇总', () => {
  it('按状态计数，只列出非零项', () => {
    const r = summarizePromptImport({
      items: [
        { key: 'a', status: 'created', message: '', errors: [] },
        { key: 'b', status: 'created', message: '', errors: [] },
        { key: 'c', status: 'invalid', message: '', errors: [{ part: 'user', line: 1, message: 'x' }] },
        { key: 'd', status: 'skipped', message: '未知模板', errors: [] },
      ],
    })
    expect(r.counts.created).toBe(2)
    expect(r.counts.invalid).toBe(1)
    expect(r.text).toBe('已新建覆盖 2，已跳过 1，校验失败 1')
    expect(summarizePromptImport({ items: [] }).text).toBe('文件中没有模板')
  })
})
