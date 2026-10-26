import { Alert, Modal, Space, Table, Tag, Typography } from 'antd'
import type { PromptImportItemResult, PromptImportResult } from '../../api/types'
import { formatIssue, PROMPT_IMPORT_STATUS, summarizePromptImport } from '../../utils/prompts'

interface Props {
  result: PromptImportResult | null
  /** key → 模板名称 */
  names: Record<string, string>
  onClose: () => void
}

/** 模板导入结果：逐项列出新建 / 更新 / 无变化 / 与默认相同 / 跳过 / 校验失败（带出错位置）。 */
export default function PromptImportResultModal({ result, names, onClose }: Props) {
  const summary = result ? summarizePromptImport(result) : null
  return (
    <Modal open={result !== null} title="导入结果" onCancel={onClose} onOk={onClose} cancelButtonProps={{ style: { display: 'none' } }} width={760}>
      {result && summary && (
        <Space direction="vertical" style={{ width: '100%' }}>
          <Alert type={summary.counts.invalid ? 'warning' : 'success'} showIcon message={summary.text} />
          <Table<PromptImportItemResult>
            size="small"
            rowKey={(r, i) => `${r.key}-${i}`}
            pagination={false}
            dataSource={result.items}
            columns={[
              {
                title: '模板',
                width: 220,
                render: (_, r) => (
                  <Space direction="vertical" size={0}>
                    <span>{names[r.key] ?? (r.key || '（无 key）')}</span>
                    {names[r.key] && (
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        {r.key}
                      </Typography.Text>
                    )}
                  </Space>
                ),
              },
              {
                title: '结果',
                width: 110,
                render: (_, r) => <Tag color={PROMPT_IMPORT_STATUS[r.status].color}>{PROMPT_IMPORT_STATUS[r.status].label}</Tag>,
              },
              {
                title: '说明',
                render: (_, r) => (
                  <Space direction="vertical" size={0}>
                    {r.message && <span>{r.message}</span>}
                    {r.errors.map((e, i) => (
                      <Typography.Text key={i} type="danger">
                        {formatIssue(e)}
                      </Typography.Text>
                    ))}
                  </Space>
                ),
              },
            ]}
          />
        </Space>
      )}
    </Modal>
  )
}
