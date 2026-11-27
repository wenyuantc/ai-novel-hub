import { VideoCameraOutlined } from '@ant-design/icons'
import type { FeaturePlugin } from '../types'

export const dramaPlugin: FeaturePlugin = {
  id: 'drama',
  tasks: {
    'drama.rewrite': { label: '改写短剧剧本', collaboratorStream: true },
    'drama.extract': { label: '提取短剧素材', collaboratorStream: true },
    'drama.storyboard': { label: '拆解短剧分镜', collaboratorStream: true },
    'drama.prompts': { label: '生成短剧提示词', collaboratorStream: true },
  },
  project: [
    {
      path: 'drama',
      nav: { group: '改编', label: '短剧', icon: <VideoCameraOutlined /> },
      load: () => import('./pages/SeriesList'),
    },
    { path: 'drama/:sid', load: () => import('./pages/SeriesDetail') },
    { path: 'drama/:sid/episodes/:eid', load: () => import('./pages/EpisodeWorkbench') },
  ],
}
