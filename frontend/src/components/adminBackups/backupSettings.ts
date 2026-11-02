import type { BackupSecretKey, BackupSettingsChanges } from '../../api/types'

export const BACKUP_SECRET_LABELS: Record<BackupSecretKey, string> = {
  backup_password: '备份密码', s3_access_key: 'S3 Access Key', s3_secret_key: 'S3 Secret Key',
  smtp_username: 'SMTP 用户名', smtp_password: 'SMTP 密码',
}
export const BACKUP_SECRET_KEYS: readonly BackupSecretKey[] = ['backup_password', 's3_access_key', 's3_secret_key', 'smtp_username', 'smtp_password']

/** 空输入只表示不修改；秘密不trim，显式null才能清除。 */
export function backupChanges(changes: BackupSettingsChanges): BackupSettingsChanges {
  const result = { ...changes }
  for (const key of BACKUP_SECRET_KEYS) if (result[key] === '') delete result[key]
  return result
}

export function backupDraftError(changes: BackupSettingsChanges): string | null {
  for (const key of BACKUP_SECRET_KEYS) {
    const value = changes[key]
    if (typeof value !== 'string' || value === '') continue
    if (value.length < (key === 'backup_password' ? 16 : 1) || value.length > 1024 || /[\r\n\0]/.test(value)) return `${BACKUP_SECRET_LABELS[key]}长度或字符无效${key === 'backup_password' ? '（需要16至1024个字符）' : ''}`
  }
  if (changes.schedule_time !== undefined && !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(changes.schedule_time)) return '请输入有效的北京时间 HH:mm'
  return null
}
