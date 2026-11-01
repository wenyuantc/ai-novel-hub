import { useProjectAccess } from '../../hooks/useProjectAccess'
import { App, Button, Col, Drawer, Empty, Form, Input, Row, Select, Table, Tag, Typography } from 'antd'
import { useCallback, useMemo, useState } from 'react'
import {
  CAREER_KIND_LABELS,
  CAREER_ROLE_LABELS,
  careerApi,
  newStageRow,
  rowsToStages,
  stageLabel,
  stagesToRows,
  validateStages,
  type StageRow,
} from '../../api/careers'
import { errorMessage } from '../../api/client'
import type { Career, CareerHolder, CareerKind } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import StageListEditor from './StageListEditor'

interface Props {
  projectId: number
  open: boolean
  /** null = 新建 */
  career: Career | null
  onClose: () => void
  onSaved: (career: Career) => void
}

interface FormValues {
  name: string
  kind: CareerKind
  description: string
  requirements: string
  abilities: string
  rules: string
}

const KIND_OPTIONS = (['main', 'sub'] as CareerKind[]).map((k) => ({ value: k, label: CAREER_KIND_LABELS[k] }))

/** 职业编辑抽屉：基础信息 + 阶段阶梯（排序 / 增删）+ 持有人列表；仅打开时挂载内部状态。 */
export default function CareerEditDrawer(props: Props) {
  return (
    <Drawer
      title={props.career ? `编辑职业：${props.career.name}` : '新增职业'}
      open={props.open}
      onClose={props.onClose}
      width={760}
      destroyOnHidden
    >
      {props.open && <Inner key={props.career?.id ?? 'new'} {...props} />}
    </Drawer>
  )
}

function Inner({ projectId, career, onSaved }: Props) {
  const { canEdit, canEditNow } = useProjectAccess(projectId)
  const { message } = App.useApp()
  const [form] = Form.useForm<FormValues>()
  const [stages, setStages] = useState<StageRow[]>(() =>
    career ? stagesToRows(career.stages) : [newStageRow('第一阶')],
  )
  const [saving, setSaving] = useState(false)
  const [current, setCurrent] = useState<Career | null>(career)
  const currentId = current?.id ?? null
  const currentStamp = current?.updated_at ?? ''
  // 保存后（updated_at 变化）重新加载持有人
  const holdersLoader = useCallback(
    async () => (currentId === null || !currentStamp ? [] : careerApi.holders(projectId, currentId)),
    [projectId, currentId, currentStamp],
  )
  const { data: holders = [], loading: holdersLoading } = useLoader(holdersLoader)
  const stageError = validateStages(stages)
  const lockedIds = useMemo(() => new Set(holders.map((h) => h.stage_id)), [holders])

  const handleSave = async () => {
    if (!canEditNow()) return
    let v: FormValues
    try {
      if (!canEditNow()) return
      v = await form.validateFields()
      if (!canEditNow()) return
    } catch {
      if (!canEditNow()) return
      return
    }
    if (stageError) {
      message.warning(stageError)
      return
    }
    const body = {
      name: v.name.trim(),
      kind: v.kind,
      description: v.description ?? '',
      requirements: v.requirements ?? '',
      abilities: v.abilities ?? '',
      rules: v.rules ?? '',
      stages: rowsToStages(stages),
    }
    setSaving(true)
    try {
      if (!canEditNow()) return
      const saved = current ? await careerApi.update(projectId, current.id, body) : await careerApi.create(projectId, body)
      if (!canEditNow()) return
      message.success('已保存')
      setCurrent(saved)
      setStages(stagesToRows(saved.stages))
      onSaved(saved)
    } catch (err) {
      if (!canEditNow()) return
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <Form
        form={form}
        layout="vertical"
        disabled={!canEdit || (saving)}
        initialValues={{
          name: career?.name ?? '',
          kind: (career?.kind as CareerKind | undefined) ?? 'main',
          description: career?.description ?? '',
          requirements: career?.requirements ?? '',
          abilities: career?.abilities ?? '',
          rules: career?.rules ?? '',
        }}
      >
        <Row gutter={16}>
          <Col span={14}>
            <Form.Item
              name="name"
              label="名称"
              rules={[
                { required: true, whitespace: true, message: '请输入名称' },
                { max: 100, message: '不超过 100 字' },
              ]}
            >
              <Input placeholder="例如：剑修" />
            </Form.Item>
          </Col>
          <Col span={10}>
            <Form.Item
              name="kind"
              label="类别"
              tooltip="主修职业只能挂成主职业（每个角色 1 个），副修职业只能挂成副职业（最多 3 个）"
            >
              <Select options={KIND_OPTIONS} />
            </Form.Item>
          </Col>
        </Row>
        <Form.Item name="description" label="描述">
          <Input.TextArea autoSize={{ minRows: 2, maxRows: 6 }} />
        </Form.Item>
        <Row gutter={16}>
          <Col span={12}>
            <Form.Item name="requirements" label="获得条件">
              <Input.TextArea autoSize={{ minRows: 1, maxRows: 4 }} />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="abilities" label="特殊能力">
              <Input.TextArea autoSize={{ minRows: 1, maxRows: 4 }} />
            </Form.Item>
          </Col>
        </Row>
        <Form.Item name="rules" label="体系规则" tooltip="生成章节时附在【职业体系】段落中（前 200 字）">
          <Input.TextArea autoSize={{ minRows: 2, maxRows: 6 }} />
        </Form.Item>
      </Form>
      <Typography.Title level={5}>阶段阶梯（{stages.length} 阶）</Typography.Title>
      <StageListEditor value={stages} onChange={setStages} disabled={!canEdit || saving} lockedIds={lockedIds} error={stageError} />
      <div style={{ margin: '16px 0' }}>
        <Button type="primary" loading={saving} onClick={() => void handleSave()} disabled={!canEdit}>
          保存
        </Button>
      </div>
      {current && (
        <>
          <Typography.Title level={5}>持有人（{holders.length}）</Typography.Title>
          <Table<CareerHolder>
            size="small"
            rowKey="id"
            loading={holdersLoading}
            pagination={false}
            dataSource={holders}
            locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有角色挂这个职业" /> }}
            columns={[
              { title: '角色', dataIndex: 'character_name' },
              {
                title: '身份',
                width: 90,
                render: (_, h) => (
                  <Tag color={h.role === 'main' ? 'gold' : 'blue'}>
                    {h.role === 'main' || h.role === 'sub' ? CAREER_ROLE_LABELS[h.role] : h.role}
                  </Tag>
                ),
              },
              { title: '阶段', render: (_, h) => stageLabel(h.stage_name, h.stage_position, h.stage_total) },
              { title: '状态备注', dataIndex: 'note', render: (v: string) => v || '-' },
            ]}
          />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            在角色抽屉的「职业」Tab 中挂接、换阶或卸下。
          </Typography.Text>
        </>
      )}
    </>
  )
}
