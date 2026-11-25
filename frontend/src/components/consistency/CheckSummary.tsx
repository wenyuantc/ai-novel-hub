import { Alert, Card, Descriptions, List, Space, Statistic, Tag, Typography } from 'antd'
import type { ConsistencyRun, ConsistencySummary } from '../../api/consistency'
import {
  ISSUE_STATUS, RUN_STATUS, runNotices, runScopeLabel, runStatRows, skippedStateLabel, type ScopeChapter,
} from '../../utils/consistency'
import { formatTime } from '../../utils/time'
import AutoGrid from '../AutoGrid'

interface Props { summary: ConsistencySummary; chapters: readonly ScopeChapter[] }

/** 问题计数、事实台账覆盖与最近一次检查的时间、范围和结果概要（含跳过与失败章节）。 */
export default function CheckSummary({ summary, chapters }: Props) {
  const run = summary.last_run
  const coverage = summary.ledger_coverage
  return <Space direction="vertical" size="middle" style={{ width: '100%', minWidth: 0 }}>
    <AutoGrid minItemWidth={300} gap={16}>
      <Card size="small" title="潜在冲突">
        <Space size="large" wrap>
          {(['open', 'resolved', 'ignored'] as const).map((status) => (
            <Statistic key={status} title={ISSUE_STATUS[status].label} value={summary.counts_by_status[status] ?? 0} />
          ))}
        </Space>
      </Card>
      <Card size="small" title="事实台账覆盖" extra={<Typography.Text type="secondary">按已写章节统计</Typography.Text>}>
        <Space size="large" wrap>
          <Statistic title="最新" value={coverage.fresh} suffix="章" />
          <Statistic title="已过期" value={coverage.stale} suffix="章" />
          <Statistic title="未抽取" value={coverage.missing} suffix="章" />
        </Space>
        <Typography.Paragraph type="secondary" style={{ margin: '8px 0 0' }}>
          正文修改后该章台账即过期；过期与未抽取的章节会在下次检查时重新调用模型。
        </Typography.Paragraph>
      </Card>
    </AutoGrid>
    <RunSummary run={run} chapters={chapters} title="最近一次检查" />
  </Space>
}

export function RunSummary({ run, chapters, title }: { run: ConsistencyRun | null; chapters: readonly ScopeChapter[]; title: string }) {
  const notices = run ? runNotices(run) : []
  return <Card size="small" title={title}>
    {!run ? <Typography.Text type="secondary">尚未检查</Typography.Text> : <Space direction="vertical" size="middle" style={{ width: '100%', minWidth: 0 }}>
      <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 3 }} items={[
        { key: 'time', label: '时间', children: formatTime(run.finished_at ?? run.started_at ?? run.created_at) },
        { key: 'scope', label: '范围', children: runScopeLabel(run.scope, chapters) },
        { key: 'status', label: '结果', children: <Tag color={RUN_STATUS[run.status].color}>{RUN_STATUS[run.status].label}</Tag> },
      ]} />
      <Space size={[16, 8]} wrap aria-label={`${title}的结果概要`}>
        {runStatRows(run.stats).map((row) => <Typography.Text key={row.label}>{row.label} <Typography.Text strong>{row.value}</Typography.Text></Typography.Text>)}
      </Space>
      {run.stats.skipped_chapters.length > 0 && <List
        size="small" bordered header="本次未取得结果的章节" dataSource={run.stats.skipped_chapters}
        renderItem={(item) => <List.Item key={`${item.chapter_id}:${item.state}`}>
          <Space direction="vertical" size={0} style={{ minWidth: 0 }}>
            <Typography.Text>{item.chapter_no === null ? '章节已删除' : `第 ${item.chapter_no} 章${item.chapter_title ? ` · ${item.chapter_title}` : ''}`}</Typography.Text>
            <Typography.Text type="secondary" style={{ overflowWrap: 'anywhere' }}>{skippedStateLabel(item.state)}{item.error ? `：${item.error}` : ''}</Typography.Text>
          </Space>
        </List.Item>}
      />}
      {notices.length > 0 && <Alert type="warning" showIcon message="本次检查的提示"
        description={<ul style={{ margin: 0, paddingInlineStart: 18 }}>{notices.map((text, index) => <li key={`${index}:${text}`} style={{ overflowWrap: 'anywhere' }}>{text}</li>)}</ul>} />}
    </Space>}
  </Card>
}
