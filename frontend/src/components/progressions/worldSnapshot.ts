import { progressionApi } from '../../api/progressions'
import type { SettingBaseline, WorldSetting } from '../../api/types'

/** 元数据不是时间线字段；在两次版本读取之间读取它，避免采用混合快照。 */
export async function readConsistentWorldSnapshot(
  projectId: number, worldId: number, signal: AbortSignal,
): Promise<{ baseline: SettingBaseline; item: WorldSetting }> {
  const ensureActive = () => { if (signal.aborted) throw new DOMException('请求已取消', 'AbortError') }
  const timeline = await progressionApi.findTimeline(projectId, { subject_kind: 'world', world_setting_id: worldId }, signal)
  ensureActive()
  if (!timeline) throw new Error('未找到该条目的基础设定，请稍后重试')
  const before = await progressionApi.getBaseline(projectId, timeline.id, signal)
  ensureActive()
  const item = await progressionApi.getWorldSetting(projectId, worldId, signal)
  ensureActive()
  const baseline = await progressionApi.getBaseline(projectId, timeline.id, signal)
  ensureActive()
  if (before.timeline_revision !== baseline.timeline_revision) throw new Error('设定正在发生变化，请刷新后重试')
  return { baseline, item }
}
