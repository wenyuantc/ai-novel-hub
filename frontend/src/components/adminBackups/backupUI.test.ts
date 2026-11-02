import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { auditResult, AUDIT_ACTION, AUDIT_TARGET } from '../../api/auditLogs'
import { auditSummaryRows } from '../../utils/auditLogs'
import type { AuditSummary } from '../../api/types'
import BackupRestoreGuide from './BackupRestoreGuide'
import { BACKUP_ARTIFACT_STATUS, BACKUP_CLEANUP_STATUS, BACKUP_MAIL_STATUS, BACKUP_RUN_STATUS, backupSize } from './backupDisplay'

// 结构检查只辅助真实浏览器验收，不冒充视觉/点击/下载证明。
describe('备份语义和恢复说明', () => {
  it('不把部分成功、邮件受理或清理失败误作全部成功', () => {
    expect(BACKUP_RUN_STATUS.partial.label).toBe('部分成功')
    expect(BACKUP_ARTIFACT_STATUS.unknown).toBe('结果未知')
    expect(BACKUP_MAIL_STATUS.sent).toBe('SMTP 已受理')
    expect(BACKUP_CLEANUP_STATUS.failed).toBe('清理失败')
    expect(backupSize(0)).toBe('0 B'); expect(backupSize(null)).toBe('—')
    expect(backupSize(1024 ** 3 * 2)).toBe('2.00 GiB')
  })
  it('审计只表示保存、受理与授权下载', () => {
    expect(auditResult({ action: 'backup.settings.update', result: 'success' }).label).toBe('已保存')
    expect(auditResult({ action: 'backup.run.accept', result: 'success' }).label).toBe('已受理')
    expect(auditResult({ action: 'backup.download.authorize', result: 'success' }).label).toBe('已授权下载')
    expect(auditResult({ action: 'backup.download.authorize', result: 'failure' }).label).toBe('失败')
    expect(AUDIT_ACTION['backup.run.accept']).toBe('受理整库备份')
    expect(AUDIT_TARGET.backup_run).toBe('整库备份')
  })
  it('恢复说明包含原主密钥、旧密码和完整解密校验；不提供在线覆盖按钮', () => {
    const html = renderToStaticMarkup(createElement(BackupRestoreGuide))
    expect(html).toContain('ENCRYPTION_KEY')
    expect(html).toContain('旧文件')
    expect(html).toContain('不要将尚未校验完成的解密流直接导入数据库')
    expect(html).toContain('pg_restore')
    expect(html).not.toContain('立即恢复')
  })
  it('只在对应备份动作投影有界摘要，不显示秘密值或夸大完成状态', () => {
    const summary: AuditSummary = {
      changed_fields: [], changes: [], word_count_before: null, word_count_after: null, body_chars_before: null, body_chars_after: null,
      reference_count_before: null, reference_count_after: null, affected_count: null, affected_ids: [], ids_truncated: false,
      chapter_count: null, version_count: null, from_at: null, to_at: null, expected_count: null, deleted_count: null, high_water_id: null,
      backup_change: { fields: ['backup_password', 'smtp_host'], settings_revision: 4, execution_key: null, destination: 's3', encrypted: true },
    }
    const rows = auditSummaryRows(summary, 'backup.download.authorize')
    expect(rows).toContainEqual({ label: '备份配置版本', value: '#4' })
    expect(rows).toContainEqual({ label: '备份配置变更字段', value: '备份密码、SMTP主机' })
    expect(rows).toContainEqual(expect.objectContaining({ value: '仅表示已授权下载，不代表文件已送达客户端。' }))
    expect(auditSummaryRows(summary, 'user.update').some((row) => row.label.startsWith('备份'))).toBe(false)
  })
})
