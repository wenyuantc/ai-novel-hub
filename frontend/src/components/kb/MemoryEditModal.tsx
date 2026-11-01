import { useProjectAccess } from '../../hooks/useProjectAccess'
import { App, Checkbox, Form, Input, InputNumber, Modal, Rate, Select } from 'antd'
import { useEffect, useState } from 'react'
import { errorMessage } from '../../api/client'
import { memoryApi } from '../../api/memories'
import type { Memory } from '../../api/types'

interface Props {
  projectId: number
  open: boolean
  /** null = 新增手动记忆 */
  memory: Memory | null
  characterNames: string[]
  onClose: () => void
  onSaved: (m: Memory) => void
}

interface FormValues {
  chapter_no: number
  content: string
  characters: string[]
  importance: number
  pinned: boolean
}

/** 手动记忆新增 / 编辑：章号、内容 ≤1000、相关角色（≤20，可自由填写）、重要度、钉住。 */
export default function MemoryEditModal({ projectId, open, memory, characterNames, onClose, onSaved }: Props) {
  const { canEdit, canEditNow } = useProjectAccess(projectId)
  const { message } = App.useApp()
  const [form] = Form.useForm<FormValues>()
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (!open) return
    form.setFieldsValue(
      memory
        ? {
            chapter_no: memory.chapter_no,
            content: memory.content,
            characters: memory.characters,
            importance: memory.importance,
            pinned: memory.pinned,
          }
        : { chapter_no: 1, content: '', characters: [], importance: 3, pinned: false },
    )
  }, [open, memory, form])

  const save = async () => {
    if (!canEditNow()) return
    const v = await form.validateFields()
    if (!canEditNow()) return
    const body = {
      chapter_no: v.chapter_no,
      content: v.content.trim(),
      characters: v.characters.map((c) => c.trim()).filter(Boolean),
      importance: v.importance,
      pinned: v.pinned,
    }
    setSaving(true)
    try {
      if (!canEditNow()) return
      const saved = memory ? await memoryApi.update(projectId, memory.id, body) : await memoryApi.create(projectId, body)
      if (!canEditNow()) return
      message.success(memory ? '已保存' : '已添加')
      onSaved(saved)
    } catch (err) {
      if (!canEditNow()) return
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
        okButtonProps={{ disabled: !canEdit }}
      title={memory ? '编辑手动记忆' : '新增手动记忆'}
      open={open}
      onOk={() => void save()}
      onCancel={onClose}
      confirmLoading={saving}
      okText="保存"
      cancelText="取消"
      forceRender
    >
      <Form form={form} layout="vertical" disabled={!canEdit}>
        <Form.Item
          name="chapter_no"
          label="章号"
          extra="记忆归属的章节；生成第 N 章时只召回第 N 章之前的记忆"
          rules={[{ required: true, message: '请输入章号' }]}
        >
          <InputNumber min={1} max={100000} precision={0} style={{ width: 160 }} addonBefore="第" addonAfter="章" />
        </Form.Item>
        <Form.Item
          name="content"
          label="内容"
          rules={[
            { required: true, whitespace: true, message: '请输入记忆内容' },
            { max: 1000, message: '不超过 1000 字' },
          ]}
        >
          <Input.TextArea autoSize={{ minRows: 3, maxRows: 8 }} maxLength={1000} showCount />
        </Form.Item>
        <Form.Item
          name="characters"
          label="相关角色"
          rules={[{ type: 'array', max: 20, message: '最多 20 个' }]}
        >
          <Select
            mode="tags"
            placeholder="选择或输入角色名"
            options={characterNames.map((n) => ({ value: n, label: n }))}
          />
        </Form.Item>
        <Form.Item name="importance" label="重要度">
          <Rate count={5} />
        </Form.Item>
        <Form.Item name="pinned" valuePropName="checked">
          <Checkbox>钉住（生成时优先注入）</Checkbox>
        </Form.Item>
      </Form>
    </Modal>
  )
}
