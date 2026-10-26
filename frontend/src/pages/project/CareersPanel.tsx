import { PlusOutlined, RobotOutlined } from '@ant-design/icons'
import { App, Button, Card, Col, Empty, Popconfirm, Row, Space, Spin, Tag, Tooltip, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { CAREER_KIND_LABELS, careerApi } from '../../api/careers'
import { errorMessage } from '../../api/client'
import type { Career, CareerKind } from '../../api/types'
import CareerEditDrawer from '../../components/careers/CareerEditDrawer'
import EntityTaskModal from '../../components/entities/EntityTaskModal'
import WizardModal from '../../components/WizardModal'
import { useLoader } from '../../hooks/useLoader'
import { useProjectId } from '../../hooks/useProjectId'

const GROUPS: CareerKind[] = ['main', 'sub']

/** 职业：按主修 / 副修分组的卡片（阶梯 + 持有人数），编辑抽屉维护阶段与查看持有人；AI 生成职业 / 职业体系向导。 */
export default function CareersPanel() {
  const pid = useProjectId()
  const { message } = App.useApp()
  const loader = useCallback(() => careerApi.list(pid), [pid])
  const { data: items = [], loading, reload } = useLoader(loader)
  const [editing, setEditing] = useState<Career | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [generateOpen, setGenerateOpen] = useState(false)
  const [wizardOpen, setWizardOpen] = useState(false)

  const openEdit = (c: Career | null) => {
    setEditing(c)
    setDrawerOpen(true)
  }

  const handleDelete = async (c: Career) => {
    try {
      await careerApi.remove(pid, c.id)
      message.success('已删除（角色的挂接与阶段历史一并清理）')
      reload()
    } catch (err) {
      message.error(errorMessage(err))
    }
  }

  const renderCard = (c: Career) => (
    <Col key={c.id} xs={24} md={12} xl={8}>
      <Card
        size="small"
        title={
          <Space size={4}>
            {c.name}
            <Tag color={c.kind === 'main' ? 'gold' : 'blue'}>{c.kind === 'sub' ? '副修' : '主修'}</Tag>
          </Space>
        }
        extra={
          <Space size={0}>
            <Button type="link" size="small" onClick={() => openEdit(c)}>
              编辑
            </Button>
            <Popconfirm
              title="删除该职业？"
              description={c.holder_count > 0 ? `${c.holder_count} 个角色的挂接与阶段历史会一并删除。` : undefined}
              okText="删除"
              cancelText="取消"
              okButtonProps={{ danger: true }}
              onConfirm={() => handleDelete(c)}
            >
              <Button type="link" size="small" danger>
                删除
              </Button>
            </Popconfirm>
          </Space>
        }
      >
        <Space direction="vertical" size={6} style={{ width: '100%' }}>
          <Space size={[2, 4]} wrap>
            {c.stages.map((s, i) => (
              <span key={s.id}>
                <Tooltip title={s.description || undefined}>
                  <Tag style={{ marginInlineEnd: 0 }}>
                    {s.position}·{s.name}
                  </Tag>
                </Tooltip>
                {i < c.stages.length - 1 && <Typography.Text type="secondary"> → </Typography.Text>}
              </span>
            ))}
          </Space>
          {c.description && (
            <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }} ellipsis={{ rows: 2, expandable: true, symbol: '展开' }}>
              {c.description}
            </Typography.Paragraph>
          )}
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {c.stages.length} 阶 · {c.holder_count > 0 ? `${c.holder_count} 个角色` : '暂无角色'}
          </Typography.Text>
        </Space>
      </Card>
    </Col>
  )

  return (
    <Card
      title="职业"
      extra={
        <Space wrap>
          <Button icon={<RobotOutlined />} onClick={() => setGenerateOpen(true)}>
            AI 生成职业
          </Button>
          <Button onClick={() => setWizardOpen(true)}>AI 重新生成全部</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => openEdit(null)}>
            新增职业
          </Button>
        </Space>
      }
    >
      <Spin spinning={loading}>
        {items.length === 0 ? (
          <Empty description="暂无职业。职业用于记录角色的境界 / 等级，生成章节时会注入涉及角色的职业与完整阶梯" />
        ) : (
          <Space direction="vertical" size="large" style={{ width: '100%' }}>
            {GROUPS.map((g) => {
              const list = items.filter((c) => (c.kind === 'sub' ? 'sub' : 'main') === g)
              if (list.length === 0) return null
              return (
                <div key={g}>
                  <Typography.Title level={5}>
                    {CAREER_KIND_LABELS[g]}（{list.length}）
                    <Typography.Text type="secondary" style={{ fontSize: 12, fontWeight: 'normal', marginLeft: 8 }}>
                      {g === 'main' ? '每个角色最多 1 个主职业' : '每个角色最多 3 个副职业'}
                    </Typography.Text>
                  </Typography.Title>
                  <Row gutter={[16, 16]}>{list.map(renderCard)}</Row>
                </div>
              )
            })}
          </Space>
        )}
      </Spin>

      <CareerEditDrawer
        projectId={pid}
        open={drawerOpen}
        career={editing}
        onClose={() => setDrawerOpen(false)}
        onSaved={(c) => {
          setEditing(c)
          reload()
        }}
      />
      <EntityTaskModal projectId={pid} kind="career.generate" open={generateOpen} onClose={() => setGenerateOpen(false)} onSaved={reload} />
      <WizardModal
        projectId={pid}
        kind="careers"
        open={wizardOpen}
        existingCount={items.length}
        onClose={() => setWizardOpen(false)}
        onDone={() => reload()}
      />
    </Card>
  )
}
