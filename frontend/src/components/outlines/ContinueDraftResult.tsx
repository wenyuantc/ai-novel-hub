import { DeleteOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Checkbox, Empty, Input, InputNumber, Space, Tag, Typography } from 'antd'
import { useState } from 'react'
import { errorMessage } from '../../api/client'
import {
  continueAppendItems,
  continueDraftError,
  continuePreviewNos,
  outlineProApi,
  parseContinueResult,
  toEditableContinueDrafts,
  type EditableContinueDraft,
} from '../../api/outlines'
import type { OutlineContinueResult } from '../../api/types'
import type { TaskResultViewerProps } from '../entities/types'

/** outline.continue 结果：逐条编辑 / 取消勾选 / 删除，确认后 POST /outlines/append 单事务追加（章节号由后端顺延）。 */
export default function ContinueDraftResult({ projectId, result, onSaved }: TaskResultViewerProps) {
  const parsed = parseContinueResult(result)
  if (!parsed) return <Empty description="结果格式不正确，无法预览" />
  if (parsed.drafts.length === 0) return <Empty description="没有可用的章纲草稿" />
  return <Editor projectId={projectId} parsed={parsed} onSaved={onSaved} />
}

function Editor({ projectId, parsed, onSaved }: { projectId: number; parsed: OutlineContinueResult; onSaved?: () => void }) {
  const { message } = App.useApp()
  const [drafts, setDrafts] = useState<EditableContinueDraft[]>(() => toEditableContinueDrafts(parsed.drafts))
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const startNo = parsed.drafts[0]?.chapter_no ?? 1
  const nos = continuePreviewNos(drafts, startNo)
  const items = continueAppendItems(drafts)
  const invalid = drafts.filter((d) => d.checked && continueDraftError(d) !== null)
  const disabled = saving || saved

  const patch = (key: string, p: Partial<EditableContinueDraft>) =>
    setDrafts((list) => list.map((d) => (d.key === key ? { ...d, ...p } : d)))

  const handleSave = async () => {
    setSaving(true)
    try {
      const out = await outlineProApi.append(projectId, items)
      const first = out[0]?.chapter_no
      message.success(`已追加 ${out.length} 条章纲${first !== undefined ? `（第 ${first}–${first + out.length - 1} 章）` : ''}`)
      setSaved(true)
      onSaved?.()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {parsed.warnings.map((w) => (
        <Alert key={w} type="warning" showIcon message={w} />
      ))}
      {saved && <Alert type="success" showIcon message="已追加到大纲末尾。" />}
      {drafts.length === 0 && <Empty description="草稿已全部删除" />}
      {drafts.map((d) => {
        const err = d.checked ? continueDraftError(d) : null
        const no = nos.get(d.key)
        return (
          <Card
            key={d.key}
            size="small"
            title={
              <Space wrap>
                <Checkbox checked={d.checked} disabled={disabled} onChange={(e) => patch(d.key, { checked: e.target.checked })} />
                {no !== undefined ? <Tag color="blue">预计第 {no} 章</Tag> : <Tag>不采纳</Tag>}
                <Typography.Text type="secondary">第</Typography.Text>
                <InputNumber
                  size="small"
                  min={1}
                  value={d.volume_no}
                  disabled={disabled}
                  style={{ width: 64 }}
                  onChange={(v) => patch(d.key, { volume_no: v ?? 1 })}
                />
                <Typography.Text type="secondary">卷</Typography.Text>
                <Input
                  size="small"
                  value={d.volume_title}
                  placeholder="卷名"
                  maxLength={200}
                  disabled={disabled}
                  style={{ width: 160 }}
                  onChange={(e) => patch(d.key, { volume_title: e.target.value })}
                />
                {err && <Typography.Text type="danger">{err}</Typography.Text>}
              </Space>
            }
            extra={
              <Button
                type="text"
                size="small"
                danger
                icon={<DeleteOutlined />}
                disabled={disabled}
                onClick={() => setDrafts((list) => list.filter((x) => x.key !== d.key))}
              >
                删除
              </Button>
            }
          >
            <Space direction="vertical" style={{ width: '100%' }}>
              <Input
                value={d.title}
                placeholder="章节标题"
                maxLength={200}
                disabled={disabled}
                status={d.checked && !d.title.trim() ? 'error' : undefined}
                onChange={(e) => patch(d.key, { title: e.target.value })}
              />
              <Input.TextArea
                value={d.summary}
                placeholder="章节梗概"
                autoSize={{ minRows: 2, maxRows: 8 }}
                disabled={disabled}
                onChange={(e) => patch(d.key, { summary: e.target.value })}
              />
            </Space>
          </Card>
        )
      })}
      <Space wrap>
        <Button
          type="primary"
          loading={saving}
          disabled={saved || items.length === 0 || invalid.length > 0}
          onClick={() => void handleSave()}
        >
          追加 {items.length} 条章纲
        </Button>
        <Typography.Text type={invalid.length > 0 ? 'danger' : 'secondary'}>
          {invalid.length > 0
            ? `有 ${invalid.length} 条已勾选的章纲需要修改`
            : '章节号从当前末尾顺延；未勾选或已删除的不会写入，不追加则不写入任何数据。'}
        </Typography.Text>
      </Space>
    </Space>
  )
}
