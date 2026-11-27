import { Alert, Button, Checkbox, Form, Input, Modal, Select, Space } from 'antd'
import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, errorMessage } from '../../../api/client'
import { characterApi, worldApi } from '../../../api/story'
import { useLoader } from '../../../hooks/useLoader'
import { dramaAssetApi } from '../assetApi'
import {
  assetLabel,
  assetsOfKind,
  type AssetFields,
  type AssetKind,
  type DramaAsset,
  type EpisodeVersion,
} from '../assetTypes'

export interface AssetSelection {
  kind: AssetKind
  parent: number
  row?: DramaAsset
}
type TextField = { name: string; label: string; long?: boolean; required?: boolean }
const textFields: Record<AssetKind, TextField[]> = {
  characters: [
    { name: 'name', label: '角色名', required: true },
    { name: 'role', label: '身份' },
    { name: 'appearance', label: '样貌', long: true },
    { name: 'description', label: '角色描述', long: true },
  ],
  looks: [
    { name: 'name', label: '造型名', required: true },
    { name: 'styling', label: '妆造', long: true },
    { name: 'image_prompt', label: '图片提示词', long: true },
  ],
  scenes: [
    { name: 'location', label: '地点', required: true },
    { name: 'time_of_day', label: '时间段' },
    { name: 'description', label: '场景描述', long: true },
    { name: 'lighting', label: '光线', long: true },
    { name: 'image_prompt', label: '图片提示词', long: true },
  ],
  props: [
    { name: 'name', label: '道具名', required: true },
    { name: 'category', label: '类别' },
    { name: 'description', label: '道具描述', long: true },
    { name: 'image_prompt', label: '图片提示词', long: true },
  ],
}

function editableValues(kind: AssetKind, row?: DramaAsset): AssetFields {
  const source = new Map<string, unknown>(Object.entries(row ?? {}))
  const fields: AssetFields = {}
  for (const { name } of textFields[kind]) fields[name] = String(source.get(name) ?? '')
  for (const key of ['novel_character_id', 'novel_world_setting_id']) {
    if (source.has(key)) fields[key] = Number(source.get(key)) || null
  }
  if (kind === 'looks') fields.is_default = Boolean(source.get('is_default'))
  return fields
}

