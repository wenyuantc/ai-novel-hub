import type { SceneExtractPreview, SceneRestorePreview } from '../../api/types'

/** 必须对应当前预览显式同意，切换目标或重做预览时由调用方重置。 */
export function mayApplyRichHistory(preview: SceneExtractPreview | SceneRestorePreview | undefined, confirmed: boolean): boolean {
  return !preview?.requires_rich_confirmation || confirmed
}
