import { request, type RequestOptions } from './client'

export interface AdminPlugin {
  id: string
  name: string
  description: string
  version: string
  enabled: boolean
  revision: number
  updated_at: string | null
}

export const adminPluginsApi = {
  list: () => request<{ items: AdminPlugin[] }>('/admin/plugins'),
  setEnabled: (plugin: AdminPlugin, enabled: boolean, options: Pick<RequestOptions, 'isCurrent'>) =>
    request<AdminPlugin>('/admin/plugins/' + encodeURIComponent(plugin.id), {
      ...options, method: 'PATCH', body: { revision: plugin.revision, enabled },
    }),
}