interface Props {
  pid: number
  sid: number
  episode?: EpisodeVersion
  selection: AssetSelection
  canEditNow: () => boolean
  onSaved: () => void
  onClose: () => void
}
export default function AssetEditor({ pid, sid, episode, selection, canEditNow, onSaved, onClose }: Props) {
  const { kind, parent, row } = selection
  const [form] = Form.useForm<AssetFields>()
  const [baseline, setBaseline] = useState(() => editableValues(kind, row))
  const original = useRef(baseline)
  const revision = useRef(row?.revision)
  const queue = useRef(Promise.resolve())
  const active = useRef(true)
  const hasConflict = useRef(false)
  useEffect(() => {
    active.current = true
    return () => {
      active.current = false
    }
  }, [])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [conflict, setConflict] = useState<DramaAsset | null>(null)
  const options = useLoader(
    useCallback(async () => {
      if (kind === 'characters')
        return (await characterApi.list(pid)).map(({ id, name }) => ({ value: id, label: name }))
      if (kind === 'scenes')
        return (await worldApi.list(pid))
          .filter((item) => item.category === 'place')
          .map(({ id, title }) => ({ value: id, label: title }))
      return []
    }, [pid, kind]),
  )
  const linkKey = kind === 'characters' ? 'novel_character_id' : kind === 'scenes' ? 'novel_world_setting_id' : null
  function save(close: boolean, overwrite = false) {
    queue.current = queue.current.then(() => persist(close, overwrite))
  }
  async function persist(close: boolean, overwrite: boolean) {
    if (!active.current || !canEditNow() || (hasConflict.current && !overwrite)) return
    try {
      let values: AssetFields
      try {
        values = await form.validateFields()
      } catch {
        return
      }
      if (!active.current || !canEditNow()) return
      if (linkKey) values = { ...values, [linkKey]: values[linkKey] ?? null }
      const changed = Object.fromEntries(
        Object.entries(values).filter(([key, value]) => value !== original.current[key]),
      )
      if (row && !Object.keys(changed).length) {
        if (close) onClose()
        return
      }
      setBusy(true)
      setError('')
      if (!row) {
        await dramaAssetApi.create(kind, parent, values, episode)
      } else {
        const result = await dramaAssetApi.patch(kind, row.id, {
          ...changed,
          revision: overwrite && conflict ? conflict.revision : revision.current!,
        })
        const saved = editableValues(kind, result)
        revision.current = result.revision
        original.current = saved
        setBaseline(saved)
        // Only refresh inputs that were untouched while this write was in flight.
        const current = form.getFieldsValue()
        form.setFieldsValue(Object.fromEntries(Object.entries(saved).filter(([key]) => current[key] === values[key])))
        hasConflict.current = false
        setConflict(null)
      }
      if (!active.current || !canEditNow()) return
      onSaved()
      if (close) {
        active.current = false
        onClose()
      }
    } catch (err) {
      if (!active.current || !canEditNow()) return
      setError(errorMessage(err))
      if (!(err instanceof ApiError && err.status === 409 && err.body?.code === 'stale_revision')) return
      if (!row && episode) onSaved() // Refresh the episode revision without closing the creation draft.
      if (row) {
        try {
          const latest = assetsOfKind(await dramaAssetApi.series(sid), kind).find((item) => item.id === row.id)
          if (latest) {
            hasConflict.current = true
            setConflict(latest)
          }
        } catch (refreshError) {
          setError(errorMessage(refreshError))
        }
      }
    } finally {
      setBusy(false)
    }
  }
  function reload() {
    if (!conflict) return
    original.current = editableValues(kind, conflict)
    setBaseline(original.current)
    hasConflict.current = false
    revision.current = conflict.revision
    form.setFieldsValue(original.current)
    setConflict(null)
    setError('')
  }
  const title = `${row ? '编辑' : '新建'}${assetLabel[kind]}`
  return (
    <Modal
      open
      title={title}
      onCancel={() => {
        if (!busy) onClose()
      }}
      footer={null}
      maskClosable={false}
    >
      <Form layout="vertical" form={form} initialValues={baseline} aria-label={title} onFinish={() => void save(true)}>
        {textFields[kind].map((field) => (
          <Form.Item
            key={field.name}
            name={field.name}
            label={field.label}
            rules={field.required ? [{ required: true, whitespace: true, message: `请填写${field.label}` }] : []}
          >
            {field.long ? (
              <Input.TextArea
                maxLength={60000}
                autoSize={{ minRows: 2, maxRows: 9 }}
                onBlur={() => {
                  if (row) void save(false)
                }}
              />
            ) : (
              <Input
                maxLength={100}
                onBlur={() => {
                  if (row) void save(false)
                }}
              />
            )}
          </Form.Item>
        ))}
        {kind === 'looks' && (
          <Form.Item name="is_default" valuePropName="checked">
            <Checkbox disabled={Boolean(baseline.is_default)}>设为默认造型</Checkbox>
          </Form.Item>
        )}
        {linkKey && (
          <Form.Item name={linkKey} label={kind === 'characters' ? '关联小说角色' : '关联小说地点'}>
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              options={options.data}
              loading={options.loading}
              placeholder="未关联"
            />
          </Form.Item>
        )}
        {options.error && (
          <Alert type="warning" message={options.error} action={<Button onClick={options.reload}>重试</Button>} />
        )}
        {error && (
          <Alert
            showIcon
            type="error"
            message={conflict ? '素材已被修改，你的输入已保留。' : error}
            action={
              conflict && (
                <Space wrap>
                  <Button onClick={reload}>重新载入</Button>
                  <Button loading={busy} onClick={() => void save(true, true)}>
                    覆盖保存
                  </Button>
                </Space>
              )
            }
          />
        )}
        <Space>
          <Button onClick={onClose} disabled={busy}>
            关闭
          </Button>
          <Button htmlType="submit" type="primary" loading={busy} disabled={!!conflict}>
            保存
          </Button>
        </Space>
      </Form>
    </Modal>
  )
}
