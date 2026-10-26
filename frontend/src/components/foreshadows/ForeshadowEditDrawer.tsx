import { App, Button, Col, Drawer, Empty, Form, Input, InputNumber, List, Rate, Row, Select, Space, Switch, Tag, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { errorMessage } from '../../api/client'
import {
  EMPTY_FORESHADOW_FORM,
  FORESHADOW_FIELD_LABELS,
  FORESHADOW_SOURCE_LABELS,
  FORESHADOW_STATUS,
  FORESHADOW_STATUSES,
  foreshadowApi,
  foreshadowFormError,
  foreshadowToForm,
  formatForeshadowValue,
  formToForeshadowInput,
  type ForeshadowFormValues,
} from '../../api/foreshadows'
import type { Character, Foreshadow, ForeshadowHistory } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { formatTime } from '../../utils/time'

interface Props {
  projectId: number
  open: boolean
  /** null = 新建 */
  foreshadow: Foreshadow | null
  characters: Character[]
  onClose: () => void
  onSaved: (f: Foreshadow) => void
}

const STATUS_OPTIONS = FORESHADOW_STATUSES.map((s) => ({ value: s, label: FORESHADOW_STATUS[s].label }))

/** 伏笔编辑抽屉：全部字段 + 已有伏笔的变更历史；仅打开时挂载内部状态。 */
export default function ForeshadowEditDrawer(props: Props) {
  return (
    <Drawer
      title={props.foreshadow ? `编辑伏笔：#${props.foreshadow.id} ${props.foreshadow.title}` : '新增伏笔'}
      open={props.open}
      onClose={props.onClose}
      width={720}
      destroyOnHidden
    >
      {props.open && <Inner key={props.foreshadow?.id ?? 'new'} {...props} />}
    </Drawer>
  )
}

export function ForeshadowHistoryList({ items }: { items: ForeshadowHistory[] }) {
  if (items.length === 0) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无历史" />
  return (
    <List
      size="small"
      dataSource={items}
      renderItem={(h) => (
        <List.Item>
          <Space direction="vertical" size={2} style={{ width: '100%' }}>
            <Space wrap size={4}>
              <Typography.Text type="secondary">{formatTime(h.created_at)}</Typography.Text>
              <Tag>{FORESHADOW_SOURCE_LABELS[h.source] ?? h.source}</Tag>
              {h.chapter_no !== null && <Tag color="blue">第 {h.chapter_no} 章</Tag>}
              {h.note && <Typography.Text>{h.note}</Typography.Text>}
            </Space>
            {Object.entries(h.changes).map(([field, pair]) => {
              const [oldV, newV] = Array.isArray(pair) && pair.length === 2 ? pair : [undefined, pair]
              return (
                <Typography.Text key={field} style={{ fontSize: 13, wordBreak: 'break-all' }}>
                  {FORESHADOW_FIELD_LABELS[field] ?? field}：{formatForeshadowValue(field, oldV)} → {formatForeshadowValue(field, newV)}
                </Typography.Text>
              )
            })}
          </Space>
        </List.Item>
      )}
    />
  )
}

function Inner({ projectId, foreshadow, characters, onSaved }: Props) {
  const { message } = App.useApp()
  const [form] = Form.useForm<ForeshadowFormValues>()
  const [saving, setSaving] = useState(false)
  const [current, setCurrent] = useState<Foreshadow | null>(foreshadow)
  const currentId = current?.id ?? null
  const stamp = current?.updated_at ?? ''
  // 保存后（updated_at 变化）重新加载历史
  const historyLoader = useCallback(
    async () => (currentId === null || !stamp ? [] : foreshadowApi.history(projectId, currentId)),
    [projectId, currentId, stamp],
  )
  const { data: history = [], loading: historyLoading } = useLoader(historyLoader)
  const status = Form.useWatch('status', form)

  const handleSave = async () => {
    let v: ForeshadowFormValues
    try {
      v = await form.validateFields()
    } catch {
      return
    }
    const error = foreshadowFormError(v)
    if (error) {
      message.warning(error)
      return
    }
    const body = formToForeshadowInput(v)
    setSaving(true)
    try {
      const saved = current ? await foreshadowApi.update(projectId, current.id, body) : await foreshadowApi.create(projectId, body)
      message.success('已保存')
      setCurrent(saved)
      onSaved(saved)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const chapterInput = (placeholder: string) => (
    <InputNumber min={1} max={100000} precision={0} style={{ width: '100%' }} placeholder={placeholder} />
  )

  return (
    <>
      <Form
        form={form}
        layout="vertical"
        disabled={saving}
        initialValues={foreshadow ? foreshadowToForm(foreshadow) : EMPTY_FORESHADOW_FORM}
      >
        <Row gutter={16}>
          <Col span={16}>
            <Form.Item
              name="title"
              label="标题"
              rules={[
                { required: true, whitespace: true, message: '伏笔标题不能为空' },
                { max: 200, message: '不超过 200 字' },
              ]}
            >
              <Input placeholder="例如：玉佩来历" />
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="status" label="状态" tooltip="可随时手动改回；已埋须填实际埋设章，已回收 / 部分回收须填实际回收章">
              <Select options={STATUS_OPTIONS} />
            </Form.Item>
          </Col>
        </Row>
        <Form.Item name="content" label="内容">
          <Input.TextArea autoSize={{ minRows: 2, maxRows: 6 }} placeholder="伏笔本身是什么、真相是什么" />
        </Form.Item>
        <Row gutter={16}>
          <Col span={12}>
            <Form.Item name="plant_hint" label="埋法要点">
              <Input.TextArea autoSize={{ minRows: 2, maxRows: 5 }} placeholder="生成埋设章时提示模型怎么埋" />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="reveal_hint" label="回收要点">
              <Input.TextArea autoSize={{ minRows: 2, maxRows: 5 }} placeholder="生成回收章时提示模型怎么收" />
            </Form.Item>
          </Col>
        </Row>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          章节字段填章节号，可以是尚未创建的章节；不填计划回收章即为长线伏笔。
        </Typography.Text>
        <Row gutter={16} style={{ marginTop: 8 }}>
          <Col span={6}>
            <Form.Item name="planned_plant_chapter" label="计划埋设章">
              {chapterInput('不填')}
            </Form.Item>
          </Col>
          <Col span={6}>
            <Form.Item
              name="actual_plant_chapter"
              label="实际埋设章"
              rules={status === 'planted' ? [{ required: true, message: '请填写实际埋设章节' }] : []}
            >
              {chapterInput('未埋')}
            </Form.Item>
          </Col>
          <Col span={6}>
            <Form.Item name="planned_resolve_chapter" label="计划回收章">
              {chapterInput('长线')}
            </Form.Item>
          </Col>
          <Col span={6}>
            <Form.Item
              name="actual_resolve_chapter"
              label="实际回收章"
              rules={status === 'resolved' || status === 'partial' ? [{ required: true, message: '请填写实际回收章节' }] : []}
            >
              {chapterInput('未收')}
            </Form.Item>
          </Col>
        </Row>
        <Row gutter={16}>
          <Col span={10}>
            <Form.Item name="importance" label="重要度">
              <Rate count={5} allowClear={false} />
            </Form.Item>
          </Col>
          <Col span={14}>
            <Form.Item
              name="include_in_context"
              label="注入上下文"
              valuePropName="checked"
              tooltip="开启后，生成相关章节时会在【伏笔提醒】中提示模型"
            >
              <Switch />
            </Form.Item>
          </Col>
        </Row>
        <Form.Item name="character_ids" label="关联角色">
          <Select
            mode="multiple"
            allowClear
            optionFilterProp="label"
            placeholder="可选"
            options={characters.map((c) => ({ value: c.id, label: c.name }))}
          />
        </Form.Item>
        <Form.Item name="note" label="备注">
          <Input.TextArea autoSize={{ minRows: 1, maxRows: 4 }} />
        </Form.Item>
        <Button type="primary" loading={saving} onClick={() => void handleSave()}>
          保存
        </Button>
        {current?.source === 'analysis' && (
          <Typography.Text type="secondary" style={{ marginLeft: 12, fontSize: 12 }}>
            由章节分析识别后确认创建
          </Typography.Text>
        )}
      </Form>
      {current && (
        <div style={{ marginTop: 24 }}>
          <Typography.Title level={5}>变更历史</Typography.Title>
          {historyLoading ? <Typography.Text type="secondary">加载中…</Typography.Text> : <ForeshadowHistoryList items={history} />}
        </div>
      )}
    </>
  )
}
