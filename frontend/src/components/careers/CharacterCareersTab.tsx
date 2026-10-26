import { PlusOutlined } from '@ant-design/icons'
import { App, Button, Empty, Form, Input, InputNumber, List, Modal, Popconfirm, Select, Space, Table, Tag, Typography } from 'antd'
import { useCallback, useState } from 'react'
import {
  assignBlockReason,
  assignmentApi,
  CAREER_HISTORY_FIELD_LABELS,
  CAREER_KIND_LABELS,
  CAREER_ROLE_LABELS,
  CAREER_SOURCE_LABELS,
  careerApi,
  formatCareerChange,
  stageLabel,
} from '../../api/careers'
import { errorMessage } from '../../api/client'
import type { Assignment, CareerHistory, Character } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { formatTime } from '../../utils/time'

interface Props {
  projectId: number
  character: Character
  /** 挂接变化后回调（刷新角色卡片上的主职业） */
  onChanged?: () => void
}

interface FormValues {
  career_id: number
  stage_id: number | null
  note: string
  chapter_no: number | null
  history_note: string
}

function HistoryList({ items }: { items: CareerHistory[] }) {
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
              <Typography.Text strong>{h.career_name}</Typography.Text>
              <Tag>{CAREER_SOURCE_LABELS[h.source] ?? h.source}</Tag>
              {h.chapter_no !== null && <Tag color="blue">第 {h.chapter_no} 章</Tag>}
              {h.note && <Typography.Text>{h.note}</Typography.Text>}
            </Space>
            {Object.entries(h.changes).map(([field, pair]) => {
              const [oldV, newV] = Array.isArray(pair) ? pair : [undefined, pair]
              return (
                <Typography.Text key={field} style={{ fontSize: 13 }}>
                  {field === 'removed'
                    ? `卸下职业：${formatCareerChange(field, oldV)}`
                    : `${CAREER_HISTORY_FIELD_LABELS[field] ?? field}：${formatCareerChange(field, oldV)} → ${formatCareerChange(field, newV)}`}
                </Typography.Text>
              )
            })}
          </Space>
        </List.Item>
      )}
    />
  )
}

