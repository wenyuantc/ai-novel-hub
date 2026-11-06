import { Card, Empty, Pagination, Space, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { Link } from 'react-router-dom'
import { SETTING_ENTRY_TYPE_LABELS } from '../../api/settingFields'
import type { ProgressionPositionInput, SettingCategoryEntry, SettingCategoryEntryPage } from '../../api/types'
import { useIsMobile } from '../../hooks/useIsMobile'
import { entityDetailHref } from '../../utils/entityLinks'

interface Props { projectId: number; data: SettingCategoryEntryPage; position: ProgressionPositionInput | null; onPage: (page: number, pageSize: number) => void }
const wrap = { overflowWrap: 'anywhere' as const, whiteSpace: 'normal' as const, maxWidth: '100%' }

function Tags({ item }: { item: SettingCategoryEntry }) {
  return item.setting_tags.length ? <Space wrap size={[4, 8]}>{item.setting_tags.map((tag) => <Tag key={tag} style={wrap}>{tag}</Tag>)}</Space> : <Typography.Text type="secondary">暂无整理标签</Typography.Text>
}
function Reasons({ item }: { item: SettingCategoryEntry }) {
  return <ul style={{ margin: 0, paddingInlineStart: 20, ...wrap }}>{item.reasons.map((reason, index) => <li key={`${reason.rule_index}:${index}`}>{reason.description}</li>)}</ul>
}

/** 展示服务端完整匹配后分页的结果；详情链接捕获本次结果的位置。 */
export default function CategoryEntriesView({ projectId, data, position, onPage }: Props) {
  const mobile = useIsMobile()
  const open = (item: SettingCategoryEntry) => <Link to={entityDetailHref(projectId, item.reference, position)} aria-label={`查看${item.name}的${position ? '所选章节有效设定' : '设定档案'}`}>{position ? '查看此时点' : '查看档案'}</Link>
  const columns: ColumnsType<SettingCategoryEntry> = [
    { title: '条目', key: 'name', width: '24%', render: (_, item) => <Space direction="vertical" size={4} style={{ ...wrap, minWidth: 0 }}><Typography.Text strong style={wrap}>{item.name}</Typography.Text><Typography.Text type="secondary">{SETTING_ENTRY_TYPE_LABELS[item.type]}</Typography.Text></Space> },
    { title: '当前整理标签', key: 'tags', width: '25%', render: (_, item) => <Tags item={item} /> },
    { title: '匹配理由', key: 'reasons', render: (_, item) => <Reasons item={item} /> },
    { title: '查看', key: 'open', width: 116, render: (_, item) => open(item) },
  ]
  return <Space direction="vertical" size={16} style={{ width: '100%', minWidth: 0 }}>
    <Typography.Text type="secondary">共 {data.total} 条匹配设定 · 由条件自动计算</Typography.Text>
    {!data.items.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={data.total ? '这一页没有匹配条目，可返回前一页。' : '当前条件和筛选下暂无匹配设定。'} />
      : mobile ? data.items.map((item) => <Card key={`${item.reference.kind}:${item.reference.id}`} size="small" style={{ minWidth: 0 }}>
        <Space direction="vertical" size={12} style={{ width: '100%', minWidth: 0 }}>
          <div><Typography.Text strong style={wrap}>{item.name}</Typography.Text><div><Typography.Text type="secondary">{SETTING_ENTRY_TYPE_LABELS[item.type]}</Typography.Text></div></div>
          <div><Typography.Text type="secondary">当前整理标签</Typography.Text><div style={{ marginTop: 6 }}><Tags item={item} /></div></div>
          <div><Typography.Text type="secondary">匹配理由</Typography.Text><Reasons item={item} /></div>
          {open(item)}
        </Space>
      </Card>) : <Table columns={columns} dataSource={data.items} rowKey={(item) => `${item.reference.kind}:${item.reference.id}`} pagination={false} tableLayout="fixed" size="small" />}
    {data.total > 0 && <Pagination aria-label="分类结果分页" current={data.page} pageSize={data.page_size} total={data.total} pageSizeOptions={[20, 50, 100]} showSizeChanger responsive onChange={onPage} />}
  </Space>
}
