import { Alert, Descriptions, Space, Typography } from 'antd'
import { Link } from 'react-router-dom'
import type { TaskResultViewerProps } from '../entities/types'
import { bookImportSummary, pauseText, projectFieldLabels, skippedText, statRows, summaryLine } from '../../utils/bookImport'

/**
 * book.import 的任务结果查看组件（已在 taskResultViewers 注册）：写入统计、写入的项目字段、跳过的批次与章节、警告。只读。
 * result 为 done / paused 载荷（结束后另有 ended_early）；取不到时退回 task.checkpoint（同一摘要结构）。
 */
export default function BookImportResultView({ projectId, result, task }: TaskResultViewerProps) {
  const summary = bookImportSummary(result) ?? bookImportSummary(task?.checkpoint)
  if (!summary) return <Typography.Text type="secondary">暂无结果</Typography.Text>
  const paused = pauseText(summary)
  const fields = projectFieldLabels(summary.stats.project_fields)
  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Alert
        type={paused ? 'warning' : summary.skipped.length || summary.ended_early ? 'info' : 'success'}
        showIcon
        message={summaryLine(summary)}
        description={paused ?? undefined}
      />
      <Descriptions size="small" bordered column={{ xs: 1, sm: 2, md: 4 }}>
        {statRows(summary).map((r) => (
          <Descriptions.Item key={r.key} label={r.label}>
            {r.value}
          </Descriptions.Item>
        ))}
      </Descriptions>
      <Typography.Text type="secondary">
        写入的项目信息：{fields.length ? fields.join('、') : '无（项目字段已有内容或未反推出）'}
      </Typography.Text>
      {summary.skipped.length > 0 && (
        <div>
          <Typography.Text strong>跳过的步骤</Typography.Text>
          <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
            {summary.skipped.map((s, i) => (
              <li key={i}>{skippedText(s)}</li>
            ))}
          </ul>
        </div>
      )}
      {summary.warnings.length > 0 && (
        <Alert
          type="warning"
          showIcon
          message={`警告（${summary.warnings.length}）`}
          description={
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {summary.warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          }
        />
      )}
      <Space wrap>
        <Link to={`/projects/${projectId}/overview`}>项目概览</Link>
        <Link to={`/projects/${projectId}/characters`}>角色</Link>
        <Link to={`/projects/${projectId}/outlines`}>大纲</Link>
        <Link to={`/projects/${projectId}/chapters`}>章节（可「一键分析」补做深度分析）</Link>
      </Space>
    </Space>
  )
}
