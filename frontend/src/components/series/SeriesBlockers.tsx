import { Alert, Typography } from 'antd'
import type { SeriesBlocker } from '../../api/series'

export default function SeriesBlockers({ blockers }: { blockers: SeriesBlocker[] }) {
  if (!blockers.length) return null
  return <Alert type="warning" showIcon message="无法调整归属" description={<>
    <Typography.Paragraph>请先处理下列作品中的依赖；不会删除或改写这些记录。</Typography.Paragraph>
    <ul style={{ margin: 0, paddingInlineStart: 22 }}>
      {blockers.map((book) => <li key={book.project_id}><strong>{book.title}{book.recycled ? '（回收站）' : ''}</strong>：{book.reasons.join('、')}</li>)}
    </ul>
  </>} />
}
