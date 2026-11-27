import { request, type RequestOptions } from './client'
import type { Task } from './types'

type ReadScope = Pick<RequestOptions, 'signal' | 'isCurrent'>
type WriteScope = ReadScope & Required<Pick<RequestOptions, 'editorActor'>>

function probePath(configId: number) {
  return `/ai-configs/${configId}/video-probe`
}

export const videoProbeApi = {
  latest: (configId: number, scope: ReadScope) => request<Task | null>(probePath(configId), scope),
  uploadReference: (configId: number, file: File, scope: WriteScope) => {
    const body = new FormData()
    body.append('file', file)
    return request<{ object_id: string }>(probePath(configId) + '/reference', { ...scope, method: 'POST', body })
  },
  start: (configId: number, imageIds: string[], scope: WriteScope) =>
    request<Task>(probePath(configId), { ...scope, method: 'POST', body: { image_ids: imageIds } }),
}
