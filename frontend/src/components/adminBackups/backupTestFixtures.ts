import type { BackupOverview, BackupRunOut, BackupSettingsOut } from '../../api/types'

export function backupSettings(revision = 1): BackupSettingsOut {
  return {
    revision, updated_at: null, timezone: 'Asia/Shanghai', schedule_enabled: false, schedule_time: '03:00',
    local_enabled: true, s3_enabled: false, local_keep: 7, s3_keep: 7, encryption_enabled: true,
    s3_endpoint: '', s3_region: 'us-east-1', s3_bucket: '', s3_prefix: 'ainovel',
    smtp_host: '', smtp_port: 465, smtp_security: 'ssl', smtp_from_address: '', smtp_recipients: [],
    backup_password_configured: true, s3_access_key_configured: false, s3_secret_key_configured: false,
    smtp_username_configured: false, smtp_password_configured: false,
  }
}
export function backupRun(id = 1): BackupRunOut {
  return {
    id, execution_key: '7e0b1eb7-376e-4e0a-992e-5c79884743bd', source: 'manual', actor_user_id: 1,
    actor_username: 'operator', settings_revision: 1, database_engine: 'sqlite', schema_revision: '0030',
    encrypted: true, file_format: 'sqlite3.gpg', status: 'queued', created_at: '2026-11-02T00:00:00Z',
    started_at: null, finished_at: null, error_code: null, error_message: null, notification_status: 'not_configured',
    notification_error: null, cleanup_status: 'not_required', cleanup_error: null, artifacts: [],
  }
}
export function backupOverview(revision = 1): BackupOverview {
  return {
    revision, schedule_enabled: false, schedule_time: '03:00', timezone: 'Asia/Shanghai', local_enabled: true,
    s3_enabled: false, local_keep: 7, s3_keep: 7, encryption_enabled: true, ready: true, readiness_errors: [],
    notification_ready: false, next_scheduled_at: null, active_run: null, last_success: null, last_failure: null, last_available: null,
  }
}
export function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}
