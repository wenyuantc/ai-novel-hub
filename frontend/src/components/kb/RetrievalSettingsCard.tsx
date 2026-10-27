import { App, Button, Card, Form, InputNumber, Space, Switch, Typography } from 'antd'
import { useEffect, useState } from 'react'
import { errorMessage } from '../../api/client'
import { kbApi } from '../../api/kb'
import type { KbSettings, KbSettingsUpdate } from '../../api/types'

interface Props {
  projectId: number
  settings: KbSettings | undefined
  /** kb = 知识库注入参数；memory = 剧情记忆召回参数（同一份项目设置，只提交本组字段） */
  scope: 'kb' | 'memory'
  onSaved: (s: KbSettings) => void
}

type KbValues = Pick<KbSettings, 'auto_inject' | 'top_k' | 'min_score' | 'max_chars'>
type MemoryValues = Pick<KbSettings, 'memory_top_k' | 'memory_max_chars' | 'memory_min_score'>

/** 检索设置表单：知识库（自动注入、条数、最低分、字数上限）或剧情记忆（条数、字数上限、最低分）。 */
export default function RetrievalSettingsCard({ projectId, settings, scope, onSaved }: Props) {
  const { message } = App.useApp()
  const [form] = Form.useForm<KbValues & MemoryValues>()
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (settings) form.setFieldsValue(settings)
  }, [settings, form])

  const save = async () => {
    const v = await form.validateFields()
    const body: KbSettingsUpdate =
      scope === 'kb'
        ? { auto_inject: v.auto_inject, top_k: v.top_k, min_score: v.min_score, max_chars: v.max_chars }
        : { memory_top_k: v.memory_top_k, memory_max_chars: v.memory_max_chars, memory_min_score: v.memory_min_score }
    setSaving(true)
    try {
      const out = await kbApi.updateSettings(projectId, body)
      onSaved(out)
      message.success('设置已保存')
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card
      size="small"
      title={scope === 'kb' ? '注入设置' : '召回设置'}
      extra={
        <Button size="small" type="primary" loading={saving} disabled={!settings} onClick={() => void save()}>
          保存
        </Button>
      }
    >
      <Form form={form} layout="vertical" disabled={!settings}>
        {scope === 'kb' ? (
          <>
            <Form.Item
              name="auto_inject"
              label="自动注入"
              valuePropName="checked"
              extra="章节生成、按建议重写、批量、大纲续写与展开、向导、实体生成、灵感模式会按相关度注入参考资料"
            >
              <Switch />
            </Form.Item>
            <Space size="middle" wrap align="start">
              <Form.Item name="top_k" label="召回条数" rules={[{ required: true, message: '必填' }]}>
                <InputNumber min={1} max={20} precision={0} style={{ width: 120 }} />
              </Form.Item>
              <Form.Item name="min_score" label="最低分数" rules={[{ required: true, message: '必填' }]}>
                <InputNumber min={0} max={1} step={0.05} precision={2} style={{ width: 120 }} />
              </Form.Item>
              <Form.Item name="max_chars" label="注入字数上限" rules={[{ required: true, message: '必填' }]}>
                <InputNumber min={500} max={8000} step={500} precision={0} style={{ width: 140 }} />
              </Form.Item>
            </Space>
          </>
        ) : (
          <Space size="middle" wrap align="start">
            <Form.Item name="memory_top_k" label="召回条数" rules={[{ required: true, message: '必填' }]}>
              <InputNumber min={1} max={20} precision={0} style={{ width: 120 }} />
            </Form.Item>
            <Form.Item name="memory_min_score" label="最低分数" rules={[{ required: true, message: '必填' }]}>
              <InputNumber min={0} max={1} step={0.05} precision={2} style={{ width: 120 }} />
            </Form.Item>
            <Form.Item name="memory_max_chars" label="注入字数上限" rules={[{ required: true, message: '必填' }]}>
              <InputNumber min={300} max={6000} step={100} precision={0} style={{ width: 140 }} />
            </Form.Item>
          </Space>
        )}
      </Form>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {scope === 'kb'
          ? '分数为 0–1：有向量模型时 = 0.7 × 向量分 + 0.3 × 关键词分，否则只用关键词分；低于最低分数的不注入。注入时附带「仅供参考，与项目设定冲突时以设定为准」。'
          : '生成第 N 章时只从第 N 章之前、且前情里没有给出全文的章节召回；钉住的优先，其余按相关度补足，最后按章号排列。屏蔽的不会被召回。'}
      </Typography.Text>
    </Card>
  )
}
