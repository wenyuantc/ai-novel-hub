import { describe, expect, it } from 'vitest'
import { ApiError } from '../api/client'
import type { BackupEstimate } from '../api/types'
import {
  BACKUP_LARGE_BYTES,
  BACKUP_MAX_BYTES,
  backupSizeHint,
  countRows,
  DEFAULT_BACKUP_OPTIONS,
  estimateTotal,
  formatBytes,
  importFileErrors,
  includedOptionsText,
  isTokenExpired,
  precheckBackupFile,
  styleActionLabel,
  tableLabel,
  tokenExpiredLocally,
  TRANSFER_TABLE_LABELS,
} from './transfer'

const g = (bytes: number, rows = 1) => ({ rows, chars: 0, bytes })

describe('统计表格标签', () => {
  it('覆盖后端全部 27 张导出表', () => {
    expect(Object.keys(TRANSFER_TABLE_LABELS)).toHaveLength(27)
    expect(tableLabel('chapter_plans')).toBe('细纲')
    expect(tableLabel('analysis_changes')).toBe('分析回写条目')
    expect(tableLabel('new_table')).toBe('new_table')
  })
  it('按固定顺序、去掉 0 行、未知表排在后面', () => {
    const rows = countRows({ chapters: 3, characters: 2, zeta: 1, alpha: 4, outlines: 0 })
    expect(rows.map((r) => r.table)).toEqual(['characters', 'chapters', 'alpha', 'zeta'])
    expect(rows[0]).toEqual({ table: 'characters', label: '角色', count: 2 })
    expect(countRows({})).toEqual([])
  })
})

describe('大小格式化', () => {
  it('B / KB / MB / GB', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(-1)).toBe('0 B')
    expect(formatBytes(NaN)).toBe('0 B')
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(1536)).toBe('1.5 KB')
    expect(formatBytes(100 * 1024 * 1024)).toBe('100.0 MB')
    expect(formatBytes(3 * 1024 ** 3)).toBe('3.0 GB')
  })
})

describe('备份估算', () => {
  const est: BackupEstimate = {
    core: g(1000),
    versions: g(200),
    snapshots: g(30),
    generation_logs: g(4000),
    inspiration: g(5),
  }
  it('核心始终计入，可选项按勾选累加', () => {
    expect(estimateTotal(est, DEFAULT_BACKUP_OPTIONS)).toBe(1200)
    expect(estimateTotal(est, { versions: false, snapshots: true, generation_logs: true, inspiration: true })).toBe(5035)
  })
  it('较大 warning、超过导入上限 error', () => {
    expect(backupSizeHint(BACKUP_LARGE_BYTES)).toBeNull()
    expect(backupSizeHint(BACKUP_LARGE_BYTES + 1)?.type).toBe('warning')
    expect(backupSizeHint(BACKUP_MAX_BYTES + 1)?.type).toBe('error')
  })
  it('默认只勾章节版本；可选项文案', () => {
    expect(DEFAULT_BACKUP_OPTIONS).toEqual({ versions: true, snapshots: false, generation_logs: false, inspiration: false })
    expect(includedOptionsText(DEFAULT_BACKUP_OPTIONS)).toBe('章节版本')
    expect(includedOptionsText({ versions: false, snapshots: false, generation_logs: false, inspiration: false })).toBe('无')
    expect(includedOptionsText({ versions: true, snapshots: true, generation_logs: true, inspiration: true })).toBe(
      '章节版本、大纲快照、生成记录、关联的灵感会话',
    )
  })
})

describe('导入', () => {
  it('上传前检查', () => {
    expect(precheckBackupFile({ name: 'a.ainovel.JSON', size: 10 })).toBeNull()
    expect(precheckBackupFile({ name: 'a.txt', size: 10 })).toContain('.json')
    expect(precheckBackupFile({ name: 'a.json', size: 0 })).toBe('文件为空')
    expect(precheckBackupFile({ name: 'a.json', size: BACKUP_MAX_BYTES })).toBeNull()
    expect(precheckBackupFile({ name: 'a.json', size: BACKUP_MAX_BYTES + 1 })).toBe('备份文件过大，上限为 100MB')
  })
  it('400 响应体的 errors', () => {
    const err = new ApiError(400, '备份文件校验失败：…', { detail: 'x', errors: ['角色第 1 行缺少字段 name', 1, ''] })
    expect(importFileErrors(err)).toEqual(['角色第 1 行缺少字段 name'])
    expect(importFileErrors(new ApiError(400, 'x', { detail: 'x' }))).toEqual([])
    expect(importFileErrors(new ApiError(500, 'x', { errors: ['a'] }))).toEqual([])
    expect(importFileErrors(new Error('x'))).toEqual([])
  })
  it('令牌过期判断', () => {
    expect(isTokenExpired(new ApiError(400, '导入凭据已过期，请重新选择备份文件'))).toBe(true)
    expect(isTokenExpired(new ApiError(400, '导入失败：…'))).toBe(false)
    expect(tokenExpiredLocally(10_000, 4_999)).toBe(false)
    expect(tokenExpiredLocally(10_000, 5_000)).toBe(true)
  })
  it('风格处理标签，未知动作原样显示', () => {
    expect(styleActionLabel('reuse').label).toBe('复用已有')
    expect(styleActionLabel('copy').label).toContain('复制')
    expect(styleActionLabel('weird')).toEqual({ label: 'weird', color: 'default' })
  })
})
