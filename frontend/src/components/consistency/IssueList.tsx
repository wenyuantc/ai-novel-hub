import { Button, Empty, Form, List, Pagination, Select, Space, Tag, Typography } from 'antd'
import type { ReactNode } from 'react'
import type { ConsistencyIssue, FactCategory, IssueStatus } from '../../api/consistency'
import {
  hasIssueFilter, ISSUE_CATEGORY_LABELS, ISSUE_PAGE_SIZE, ISSUE_SEVERITY, ISSUE_STATUS, issueChaptersLabel,
  type IssueFilter, type RecheckFilter,
} from '../../utils/consistency'
import AutoGrid from '../AutoGrid'

export function IssueTags({ issue }: { issue: ConsistencyIssue }) {
  return <Space size={[4, 4]} wrap>
    <Tag color={ISSUE_STATUS[issue.status].color}>{ISSUE_STATUS[issue.status].label}</Tag>
    <Tag>{ISSUE_CATEGORY_LABELS[issue.category]}</Tag>
    <Tag color={ISSUE_SEVERITY[issue.severity].color}>{ISSUE_SEVERITY[issue.severity].label}</Tag>
    {issue.reappeared && <Tag color="volcano">再次出现</Tag>}
    {issue.not_reproduced && <Tag color="blue">本次未复现</Tag>}
    {issue.needs_recheck && <Tag color="gold">需重新核查</Tag>}
  </Space>
}

interface FilterProps {
  filter: IssueFilter
  chapters: readonly { id: number; chapter_no: number; title: string }[]
  onChange: (filter: IssueFilter) => void
}

export function IssueFilters({ filter, chapters, onChange }: FilterProps) {
  const item = { marginBottom: 0 }
  return <Form layout="vertical">
    <AutoGrid minItemWidth={170} gap={12}>
      <Form.Item label="状态" htmlFor="consistency-filter-status" style={item}>
        <Select<IssueStatus> id="consistency-filter-status" allowClear placeholder="全部状态" value={filter.status}
          onChange={(status) => onChange({ ...filter, status: status ?? undefined })}
          options={(Object.keys(ISSUE_STATUS) as IssueStatus[]).map((value) => ({ value, label: ISSUE_STATUS[value].label }))} />
      </Form.Item>
      <Form.Item label="类别" htmlFor="consistency-filter-category" style={item}>
        <Select<FactCategory> id="consistency-filter-category" allowClear placeholder="全部类别" value={filter.category}
          onChange={(category) => onChange({ ...filter, category: category ?? undefined })}
          options={(Object.keys(ISSUE_CATEGORY_LABELS) as FactCategory[]).map((value) => ({ value, label: ISSUE_CATEGORY_LABELS[value] }))} />
      </Form.Item>
      <Form.Item label="涉及章节" htmlFor="consistency-filter-chapter" style={item}>
        <Select<number> id="consistency-filter-chapter" allowClear showSearch optionFilterProp="label" placeholder="全部章节" value={filter.chapterId}
          onChange={(chapterId) => onChange({ ...filter, chapterId: chapterId ?? undefined })}
          options={chapters.map((chapter) => ({ value: chapter.id, label: `第 ${chapter.chapter_no} 章 · ${chapter.title || '未命名'}` }))} />
      </Form.Item>
      <Form.Item label="重新核查" htmlFor="consistency-filter-recheck" style={item}>
        <Select<RecheckFilter> id="consistency-filter-recheck" allowClear placeholder="不限" value={filter.recheck}
          onChange={(recheck) => onChange({ ...filter, recheck: recheck ?? undefined })}
          options={[{ value: 'yes', label: '需重新核查' }, { value: 'no', label: '无需重新核查' }]} />
      </Form.Item>
    </AutoGrid>
    {hasIssueFilter(filter) && <Button type="link" onClick={() => onChange({})} style={{ padding: 0, marginTop: 8 }}>清除筛选</Button>}
  </Form>
}

interface ListProps {
  items: ConsistencyIssue[]
  total: number
  page: number
  filter: IssueFilter
  /** 作品是否做过检查（区分「尚未检查」与「没有问题」） */
  checked: boolean
  emptyAction?: ReactNode
  onPage: (page: number) => void
  onOpen: (issue: ConsistencyIssue) => void
}

/** 问题清单：卡片式条目（宽窄屏同一布局，不横向滚动），标题为可聚焦按钮。 */
export default function IssueList({ items, total, page, filter, checked, emptyAction, onPage, onOpen }: ListProps) {
  if (total === 0) {
    const description = hasIssueFilter(filter) ? '没有符合筛选条件的潜在冲突' : checked ? '目前没有记录在案的潜在冲突' : '尚未检查'
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={description}>{!hasIssueFilter(filter) && emptyAction}</Empty>
  }
  return <>
    {items.length === 0 && page > 1 && <Button onClick={() => onPage(1)}>返回第一页</Button>}
    <List<ConsistencyIssue> aria-label="潜在冲突列表" itemLayout="vertical" dataSource={items} rowKey="id"
      renderItem={(issue) => <List.Item key={issue.id} style={{ paddingInline: 0 }}>
        <Space direction="vertical" size={6} style={{ width: '100%', minWidth: 0 }}>
          <Button type="link" onClick={() => onOpen(issue)} aria-label={`查看潜在冲突：${issue.title}`} data-consistency-issue={issue.id}
            style={{ padding: 0, height: 'auto', whiteSpace: 'normal', textAlign: 'left', fontWeight: 600, overflowWrap: 'anywhere' }}>
            {issue.title || '未命名问题'}
          </Button>
          <IssueTags issue={issue} />
          <Typography.Paragraph ellipsis={{ rows: 2 }} style={{ margin: 0, overflowWrap: 'anywhere' }}>{issue.description}</Typography.Paragraph>
          <Typography.Text type="secondary">涉及：{issueChaptersLabel(issue) || '无现存证据'}</Typography.Text>
        </Space>
      </List.Item>} />
    <Pagination current={page} total={total} pageSize={ISSUE_PAGE_SIZE} showSizeChanger={false} responsive
      onChange={onPage} showTotal={(count) => `共 ${count} 个潜在冲突`} style={{ marginTop: 16 }} />
  </>
}
