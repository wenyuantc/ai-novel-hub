import type { SettingThumbnailInfo } from '../../api/types'

type ThumbnailEntity = {
  id: number
  thumbnail?: SettingThumbnailInfo | null
  thumbnail_editable?: boolean
}

/** 文字保存期间如有图片结果，保留更晚提交到界面的图片状态。 */
export function createThumbnailRevision() {
  let revision = 0
  return {
    capture: () => revision,
    changed: () => { revision += 1 },
    merge: <T extends ThumbnailEntity>(saved: T, current: T | null, ticket: number): T => {
      if (!current || current.id !== saved.id || revision === ticket) return saved
      return { ...saved, thumbnail: current.thumbnail, thumbnail_editable: current.thumbnail_editable }
    },
  }
}

/** 编辑器关闭后仍在同一作品时重新读取，不能把迟到结果写入新的编辑器或账号。 */
export async function settleThumbnailWrite<T>(
  operation: () => Promise<T>, editorCurrent: () => boolean, scopeCurrent: () => boolean,
  apply: (result: T) => void, invalidate: () => void,
): Promise<void> {
  try {
    const result = await operation()
    if (editorCurrent()) apply(result)
    else if (scopeCurrent()) invalidate()
  } catch (err) {
    // 未知提交结果也需在安全作用域内重新读取；失效的旧账号不能触发刷新。
    if (!editorCurrent() && scopeCurrent()) invalidate()
    throw err
  }
}