/** 角色抽屉「职业」Tab：挂接 / 换阶 / 改备注 / 卸下（每次变化写历史），每条可展开查看该职业的历史。 */
export default function CharacterCareersTab({ projectId, character, onChanged }: Props) {
  const { message } = App.useApp()
  const loader = useCallback(() => assignmentApi.list(projectId, character.id), [projectId, character.id])
  const { data: rows = [], loading, reload } = useLoader(loader)
  const historyLoader = useCallback(() => assignmentApi.history(projectId, character.id), [projectId, character.id])
  const { data: history = [], reload: reloadHistory } = useLoader(historyLoader)
  const careerLoader = useCallback(() => careerApi.list(projectId), [projectId])
  const { data: careers = [] } = useLoader(careerLoader)
  const [editing, setEditing] = useState<Assignment | null>(null)
  const [open, setOpen] = useState(false)
  const [showAll, setShowAll] = useState(false)
  const [saving, setSaving] = useState(false)
  const [form] = Form.useForm<FormValues>()
  const careerId = Form.useWatch('career_id', form)
  const selectedCareer = careers.find((c) => c.id === careerId)

  const refresh = () => {
    reload()
    reloadHistory()
    onChanged?.()
  }

  const openEdit = (a: Assignment | null) => {
    setEditing(a)
    form.setFieldsValue(
      a
        ? { career_id: a.career_id, stage_id: a.stage_id, note: a.note, chapter_no: null, history_note: '' }
        : { career_id: undefined, stage_id: null, note: '', chapter_no: null, history_note: '' },
    )
    setOpen(true)
  }

  const handleSave = async () => {
    const v = await form.validateFields()
    setSaving(true)
    try {
      const common = { note: v.note?.trim() ?? '', chapter_no: v.chapter_no ?? null, history_note: v.history_note?.trim() ?? '' }
      if (editing) {
        await assignmentApi.update(projectId, character.id, editing.id, { stage_id: v.stage_id, ...common })
      } else {
        await assignmentApi.create(projectId, character.id, { career_id: v.career_id, stage_id: v.stage_id ?? null, ...common })
      }
      message.success('已保存')
      setOpen(false)
      refresh()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const changeStage = async (a: Assignment, stageId: number) => {
    try {
      await assignmentApi.update(projectId, character.id, a.id, { stage_id: stageId })
      message.success('已换阶')
      refresh()
    } catch (err) {
      message.error(errorMessage(err))
    }
  }

  const handleRemove = async (a: Assignment) => {
    try {
      await assignmentApi.remove(projectId, character.id, a.id)
      message.success('已卸下')
      refresh()
    } catch (err) {
      message.error(errorMessage(err))
    }
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Space>
        <Button icon={<PlusOutlined />} onClick={() => openEdit(null)} disabled={careers.length === 0}>
          挂接职业
        </Button>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          最多 1 个主职业、3 个副职业；每次换阶 / 改备注都会记入历史。
        </Typography.Text>
      </Space>
      <Table<Assignment>
        size="small"
        rowKey="id"
        loading={loading}
        dataSource={rows}
        pagination={false}
        expandable={{
          expandedRowRender: (a) => <HistoryList items={history.filter((h) => h.career_id === a.career_id)} />,
          rowExpandable: () => true,
        }}
        locale={{
          emptyText: (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={careers.length === 0 ? '项目中还没有职业，可在「职业」页添加' : '未挂接任何职业'}
            />
          ),
        }}
        columns={[
          {
            title: '身份',
            width: 80,
            render: (_, a) => (
              <Tag color={a.role === 'main' ? 'gold' : 'blue'}>
                {a.role === 'main' || a.role === 'sub' ? CAREER_ROLE_LABELS[a.role] : a.role}
              </Tag>
            ),
          },
          { title: '职业', dataIndex: 'career_name' },
          {
            title: '阶段',
            width: 200,
            render: (_, a) => {
              const c = careers.find((x) => x.id === a.career_id)
              return (
                <Select
                  size="small"
                  style={{ width: '100%' }}
                  value={a.stage_id}
                  labelRender={() => stageLabel(a.stage_name, a.stage_position, a.stage_total)}
                  options={(c?.stages ?? []).map((s) => ({ value: s.id, label: `${s.position}·${s.name}` }))}
                  onChange={(v: number) => v !== a.stage_id && void changeStage(a, v)}
                />
              )
            },
          },
          { title: '状态备注', dataIndex: 'note', render: (v: string) => v || '-' },
          {
            title: '',
            width: 110,
            render: (_, a) => (
              <Space size={0}>
                <Button type="link" size="small" onClick={() => openEdit(a)}>
                  编辑
                </Button>
                <Popconfirm title="卸下该职业？" description="会记入历史。" okText="卸下" cancelText="取消" onConfirm={() => handleRemove(a)}>
                  <Button type="link" size="small" danger>
                    卸下
                  </Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
      <Button type="link" size="small" style={{ paddingInline: 0 }} onClick={() => setShowAll((s) => !s)}>
        {showAll ? '收起全部历史' : `查看全部职业历史（${history.length}，含已卸下的职业）`}
      </Button>
      {showAll && <HistoryList items={history} />}

      <Modal
        title={editing ? `编辑：${editing.career_name}` : '挂接职业'}
        open={open}
        onOk={() => void handleSave()}
        onCancel={() => setOpen(false)}
        confirmLoading={saving}
        okText="保存"
        cancelText="取消"
        forceRender
      >
        <Form form={form} layout="vertical">
          <Form.Item name="career_id" label="职业" rules={[{ required: true, message: '请选择职业' }]}>
            <Select
              disabled={!!editing}
              showSearch
              optionFilterProp="label"
              onChange={() => form.setFieldValue('stage_id', null)}
              options={careers.map((c) => {
                const reason = editing ? null : assignBlockReason(rows, c)
                return {
                  value: c.id,
                  label: `${c.name}（${CAREER_KIND_LABELS[c.kind === 'sub' ? 'sub' : 'main']}）${reason ? ` · ${reason}` : ''}`,
                  disabled: !!reason,
                }
              })}
            />
          </Form.Item>
          <Form.Item name="stage_id" label="阶段" tooltip={editing ? undefined : '不选 = 第 1 阶'}>
            <Select
              allowClear={!editing}
              placeholder={selectedCareer ? `第 1 阶 · ${selectedCareer.stages[0]?.name ?? ''}` : '先选择职业'}
              options={(selectedCareer?.stages ?? []).map((s) => ({ value: s.id, label: `${s.position}·${s.name}` }))}
            />
          </Form.Item>
          <Form.Item name="note" label="状态备注" rules={[{ max: 200, message: '不超过 200 字' }]}>
            <Input placeholder="例如：半步金丹 / 瓶颈期" />
          </Form.Item>
          <Space size="large" align="start">
            <Form.Item name="chapter_no" label="发生在第几章（可选）">
              <InputNumber min={1} style={{ width: 160 }} />
            </Form.Item>
            <Form.Item name="history_note" label="历史备注（可选）" rules={[{ max: 200, message: '不超过 200 字' }]}>
              <Input style={{ width: 260 }} />
            </Form.Item>
          </Space>
        </Form>
      </Modal>
    </Space>
  )
}
