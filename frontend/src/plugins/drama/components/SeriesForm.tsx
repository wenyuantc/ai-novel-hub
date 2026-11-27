import { Alert, Button, Form, Input, Modal, Select, Space } from 'antd'
import { useState } from 'react'
import { ApiError, errorMessage } from '../../../api/client'
import { dramaApi, type Series, type SeriesInput } from '../api'

export default function SeriesForm({
  pid,
  initial,
  styles,
  close,
  saved,
  current,
}: {
  pid: number
  initial?: Series
  styles: { key: string; label: string }[]
  close: () => void
  saved: (row: Series) => void
  current: () => boolean
}) {
  const [form] = Form.useForm<SeriesInput>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [conflict, setConflict] = useState(false)
  const [revision, setRevision] = useState(initial?.revision)
  const reload = async (keep: boolean) => {
    if (!initial || !current()) return
    try {
      const fresh = await dramaApi.series(initial.id)
      if (!current()) return
      if (!keep) form.setFieldsValue(fresh)
      setRevision(fresh.revision)
      setConflict(false)
      setError('')
      if (keep) await save(form.getFieldsValue(), fresh.revision)
    } catch (e) {
      if (current()) setError(errorMessage(e))
    }
  }
  const save = async (values: SeriesInput, expected = revision) => {
    if (!current() || busy) return
    setBusy(true)
    try {
      const body = { ...values, title: values.title.trim() }
      const row =
        initial && expected !== undefined
          ? await dramaApi.update(initial.id, { ...body, revision: expected })
          : await dramaApi.create(pid, body)
      if (current()) saved(row)
    } catch (e) {
      if (current()) {
        setError(errorMessage(e))
        setConflict(e instanceof ApiError && e.status === 409)
      }
    } finally {
      setBusy(false)
    }
  }
  return (
    <Modal
      open
      title={initial ? '编辑短剧' : '新建短剧'}
      onCancel={close}
      okText="保存"
      confirmLoading={busy}
      onOk={() => form.submit()}
    >
      {error && (
        <Alert
          type="error"
          message={error}
          action={
            conflict && (
              <Space>
                <Button onClick={() => void reload(false)}>重新载入</Button>
                <Button onClick={() => void reload(true)}>覆盖保存</Button>
              </Space>
            )
          }
        />
      )}
      <Form
        layout="vertical"
        form={form}
        onFinish={(values) => void save(values)}
        initialValues={initial ?? { title: '', description: '', style: styles[0]?.key, aspect_ratio: '16:9' }}
      >
        <Form.Item label="剧名" name="title" rules={[{ required: true, whitespace: true, message: '请输入剧名' }]}>
          <Input maxLength={200} autoFocus />
        </Form.Item>
        <Form.Item label="风格" name="style" rules={[{ required: true }]}>
          <Select options={styles.map((s) => ({ value: s.key, label: s.label }))} />
        </Form.Item>
        <Form.Item label="画幅" name="aspect_ratio">
          <Select options={['16:9', '9:16', '1:1'].map((value) => ({ value }))} />
        </Form.Item>
        <Form.Item label="简介" name="description">
          <Input.TextArea rows={3} />
        </Form.Item>
      </Form>
    </Modal>
  )
}
