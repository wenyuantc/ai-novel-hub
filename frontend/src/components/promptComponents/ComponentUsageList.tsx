import { Empty, Space, Tag, Typography } from 'antd'
import { Link } from 'react-router-dom'
import type { PromptComponentFailure, PromptComponentUsage } from '../../api/types'
import { usageHref, usageLabel } from '../../utils/promptComponents'
import { formatIssue } from '../../utils/prompts'

const wrap = { overflowWrap: 'anywhere' as const }

/** 引用方列表：类型、名称（可跳转到对应编辑页）、覆盖是否启用 */
export function ComponentUsageList({ usages, empty = '暂无引用' }: { usages: PromptComponentUsage[]; empty?: string }) {
  if (!usages.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={empty} />
  return (
    <ul aria-label="组件引用方" style={{ margin: 0, paddingLeft: 18 }}>
      {usages.map((u) => {
        const href = usageHref(u)
        return (
          <li key={`${u.kind}-${u.id ?? u.key}`} style={wrap}>
            <Space size={4} wrap>
              <Tag color={u.kind === 'skill' ? 'blue' : u.enabled === false ? 'default' : 'purple'}>{usageLabel(u)}</Tag>
              {href ? <Link to={href}>{u.name}</Link> : <span>{u.name}</span>}
              {u.kind === 'override' && u.key && (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {u.key}
                </Typography.Text>
              )}
            </Space>
          </li>
        )
      })}
    </ul>
  )
}

/** 改内容被拒时，逐个受影响覆盖列出校验错误 */
export function ComponentFailureList({ failures }: { failures: PromptComponentFailure[] }) {
  return (
    <ul aria-label="受影响的模板覆盖" style={{ margin: 0, paddingLeft: 18 }}>
      {failures.map((f) => (
        <li key={f.key} style={wrap}>
          <Link to={usageHref({ kind: 'override', id: null, key: f.key }) ?? '#'}>模板「{f.name}」</Link>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {f.errors.map((e, i) => (
              <li key={i}>
                <Typography.Text type="danger">{formatIssue(e)}</Typography.Text>
              </li>
            ))}
          </ul>
        </li>
      ))}
    </ul>
  )
}
