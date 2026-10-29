import { DownloadOutlined, LikeOutlined } from '@ant-design/icons'
import { Button, Card, Space, Tag, Typography } from 'antd'
import type { WorkshopItem } from '../../api/types'
import { WORKSHOP_CATEGORIES, WORKSHOP_KINDS } from '../../utils/workshop'
import { formatTime } from '../../utils/time'

export default function WorkshopItemCard({ item, onOpen }: { item: WorkshopItem; onOpen: (item: WorkshopItem) => void }) {
  return <Card size="small" style={{ height: '100%', minWidth: 0, overflowWrap: 'anywhere' }}
    title={<Typography.Text ellipsis title={item.name}>{item.name}</Typography.Text>}
    extra={item.is_official && <Tag color="blue">官方</Tag>}
    actions={[<Button key="detail" type="link" onClick={() => onOpen(item)}>查看详情</Button>]}>
    <Space wrap size={[4, 8]}>
      <Tag>{WORKSHOP_KINDS[item.kind]}</Tag><Tag>{WORKSHOP_CATEGORIES[item.category]}</Tag>
      {item.status === 'hidden' && <Tag color="orange">{item.hidden_by === 'admin' ? '管理员下架' : '已下架'}</Tag>}
      {item.imported && <Tag color="green">已导入</Tag>}
      {item.liked && <Tag color="magenta">已赞</Tag>}
    </Space>
    <Typography.Paragraph ellipsis={{ rows: 2 }} style={{ marginTop: 12 }}>{item.description || '暂无说明'}</Typography.Paragraph>
    {item.prompt_key && <Typography.Paragraph type="secondary" style={{ overflowWrap: 'anywhere' }}>{item.prompt_key}</Typography.Paragraph>}
    <Space wrap size={[4, 8]}>{item.tags.map((t) => <Tag key={t}>{t}</Tag>)}</Space>
    <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 8 }}>作者：{item.author_name ?? '匿名'}{item.is_anonymous && item.author_username && `（匿名分享，账号：${item.author_username}）`}</Typography.Paragraph>
    <Space wrap><span><LikeOutlined /> {item.like_count}</span><span><DownloadOutlined /> {item.download_count}</span><Typography.Text type="secondary">{formatTime(item.updated_at)}</Typography.Text></Space>
  </Card>
}
