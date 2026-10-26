import { Alert, App, Button, Card, Checkbox, Col, Collapse, Empty, Input, Row, Select, Space, Tag, Typography } from 'antd'
import { useCallback, useMemo, useState } from 'react'
import {
  CAREER_KIND_LABELS,
  careerApi,
  draftBlockReason,
  DRAFT_NAME_CONFLICT,
  draftsToBatch,
  parseCareerGenerateResult,
  toEditableDrafts,
  type EditableCareerDraft,
} from '../../api/careers'
import { errorMessage } from '../../api/client'
import type { CareerGenerateResult, CareerKind } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import type { TaskResultViewerProps } from '../entities/types'
import StageListEditor from './StageListEditor'

const KIND_OPTIONS = (['main', 'sub'] as CareerKind[]).map((k) => ({ value: k, label: CAREER_KIND_LABELS[k] }))

/** career.generate 结果：逐个编辑 / 取消勾选，确认后 careers/batch 单事务保存；同名职业需改名后才能勾选。 */
export default function CareerDraftResult({ projectId, result, onSaved }: TaskResultViewerProps) {
  const parsed = parseCareerGenerateResult(result)
  if (!parsed) return <Empty description="结果格式不正确，无法预览" />
  if (parsed.drafts.length === 0) return <Empty description="没有可用的职业草稿" />
  return <Editor projectId={projectId} parsed={parsed} onSaved={onSaved} />
}

function Editor({ projectId, parsed, onSaved }: { projectId: number; parsed: CareerGenerateResult; onSaved?: () => void }) {
  const { message } = App.useApp()
  const [drafts, setDrafts] = useState<EditableCareerDraft[]>(() => toEditableDrafts(parsed.drafts))
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  // 当前已有职业名（结果可能是很久以前的任务，以最新数据判定同名）
  const loader = useCallback(() => careerApi.list(projectId), [projectId])
  const { data: careers } = useLoader(loader)
  const existingNames = useMemo(() => new Set((careers ?? []).map((c) => c.name.trim())), [careers])

  const patch = (key: string, p: Partial<EditableCareerDraft>) =>
    setDrafts((list) => list.map((d) => (d.key === key ? { ...d, ...p } : d)))
  const reasons = new Map(drafts.map((d) => [d.key, draftBlockReason(d, drafts, existingNames)]))
  const payload = draftsToBatch(drafts, existingNames)
  // 同名冲突的草稿视为未勾选（勾选框禁用），其余已勾选但不合法的草稿阻止保存
  const blocked = drafts.filter((d) => {
    const r = reasons.get(d.key) ?? null
    return d.checked && r !== null && r !== DRAFT_NAME_CONFLICT
  })

  const handleSave = async () => {
    setSaving(true)
    try {
      const out = await careerApi.batch(projectId, payload)
      message.success(`已保存 ${out.length} 个职业`)
      setSaved(true)
      onSaved?.()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const disabled = saving || saved
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {parsed.warnings.map((w) => (
        <Alert key={w} type="warning" showIcon message={w} />
      ))}
      {saved && <Alert type="success" showIcon message="已保存。" />}
      {drafts.map((d) => {
        const reason = reasons.get(d.key) ?? null
        const conflict = reason === DRAFT_NAME_CONFLICT
        return (
          <Card
            key={d.key}
            size="small"
            title={
              <Space wrap>
                <Checkbox
                  checked={d.checked && !conflict}
                  disabled={disabled || conflict}
                  onChange={(e) => patch(d.key, { checked: e.target.checked })}
                />
                <Input
                  value={d.name}
                  maxLength={100}
                  disabled={disabled}
                  style={{ width: 200 }}
                  status={conflict ? 'error' : undefined}
                  onChange={(e) => patch(d.key, { name: e.target.value })}
                />
                <Select
                  value={d.kind}
                  options={KIND_OPTIONS}
                  disabled={disabled}
                  style={{ width: 100 }}
                  onChange={(v) => patch(d.key, { kind: v })}
                />
                <Tag>{d.stages.length} 阶</Tag>
                {reason && (d.checked || conflict) && <Typography.Text type="danger">{reason}</Typography.Text>}
              </Space>
            }
          >
            <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }} ellipsis={{ rows: 1 }}>
              {d.stages.map((s) => s.name).join(' → ') || '（无阶段）'}
            </Typography.Paragraph>
            <Collapse
              size="small"
              items={[
                {
                  key: 'detail',
                  label: '编辑详情与阶段',
                  children: (
                    <Space direction="vertical" style={{ width: '100%' }}>
                      <Row gutter={12}>
                        {(
                          [
                            ['description', '描述'],
                            ['requirements', '获得条件'],
                            ['abilities', '特殊能力'],
                            ['rules', '体系规则'],
                          ] as const
                        ).map(([field, label]) => (
                          <Col key={field} span={12} style={{ marginBottom: 8 }}>
                            <Typography.Text type="secondary">{label}</Typography.Text>
                            <Input.TextArea
                              autoSize={{ minRows: 1, maxRows: 5 }}
                              value={d[field]}
                              disabled={disabled}
                              onChange={(e) => patch(d.key, { [field]: e.target.value })}
                            />
                          </Col>
                        ))}
                      </Row>
                      <StageListEditor value={d.stages} onChange={(stages) => patch(d.key, { stages })} disabled={disabled} />
                    </Space>
                  ),
                },
              ]}
            />
          </Card>
        )
      })}
      <Space>
        <Button
          type="primary"
          loading={saving}
          disabled={saved || payload.length === 0 || blocked.length > 0}
          onClick={() => void handleSave()}
        >
          保存 {payload.length} 个职业
        </Button>
        <Typography.Text type={blocked.length > 0 ? 'danger' : 'secondary'}>
          {blocked.length > 0 ? `有 ${blocked.length} 个已勾选的职业需要修改` : '未勾选的职业不会写入；不保存则不写入任何数据。'}
        </Typography.Text>
      </Space>
    </Space>
  )
}
