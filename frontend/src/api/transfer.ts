// 项目备份（ainovel.project）：大小估算 / 下载备份 / 导入预览 / 导入。只被备份与导入弹窗（懒加载）引用，不进入口 chunk。

import { api } from './client'
import { downloadAttachment } from './projects'
import type { BackupEstimate, BackupOptions, ImportPreview, ImportRequest, ImportResult } from './types'

/** 备份下载的查询参数（纯函数）：generation_logs → logs */
export function backupQuery(options: BackupOptions): Record<string, boolean> {
  return {
    versions: options.versions,
    snapshots: options.snapshots,
    logs: options.generation_logs,
    inspiration: options.inspiration,
  }
}

export const transferApi = {
  estimate: (projectId: number) => api.get<BackupEstimate>(`/projects/${projectId}/backup/estimate`),
  /** 流式 JSON 附件，文件名取 Content-Disposition（RFC 5987） */
  download: async (projectId: number, options: BackupOptions): Promise<string> =>
    (await downloadAttachment(`/projects/${projectId}/backup`, backupQuery(options), 'backup.ainovel.json')).filename,
  preview: (file: File, signal?: AbortSignal) => {
    const form = new FormData()
    form.append('file', file)
    return api.upload<ImportPreview>('/projects/import/preview', form, signal)
  },
  importBackup: (body: ImportRequest) => api.post<ImportResult>('/projects/import', body),
}
