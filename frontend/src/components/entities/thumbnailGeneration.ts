import type { ThumbnailTarget } from '../../api/settingThumbnails'
import type { AIConfig, ProjectAccess } from '../../api/types'
import { coverChoice } from '../project/coverGeneration'

/** 系列是独立授权域，绝不使用作品作者的模型回退。 */
export function thumbnailModelChoice(target: ThumbnailTarget, access: ProjectAccess | null | undefined,
  loaded: boolean, configs: AIConfig[], selectedId: number | null) {
  return coverChoice(target.scope === 'project' ? access : null, loaded, configs, selectedId)
}
