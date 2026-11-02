import { buildUrl, rawRequest, request } from './client'
import type { BackupDestination, BackupManualRequest, BackupOverview, BackupRunFilters, BackupRunList, BackupRunOut, BackupSettingsOut, BackupSettingsUpdate } from './types'

const base = '/admin/backups'

export const adminBackupsApi = {
  overview: (signal?: AbortSignal) => request<BackupOverview>(base, { signal }),
  settings: (signal?: AbortSignal) => request<BackupSettingsOut>(`${base}/settings`, { signal }),
  update: (body: BackupSettingsUpdate, signal?: AbortSignal) => request<BackupSettingsOut>(`${base}/settings`, { method: 'PATCH', body, signal }),
  list: (query: BackupRunFilters = {}, signal?: AbortSignal) => request<BackupRunList>(`${base}/runs`, { query: { ...query }, signal }),
  get: (id: number, signal?: AbortSignal) => request<BackupRunOut>(`${base}/runs/${id}`, { signal }),
  run: (body: BackupManualRequest, signal?: AbortSignal) => request<BackupRunOut>(`${base}/runs`, { method: 'POST', body, signal }),
  download: (id: number, destination: BackupDestination, allowed: () => boolean, signal?: AbortSignal) => startBackupDownload(id, destination, allowed, signal),
}

/** HEAD只预检。GET交由浏览器附件下载，避免整库Blob占用页面内存。 */
export async function startBackupDownload(id: number, destination: BackupDestination, allowed: () => boolean, signal?: AbortSignal): Promise<boolean> {
  if (!Number.isSafeInteger(id) || id < 1 || !allowed() || signal?.aborted) return false
  const path = `${base}/runs/${id}/download`
  await rawRequest(path, { method: 'HEAD', query: { destination }, signal })
  if (!allowed() || signal?.aborted) return false
  const link = document.createElement('a')
  link.href = buildUrl(path, { destination })
  link.download = ''
  link.rel = 'noopener'
  document.body.appendChild(link)
  try { link.click() } finally { link.remove() }
  return true
}
