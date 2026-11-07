import type { ProgressionBoundary, ProgressionPositionInput } from '../api/types'

export interface StoryPositionQuery { chapter_id?: number; chapter_no?: number; scene_id?: number; boundary: ProgressionBoundary }
/** 三种位置互斥：真实 Scene 只传稳定 ID，顺序和所属章永远由服务端解析。 */
export function storyPositionQuery(position: ProgressionPositionInput): StoryPositionQuery {
  const selected = position.anchor_kind === 'chapter' ? position.chapter_id : position.anchor_kind === 'scene' ? position.scene_id : position.chapter_no
  const others = position.anchor_kind === 'chapter' ? [position.chapter_no, position.scene_id] : position.anchor_kind === 'scene' ? [position.chapter_id, position.chapter_no] : [position.chapter_id, position.scene_id]
  if (selected == null || !Number.isSafeInteger(selected) || selected < 1 || selected > 2_147_483_647 || others.some((id) => id != null) || !['start', 'end'].includes(position.boundary)) throw new Error('请选择有效章节或场景，不能混用位置')
  return position.anchor_kind === 'chapter' ? { chapter_id: selected, boundary: position.boundary } : position.anchor_kind === 'scene' ? { scene_id: selected, boundary: position.boundary } : { chapter_no: selected, boundary: position.boundary }
}
