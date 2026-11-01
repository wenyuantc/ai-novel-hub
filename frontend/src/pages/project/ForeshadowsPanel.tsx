import { useProjectAccess } from '../../hooks/useProjectAccess'
import { PlusOutlined } from '@ant-design/icons'
import {
  App,
  Button,
  Card,
  Checkbox,
  Dropdown,
  InputNumber,
  Modal,
  Rate,
  Segmented,
  Select,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  theme,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { errorMessage } from '../../api/client'
import {
  defaultPlantChapter,
  defaultResolveChapter,
  FORESHADOW_STATUS,
  FORESHADOW_STATUSES,
  foreshadowApi,
  foreshadowChapterSummary,
} from '../../api/foreshadows'
import { characterApi } from '../../api/story'
import type { Foreshadow, ForeshadowListQuery, ForeshadowSort, ForeshadowStatus } from '../../api/types'
import AutoGrid from '../../components/AutoGrid'
import ForeshadowEditDrawer from '../../components/foreshadows/ForeshadowEditDrawer'
import ForeshadowTimeline from '../../components/foreshadows/ForeshadowTimeline'
import { useLoader } from '../../hooks/useLoader'
import { useProjectId } from '../../hooks/useProjectId'

const ALL = '__all__'

interface QuickAction {
  mode: 'plant' | 'resolve'
  foreshadow: Foreshadow
  chapter: number | null
  partial: boolean
}

function positiveInt(v: string | null): number | undefined {
  const n = Number(v)
  return Number.isInteger(n) && n > 0 ? n : undefined
}

/** 伏笔：统计卡 + 筛选排序 + 列表（快捷操作：标已埋 / 标回收 / 废弃）/ 时间线，编辑抽屉含历史。URL `?character_id=` 预置角色筛选，`?id=` 打开该伏笔。 */
export default function ForeshadowsPanel() {
  const { token } = theme.useToken()
  const pid = useProjectId()
  const { canEdit, canEditNow, canReadNow } = useProjectAccess(pid)
  const { message } = App.useApp()
  const [searchParams, setSearchParams] = useSearchParams()
  const [status, setStatus] = useState<ForeshadowStatus | typeof ALL>(ALL)
  const [overdueOnly, setOverdueOnly] = useState(false)
  const [longTermOnly, setLongTermOnly] = useState(false)
  const [characterId, setCharacterId] = useState<number | undefined>(() => positiveInt(searchParams.get('character_id')))
  const [sort, setSort] = useState<ForeshadowSort>('planned_resolve')
  const [tab, setTab] = useState('list')
  const [editing, setEditing] = useState<Foreshadow | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [quick, setQuick] = useState<QuickAction | null>(null)
  const [acting, setActing] = useState(false)
  // 删除确认走 Modal（Dropdown 菜单项内挂 Popconfirm 会随菜单关闭而消失）
  const [deleting, setDeleting] = useState<Foreshadow | null>(null)

  const query = useMemo<ForeshadowListQuery>(
    () => ({
      status: status === ALL ? undefined : status,
      overdue: overdueOnly,
      long_term: longTermOnly,
      character_id: characterId,
      sort,
    }),
    [status, overdueOnly, longTermOnly, characterId, sort],
  )
  const listLoader = useCallback(() => foreshadowApi.list(pid, query), [pid, query])
  const { data: items = [], loading, reload } = useLoader(listLoader, { projectId: pid, resources: ['foreshadows', 'characters', 'chapters'] })
  const statsLoader = useCallback(() => foreshadowApi.stats(pid), [pid])
  const { data: stats, reload: reloadStats } = useLoader(statsLoader, { projectId: pid, resources: ['foreshadows', 'characters', 'chapters'] })
  const charLoader = useCallback(() => characterApi.list(pid), [pid])
  const { data: characters = [] } = useLoader(charLoader, { projectId: pid, resources: ['foreshadows', 'characters', 'chapters'] })
  const currentChapter = stats?.current_chapter ?? 0

  const refresh = useCallback(() => {
    reload()
    reloadStats()
  }, [reload, reloadStats])

  // ?id= 打开指定伏笔（角色抽屉「伏笔」Tab 跳转过来）；打开后从 URL 去掉 id
  const openId = positiveInt(searchParams.get('id'))
  useEffect(() => {
    if (openId === undefined) return
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        next.delete('id')
        return next
      },
      { replace: true },
    )
    let alive = true
    foreshadowApi.get(pid, openId).then(
      (f) => {
        if (!alive || !canReadNow()) return
        setEditing(f)
        setDrawerOpen(true)
      },
      (err: unknown) => { if (alive && canReadNow()) message.error(errorMessage(err)) },
    )
    return () => { alive = false }
  }, [openId, pid, message, setSearchParams, canReadNow])

  const openEdit = (f: Foreshadow | null) => {
    setEditing(f)
    setDrawerOpen(true)
  }

  const run = async (fn: () => Promise<unknown>, done: string) => {
    if (!canEditNow()) return
    setActing(true)
    try {
      if (!canEditNow()) return
      await fn()
      if (!canEditNow()) return
      message.success(done)
      refresh()
      return true
    } catch (err) {
      if (!canEditNow()) return
      message.error(errorMessage(err))
      return false
    } finally {
      setActing(false)
    }
  }

  const handleQuickOk = async () => {
    if (!canEditNow()) return
    if (!quick) return
    if (quick.chapter === null) {
      message.warning(quick.mode === 'plant' ? '请填写实际埋设章节' : '请填写实际回收章节')
      return
    }
    const { foreshadow: f, chapter } = quick
    if (!canEditNow()) return
    const ok =
      quick.mode === 'plant'
        ? await run(() => foreshadowApi.plant(pid, f.id, chapter), '已标记为已埋')
        : await run(
            () => foreshadowApi.resolve(pid, f.id, chapter, quick.partial),
            quick.partial ? '已标记为部分回收' : '已标记为已回收',
          )
    if (!canEditNow()) return
    if (ok) setQuick(null)
  }

  const quickActions = (f: Foreshadow) => {
    const menu = [
      ...(f.status === 'pending' || f.status === 'abandoned'
        ? [{ key: 'plant', label: '标记已埋…' }]
        : []),
      ...(f.status === 'planted' || f.status === 'partial' ? [{ key: 'resolve', label: '标记回收…' }] : []),
      ...(f.status !== 'abandoned' ? [{ key: 'abandon', label: '废弃' }] : []),
      { type: 'divider' as const },
      { key: 'delete', label: '删除', danger: true },
    ]
    return (
      <Space size={0}>
        <Button type="link" size="small" onClick={() => openEdit(f)}>
          {canEdit ? '编辑' : '查看'}</Button>
        <Dropdown
          disabled={!canEdit}
          trigger={['click']}
          menu={{
            items: menu,
            onClick: ({ key }) => {
              if (key === 'plant')
                setQuick({ mode: 'plant', foreshadow: f, chapter: defaultPlantChapter(f, currentChapter), partial: false })
              else if (key === 'resolve')
                setQuick({ mode: 'resolve', foreshadow: f, chapter: defaultResolveChapter(f, currentChapter), partial: false })
              else if (key === 'abandon') void run(() => foreshadowApi.abandon(pid, f.id), '已废弃')
              else if (key === 'delete') setDeleting(f)
            },
          }}
        >
          <Button type="link" size="small" disabled={!canEdit || acting}>
            操作
          </Button>
        </Dropdown>
      </Space>
    )
  }

  const columns: ColumnsType<Foreshadow> = [
    {
      title: '伏笔',
      key: 'title',
      render: (_, f) => (
        <Space direction="vertical" size={2} style={{ minWidth: 0 }}>
          <Space size={4} wrap>
            <Typography.Link onClick={() => openEdit(f)}>
              #{f.id} {f.title}
            </Typography.Link>
            {f.overdue_by > 0 && <Tag color="red">超期 {f.overdue_by} 章</Tag>}
            {f.is_long_term && <Tag color="purple">长线</Tag>}
            {!f.include_in_context && (
              <Tooltip title="生成章节时不会出现在【伏笔提醒】中">
                <Tag>不注入</Tag>
              </Tooltip>
            )}
            {f.source === 'analysis' && <Tag color="geekblue">分析</Tag>}
          </Space>
          {f.content && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }} ellipsis={{ tooltip: f.content }}>
              {f.content}
            </Typography.Text>
          )}
        </Space>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 96,
      render: (s: ForeshadowStatus) => <Tag color={FORESHADOW_STATUS[s].color}>{FORESHADOW_STATUS[s].label}</Tag>,
    },
    {
      title: '章节',
      key: 'chapters',
      width: 220,
      render: (_, f) => {
        const c = foreshadowChapterSummary(f)
        return (
          <Space direction="vertical" size={0} style={{ fontSize: 12 }}>
            <span>埋：{c.plant}</span>
            <span style={{ color: f.overdue_by > 0 ? token.colorError : undefined }}>收：{c.resolve}</span>
          </Space>
        )
      },
    },
    {
      title: '重要度',
      dataIndex: 'importance',
      width: 120,
      render: (v: number) => <Rate disabled count={5} value={v} style={{ fontSize: 12 }} />,
    },
    {
      title: '关联角色',
      key: 'chars',
      width: 160,
      render: (_, f) =>
        f.character_names.length ? (
          <Space size={[2, 2]} wrap>
            {f.character_names.map((n, i) => (
              <Tag key={f.character_ids[i] ?? n} style={{ marginInlineEnd: 0 }}>
                {n}
              </Tag>
            ))}
          </Space>
        ) : (
          <Typography.Text type="secondary">-</Typography.Text>
        ),
    },
    { title: '', key: 'actions', width: 120, render: (_, f) => quickActions(f) },
  ]

  const statItems: Array<{ key: string; label: string; value: number; color?: string }> = stats
    ? [
        { key: 'pending', label: '待埋', value: stats.pending },
        { key: 'planted', label: '已埋', value: stats.planted },
        { key: 'partial', label: '部分回收', value: stats.partial },
        { key: 'resolved', label: '已回收', value: stats.resolved },
        { key: 'abandoned', label: '已废弃', value: stats.abandoned },
        { key: 'overdue', label: '超期', value: stats.overdue, color: stats.overdue > 0 ? token.colorError : undefined },
        { key: 'long_term', label: '长线', value: stats.long_term },
      ]
    : []

  const filters = (
    <Space wrap>
      <Select
        style={{ width: 120 }}
        value={status}
        onChange={setStatus}
        options={[{ value: ALL, label: '全部状态' }, ...FORESHADOW_STATUSES.map((s) => ({ value: s, label: FORESHADOW_STATUS[s].label }))]}
      />
      <Checkbox checked={overdueOnly} onChange={(e) => setOverdueOnly(e.target.checked)}>
        仅超期
      </Checkbox>
      <Checkbox checked={longTermOnly} onChange={(e) => setLongTermOnly(e.target.checked)}>
        仅长线
      </Checkbox>
      <Select
        style={{ width: 160 }}
        allowClear
        showSearch
        optionFilterProp="label"
        placeholder="关联角色"
        value={characterId}
        onChange={(v?: number) => setCharacterId(v)}
        options={characters.map((c) => ({ value: c.id, label: c.name }))}
      />
      <Segmented<ForeshadowSort>
        value={sort}
        onChange={setSort}
        options={[
          { value: 'planned_resolve', label: '按计划回收章' },
          { value: 'importance', label: '按重要度' },
        ]}
      />
    </Space>
  )

  return (
    <Card
      title="伏笔"
      extra={
        <Button type="primary" icon={<PlusOutlined />} onClick={() => openEdit(null)} disabled={!canEdit}>
          新增伏笔
        </Button>
      }
    >
      <AutoGrid minItemWidth={96} gap="12px 16px" mobileColumns={3} style={{ marginBottom: 16 }}>
        {statItems.map((s) => (
          <Statistic key={s.key} title={s.label} value={s.value} valueStyle={s.color ? { color: s.color } : undefined} />
        ))}
        {stats && (
          <Statistic
            title={
              <Tooltip title="已有正文的最大章节号，超期按它判断">
                <span>当前章</span>
              </Tooltip>
            }
            value={stats.current_chapter || '-'}
          />
        )}
      </AutoGrid>
      {filters}
      <Tabs
        activeKey={tab}
        onChange={setTab}
        style={{ marginTop: 8 }}
        items={[
          {
            key: 'list',
            label: `列表（${items.length}）`,
            children: (
              <Table<Foreshadow>
                rowKey="id"
                size="small"
                loading={loading}
                columns={columns}
                dataSource={items}
                pagination={items.length > 50 ? { pageSize: 50, showSizeChanger: false } : false}
                locale={{ emptyText: '暂无伏笔。记录计划在哪章埋、哪章收，生成章节时会自动提醒模型' }}
                scroll={{ x: 900 }}
              />
            ),
          },
          {
            key: 'timeline',
            label: '时间线',
            children: <ForeshadowTimeline items={items} currentChapter={currentChapter} onSelect={openEdit} />,
          },
        ]}
      />

      <ForeshadowEditDrawer
        projectId={pid}
        open={drawerOpen}
        foreshadow={editing}
        characters={characters}
        onClose={() => setDrawerOpen(false)}
        onSaved={(f) => {
          setEditing(f)
          refresh()
        }}
      />
      <Modal
        okButtonProps={{ disabled: !canEdit }}
        open={quick !== null}
        title={quick ? `${quick.mode === 'plant' ? '标记已埋' : '标记回收'}：#${quick.foreshadow.id} ${quick.foreshadow.title}` : ''}
        okText="确定"
        cancelText="取消"
        confirmLoading={acting}
        onOk={() => void handleQuickOk()}
        onCancel={() => setQuick(null)}
        destroyOnHidden
      >
        {quick && (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Space>
              <span>{quick.mode === 'plant' ? '实际埋设章' : '实际回收章'}</span>
              <InputNumber
                min={1}
                max={100000}
                precision={0}
                value={quick.chapter}
                onChange={(v) => setQuick({ ...quick, chapter: typeof v === 'number' ? v : null })}
                addonBefore="第"
                addonAfter="章"
              />
            </Space>
            {quick.mode === 'resolve' && (
              <Checkbox checked={quick.partial} onChange={(e) => setQuick({ ...quick, partial: e.target.checked })}>
                部分回收（仍保持未收束，后续可继续回收）
              </Checkbox>
            )}
          </Space>
        )}
      </Modal>
      <Modal
        open={deleting !== null}
        title="删除该伏笔？"
        okText="删除"
        cancelText="取消"
        okButtonProps={{ danger: true, disabled: !canEdit }}
        confirmLoading={acting}
        onCancel={() => setDeleting(null)}
        onOk={() => {
          const f = deleting
          if (!f) return
          void run(() => foreshadowApi.remove(pid, f.id), '已删除').then((ok) => ok && setDeleting(null))
        }}
      >
        {deleting && `#${deleting.id} ${deleting.title} 及其变更历史会一并删除；也可以改为「废弃」保留记录。`}
      </Modal>
    </Card>
  )
}
