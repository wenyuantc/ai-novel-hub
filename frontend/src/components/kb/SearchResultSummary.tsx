import { Alert, Space, Typography } from 'antd'
import type { SearchMode } from '../../api/types'
import { searchSummary } from '../../utils/kb'

interface Props {
  result: { mode: SearchMode; candidates: number; hits: unknown[]; warnings: string[] }
}

/** 检索调试结果的模式说明与 warnings（含「部分内容缺少当前向量，本次只用关键词」）。 */
export default function SearchResultSummary({ result }: Props) {
  return (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      <Typography.Text type="secondary">{searchSummary(result)}</Typography.Text>
      {result.warnings.map((w) => (
        <Alert key={w} type="warning" showIcon message={w} />
      ))}
    </Space>
  )
}
