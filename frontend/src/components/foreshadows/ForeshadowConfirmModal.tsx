import { App, Button, Form, Input, InputNumber, Modal, Rate, Select, Typography } from 'antd'
import { useCallback, useMemo, useState } from 'react'
import { analysisApi } from '../../api/analysis'
import { errorMessage } from '../../api/client'
import { buildForeshadowOverrides, newForeshadowDraft, type NewForeshadowFormValues } from '../../api/foreshadows'
import { characterApi } from '../../api/story'
import type { AnalysisChange } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { useProjectId } from '../../hooks/useProjectId'

interface Props {
  /** 待确认的 foreshadow_new 条目；null = 关闭 */
  change: AnalysisChange | null
  onClose: () => void
  onConfirmed: (updated: AnalysisChange) => void
}

/** 确认分析识别的新伏笔：可修改标题等字段（只把改动的字段作为 overrides 提交），确认后创建为「已埋」、实际埋设章 = 本章。 */
export default function ForeshadowConfirmModal({ change, onClose, onConfirmed }: Props) {
  return (
    <Modal
      open={change !== null}
      title={change ? `确认新伏笔（第 ${change.chapter_no} 章）` : ''}
      onCancel={onClose}
      footer={null}
      destroyOnHidden
      width={560}
    >
      {change && <Inner key={change.id} change={change} onClose={onClose} onConfirmed={onConfirmed} />}
    </Modal>
  )
}

function Inner({ change, onClose, onConfirmed }: { change: AnalysisChange } & Omit<Props, 'change'>) {
  const pid = useProjectId()
  const { message } = App.useApp()
  const [form] = Form.useForm<NewForeshadowFormValues>()
  const [submitting, setSubmitting] = useState(false)
  const initial = useMemo(() => newForeshadowDraft(change.after), [change.after])
  const charLoader = useCallback(async () => (Number.isFinite(pid) ? characterApi.list(pid) : []), [pid])
  const { data: characters = [] } = useLoader(charLoader)

  const handleOk = async () => {
    let v: NewForeshadowFormValues
    try {
      v = await form.validateFields()
    } catch {
      return
    }
    const built = buildForeshadowOverrides(initial, v, { charactersTouched: form.isFieldTouched('character_ids') })
    if (!built.ok) {
      message.warning(built.error)
      return
    }
    setSubmitting(true)
    try {
      const overrides = Object.keys(built.overrides).length > 0 ? built.overrides : undefined
      onConfirmed(await analysisApi.confirm(change.id, overrides))
      message.success('已确认，伏笔已创建')
      onClose()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Form form={form} layout="vertical" initialValues={initial} disabled={submitting}>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        确认后创建为「已埋」，实际埋设章为第 {change.chapter_no} 章；同一章已有同名伏笔时无法确认。
      </Typography.Paragraph>
      <Form.Item
        name="title"
        label="标题"
        rules={[
          { required: true, whitespace: true, message: '伏笔标题不能为空' },
          { max: 200, message: '不超过 200 字' },
        ]}
      >
        <Input />
      </Form.Item>
      <Form.Item name="content" label="内容">
        <Input.TextArea autoSize={{ minRows: 2, maxRows: 5 }} />
      </Form.Item>
      <Form.Item name="plant_hint" label="埋法要点">
        <Input.TextArea autoSize={{ minRows: 1, maxRows: 4 }} placeholder="可选：本章是怎么埋下的，供后续章节参考" />
      </Form.Item>
      <Form.Item name="reveal_hint" label="回收要点">
        <Input.TextArea autoSize={{ minRows: 1, maxRows: 4 }} placeholder="可选：生成回收章时提示模型怎么收" />
      </Form.Item>
      <Form.Item name="planned_resolve_chapter" label="计划回收章" tooltip="不填即为长线伏笔">
        <InputNumber min={1} max={100000} precision={0} addonBefore="第" addonAfter="章" placeholder="长线" />
      </Form.Item>
      <Form.Item name="importance" label="重要度">
        <Rate count={5} allowClear={false} />
      </Form.Item>
      <Form.Item name="character_ids" label="关联角色">
        <Select
          mode="multiple"
          allowClear
          optionFilterProp="label"
          placeholder="可选"
          options={characters.map((c) => ({ value: c.id, label: c.name }))}
        />
      </Form.Item>
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
        <Button onClick={onClose} disabled={submitting}>
          取消
        </Button>
        <Button type="primary" loading={submitting} onClick={() => void handleOk()}>
          确认创建
        </Button>
      </div>
    </Form>
  )
}
