import { Tag, Tooltip } from 'antd'
import type { Memory } from '../../api/types'
import { memorySourceStatus } from '../../utils/kb'

/** 已关联的自动记忆可展示来源状态；未知来源不伪装为有效。 */
export default function MemorySourceBadge({ memory }: { memory: Pick<Memory, 'origin' | 'source_stale'> }) {
  const status = memorySourceStatus(memory)
  if (status === 'stale') {
    return (
      <Tooltip title="来源章节或启用的作者区块已变化；该记忆仍可能被召回，可重新分析或手动屏蔽。">
        <Tag color="warning">来源分析已过期</Tag>
      </Tooltip>
    )
  }
  if (status === 'unknown') {
    return <Tooltip title="没有可验证的关联分析，无法判断此记忆是否过期。"><Tag>来源未核实</Tag></Tooltip>
  }
  return null
}
