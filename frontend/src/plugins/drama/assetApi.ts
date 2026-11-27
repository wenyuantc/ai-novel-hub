import { api } from '../../api/client'
import { dramaWrite } from './write'
import type { AssetFields, AssetKind, DramaAsset, DramaAssets, EpisodeVersion, MemberKind } from './assetTypes'

const base = '/plugins/drama'
export const dramaAssetApi = {
  series: (id: number) => api.get<DramaAssets>(`${base}/series/${id}/assets`),
  episode: (id: number) => api.get<DramaAssets>(`${base}/episodes/${id}/assets`),
  create: (kind: AssetKind, parent: number, body: AssetFields, episode?: EpisodeVersion) =>
    dramaWrite<DramaAsset>(`${base}/${kind === 'looks' ? 'characters' : 'series'}/${parent}/${kind}`, {
      method: 'POST',
      body,
      query: episode && { episode_id: episode.id, episode_revision: episode.revision },
    }),
  patch: (kind: AssetKind, id: number, body: AssetFields) =>
    dramaWrite<DramaAsset>(`${base}/${kind}/${id}`, { method: 'PATCH', body }),
  remove: (kind: AssetKind, id: number, revision: number) =>
    dramaWrite<void>(`${base}/${kind}/${id}`, { method: 'DELETE', query: { revision } }),
  membership: (episodeId: number, kind: MemberKind, id: number, revision: number, add: boolean) =>
    dramaWrite<void>(`${base}/episodes/${episodeId}/assets/${kind}/${id}`, {
      method: add ? 'PUT' : 'DELETE',
      query: { revision },
    }),
}
