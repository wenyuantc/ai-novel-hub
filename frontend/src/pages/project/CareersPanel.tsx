import { PlusOutlined, RobotOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Empty, Popconfirm, Space, Spin, Tag, Tooltip, Typography } from 'antd'
import { useCallback, useState, type CSSProperties } from 'react'
import { CAREER_KIND_LABELS, careerApi } from '../../api/careers'
import { errorMessage } from '../../api/client'
import type { Career, CareerKind, ProgressionPositionInput } from '../../api/types'
import { useSearchParams } from 'react-router-dom'
import { readEntityFieldPosition } from '../../utils/entityLinks'
import { invalidateProject } from '../../utils/projectRefresh'
import AutoGrid from '../../components/AutoGrid'
import CareerEditDrawer from '../../components/careers/CareerEditDrawer'
import { useSettingAiSession } from '../../components/settingAi/useSettingAiSession'
import { useSettingTagsSession } from '../../components/settingTags/useSettingTagsSession'
import EntityTaskModal from '../../components/entities/EntityTaskModal'
import WizardModal from '../../components/WizardModal'
import { useLoader } from '../../hooks/useLoader'
import { useEntityDeepLink } from '../../hooks/useEntityDeepLink'
import { useProjectId } from '../../hooks/useProjectId'
import { useProjectAccess } from '../../hooks/useProjectAccess'

const GROUPS: CareerKind[] = ['main', 'sub']

/** 卡片标题允许换行：窄卡片里名字 + 标签放不下时折行，而不是被省略号截断。 */
const cardTitleWrap: CSSProperties = { whiteSpace: 'normal' }

/** 职业：按主修 / 副修分组的卡片（阶梯 + 持有人数），编辑抽屉维护阶段与查看持有人；AI 生成职业 / 职业体系向导。 */
export default function CareersPanel() {
  const pid = useProjectId()
  const tagsSession = useSettingTagsSession(pid)
  const aiSession = useSettingAiSession(pid)
  const [searchParams] = useSearchParams()
  const { canUseAI, canEdit, canEditNow, canReadNow } = useProjectAccess(pid)
  const { message } = App.useApp()
  const loader = useCallback(() => careerApi.list(pid), [pid])
  const { data: loadedItems, loading, error: listError, reload } = useLoader(loader, { projectId: pid, resources: ['careers', 'characters'] })
  const items = loadedItems ?? []
  const [editing, setEditing] = useState<Career | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [fieldPosition, setFieldPosition] = useState<ProgressionPositionInput | null>(null)
  const [generateOpen, setGenerateOpen] = useState(false)
  const [wizardOpen, setWizardOpen] = useState(false)

  const openEdit = (c: Career | null) => {
    setFieldPosition(c && searchParams.get('id') === String(c.id) ? readEntityFieldPosition(searchParams) : null)
    setEditing(c)
    setDrawerOpen(true)
  }

  useEntityDeepLink(pid, loadedItems, loading, openEdit, canReadNow)

  const handleDelete = async (c: Career) => {
    if (!canEditNow()) return
    try {
      if (!canEditNow()) return
      await careerApi.remove(pid, c.id)
      tagsSession.forget({ kind: 'career', id: c.id })
      aiSession.forget({ kind: 'career', id: c.id })
      if (!canEditNow()) return
      message.success('已删除（角色的挂接与阶段历史一并清理）')
      invalidateProject(pid, ['careers', 'setting-ai-visibility'])
    } catch (err) {
      if (!canEditNow()) return
      message.error(errorMessage(err))
    }
  }

  const renderCard = (c: Career) => (
      <Card
        key={c.id}
        size="small"
        styles={{ title: cardTitleWrap }}
        title={
          <Space size={4} wrap>
            {c.name}
            <Tag color={c.kind === 'main' ? 'gold' : 'blue'}>{c.kind === 'sub' ? '副修' : '主修'}</Tag>
          </Space>
        }
        extra={
          <Space size={0}>
            <Button type="link" size="small" onClick={() => openEdit(c)}>
              {canEdit ? '编辑' : '查看'}</Button>
            <Popconfirm
              title="删除该职业？"
              description={c.holder_count > 0 ? `${c.holder_count} 个角色的挂接与阶段历史会一并删除。` : undefined}
              okText="删除"
              cancelText="取消"
              okButtonProps={{ danger: true }}
              onConfirm={() => handleDelete(c)}
            >
              <Button type="link" size="small" danger disabled={!canEdit}>
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
  )

  return (
    <Card
      title="职业"
      extra={
        <Space wrap>
          {canUseAI && (<Button icon={<RobotOutlined />} onClick={() => setGenerateOpen(true)}>
            AI 生成职业
          </Button>)}
          {canUseAI && (<Button onClick={() => setWizardOpen(true)}>AI 重新生成全部</Button>)}
          <Button type="primary" icon={<PlusOutlined />} onClick={() => openEdit(null)} disabled={!canEdit}>
            新增职业
          </Button>
        </Space>
      }
    >
      {listError && <Alert type="error" showIcon message={listError} action={<Button onClick={reload}>重试</Button>} style={{ marginBottom: 12 }} />}
      <Spin spinning={loading}>
        {items.length === 0 ? (
          <Empty description="暂无职业。职业用于记录角色的境界或等级；AI 只使用符合纳入策略且未隐藏的资料。" />
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
                  <AutoGrid>{list.map(renderCard)}</AutoGrid>
                </div>
              )
            })}
          </Space>
        )}
      </Spin>

      <CareerEditDrawer
        tagsSession={tagsSession}
        aiSession={aiSession}
        projectId={pid}
        open={drawerOpen}
        career={editing}
        fieldPosition={fieldPosition}
        onClose={() => setDrawerOpen(false)}
        onSaved={(c) => {
          setEditing(c)
          invalidateProject(pid, ['careers'])
        }}
      />
      {canUseAI && (<EntityTaskModal projectId={pid} kind="career.generate" open={generateOpen} onClose={() => setGenerateOpen(false)} onSaved={reload} />)}
      {canUseAI && (<WizardModal
        projectId={pid}
        kind="careers"
        open={wizardOpen}
        existingCount={items.length}
        onClose={() => setWizardOpen(false)}
        onDone={() => reload()}
      />)}
    </Card>
  )
}
