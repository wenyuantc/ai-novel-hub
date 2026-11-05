import { PlusOutlined, RobotOutlined, SettingOutlined } from '@ant-design/icons'
import {
  App,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Empty,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Typography,
} from 'antd'
import { lazy, Suspense, useCallback, useMemo, useState } from 'react'
import { lifeStatusLabel, roleTypeColor } from '../../api/characters'
import { errorMessage } from '../../api/client'
import {
  categoryLabel,
  describeRelation,
  intimacyColor,
  RELATION_CATEGORY,
  RELATION_STATUS,
  relationshipApi,
  relationshipTypeApi,
  statusLabel,
} from '../../api/relationships'
import { characterApi } from '../../api/story'
import type { Character, RelationCategory, Relationship, RelationStatus } from '../../api/types'
import EntityTaskModal from '../../components/entities/EntityTaskModal'
import RelationEditModal from '../../components/relations/RelationEditModal'
import RelationHistoryModal, { RelationPairHistory } from '../../components/relations/RelationHistoryModal'
import RelationTypeManager from '../../components/relations/RelationTypeManager'
import { useLoader } from '../../hooks/useLoader'
import { useProjectId } from '../../hooks/useProjectId'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { DEFAULT_GRAPH_FILTER, filterGraph, type GraphFilter } from '../../utils/graphFilter'

// 图谱依赖 @xyflow/react + @dagrejs/dagre，单独打包，只在打开「图谱」Tab 时加载
const RelationGraph = lazy(() => import('../../components/relations/RelationGraph'))

const CATEGORY_OPTIONS = (Object.keys(RELATION_CATEGORY) as RelationCategory[]).map((k) => ({
  value: k,
  label: RELATION_CATEGORY[k].label,
}))
const STATUS_OPTIONS = (Object.keys(RELATION_STATUS) as RelationStatus[]).map((k) => ({ value: k, label: RELATION_STATUS[k].label }))

/** 人物关系：列表（筛选 / 增删改 / 历史 / 类型管理）+ 懒加载的关系图谱。 */
export default function RelationshipsPanel() {
  const pid = useProjectId()
  const { canUseAI, canEdit, canEditNow } = useProjectAccess(pid)
  const { message } = App.useApp()
  const [tab, setTab] = useState('list')
  const loader = useCallback(() => relationshipApi.list(pid), [pid])
  const { data: rels = [], loading, reload } = useLoader(loader, { projectId: pid, resources: ['relationships', 'characters', 'organizations'] })
  const charLoader = useCallback(() => characterApi.list(pid), [pid])
  const { data: characters = [] } = useLoader(charLoader, { projectId: pid, resources: ['relationships', 'characters', 'organizations'] })
  const typeLoader = useCallback(() => relationshipTypeApi.list(pid), [pid])
  const { data: types = [], reload: reloadTypes } = useLoader(typeLoader, { projectId: pid, resources: ['relationships', 'characters', 'organizations'] })
  const [charFilter, setCharFilter] = useState<number | null>(null)
  const [catFilter, setCatFilter] = useState<string | null>(null)
  const [statusFilter, setStatusFilter] = useState<string | null>(null)
  const [editing, setEditing] = useState<Relationship | null>(null)
  const [editOpen, setEditOpen] = useState(false)
  const [history, setHistory] = useState<Relationship | null>(null)
  const [pairHistoryOpen, setPairHistoryOpen] = useState(false)
  const [historyA, setHistoryA] = useState<number | undefined>()
  const [historyB, setHistoryB] = useState<number | undefined>()
  const [typesOpen, setTypesOpen] = useState(false)
  const [generateOpen, setGenerateOpen] = useState(false)

  const shown = rels.filter(
    (r) =>
      (charFilter === null || r.a_id === charFilter || r.b_id === charFilter) &&
      (catFilter === null || r.category === catFilter) &&
      (statusFilter === null || r.status === statusFilter),
  )

  const handleDelete = async (r: Relationship) => {
    if (!canEditNow()) return
    try {
      if (!canEditNow()) return
      await relationshipApi.remove(pid, r.id)
      if (!canEditNow()) return
      message.success('已删除')
      reload()
    } catch (err) {
      if (!canEditNow()) return
      message.error(errorMessage(err))
    }
  }

  const list = (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Space wrap>
        <Select
          allowClear
          showSearch
          optionFilterProp="label"
          placeholder="按角色筛选"
          style={{ width: 160 }}
          value={charFilter}
          onChange={(v: number | undefined) => setCharFilter(v ?? null)}
          options={characters.map((c) => ({ value: c.id, label: c.name }))}
        />
        <Select
          allowClear
          placeholder="类别"
          style={{ width: 110 }}
          value={catFilter}
          onChange={(v: string | undefined) => setCatFilter(v ?? null)}
          options={CATEGORY_OPTIONS}
        />
        <Select
          allowClear
          placeholder="状态"
          style={{ width: 110 }}
          value={statusFilter}
          onChange={(v: string | undefined) => setStatusFilter(v ?? null)}
          options={STATUS_OPTIONS}
        />
      </Space>
      <Table<Relationship>
        size="small"
        rowKey="id"
        loading={loading}
        dataSource={shown}
        pagination={{ pageSize: 20, hideOnSinglePage: true }}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无关系" /> }}
        columns={[
          { title: '关系', render: (_, r) => describeRelation(r) },
          {
            title: '类别',
            width: 80,
            render: (_, r) => <Tag color={categoryLabel(r.category).color}>{categoryLabel(r.category).label}</Tag>,
          },
          {
            title: '亲密度',
            width: 80,
            sorter: (a, b) => a.intimacy - b.intimacy,
            render: (_, r) => <span style={{ color: intimacyColor(r.intimacy), fontWeight: 600 }}>{r.intimacy}</span>,
          },
          { title: '状态', width: 80, render: (_, r) => <Tag color={statusLabel(r.status).color}>{statusLabel(r.status).label}</Tag> },
          {
            title: '描述',
            render: (_, r) => (
              <Typography.Paragraph style={{ marginBottom: 0 }} ellipsis={{ rows: 2, expandable: true, symbol: '展开' }}>
                {r.description || '-'}
              </Typography.Paragraph>
            ),
          },
          {
            title: '',
            width: 150,
            render: (_, r) => (
              <Space size={0}>
                <Button
                  type="link"
                  size="small"
                  onClick={() => {
                    setEditing(r)
                    setEditOpen(true)
                  }}
                >
                  {canEdit ? '编辑' : '查看'}</Button>
                <Button type="link" size="small" onClick={() => setHistory(r)}>
                  历史
                </Button>
                <Popconfirm title="删除整份关系资料？" description="将同时删除全部剧情变化和操作记录，无法恢复。" okText="删除" cancelText="取消" onConfirm={() => handleDelete(r)}>
                  <Button type="link" size="small" danger disabled={!canEdit}>
                    删除
                  </Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
    </Space>
  )

  return (
    <Card
      title="人物关系"
      styles={{ header: { flexWrap: 'wrap', gap: 12, paddingBlock: 12 }, extra: { marginInlineStart: 0, maxWidth: '100%' } }}
      extra={
        <Space wrap>
          {canUseAI && (<Button icon={<RobotOutlined />} disabled={characters.length < 2} title={characters.length < 2 ? '至少需要两名已有角色' : undefined} onClick={() => setGenerateOpen(true)}>
            AI 生成关系
          </Button>)}
          <Button onClick={() => setPairHistoryOpen(true)}>按人物查看历史</Button>
          <Button icon={<SettingOutlined />} onClick={() => setTypesOpen(true)}>
            关系类型
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            disabled={!canEdit || (characters.length < 2)}
            onClick={() => {
              setEditing(null)
              setEditOpen(true)
            }}
          >
            新增关系
          </Button>
        </Space>
      }
    >
      <Tabs
        activeKey={tab}
        onChange={setTab}
        destroyOnHidden
        items={[
          { key: 'list', label: `列表（${rels.length}）`, children: list },
          {
            key: 'graph',
            label: '图谱',
            children: <GraphTab projectId={pid} characters={characters} />,
          },
        ]}
      />
      <RelationEditModal
        projectId={pid}
        open={editOpen}
        characters={characters}
        types={types}
        editing={editing}
        onClose={() => setEditOpen(false)}
        onSaved={() => {
          setEditOpen(false)
          reload()
        }}
      />
      {canUseAI && (<EntityTaskModal
        projectId={pid}
        kind="relations.generate"
        open={generateOpen}
        characters={characters}
        relationships={rels}
        onClose={() => setGenerateOpen(false)}
      />)}
      {history && <RelationHistoryModal projectId={pid} relationship={history} onClose={() => setHistory(null)} />}
      <Modal open={pairHistoryOpen} title="按人物查看关系历史" footer={null} width={680} onCancel={() => { setPairHistoryOpen(false); setHistoryA(undefined); setHistoryB(undefined) }} destroyOnHidden>
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Typography.Text type="secondary">选择两位角色，可查看已结束关系的剧情历史。</Typography.Text>
          <Select aria-label="选择第一位角色" showSearch optionFilterProp="label" placeholder="第一位角色" value={historyA} style={{ width: '100%' }}
            options={characters.filter((c) => c.id !== historyB).map((c) => ({ value: c.id, label: c.name }))} onChange={setHistoryA} />
          <Select aria-label="选择第二位角色" showSearch optionFilterProp="label" placeholder="第二位角色" value={historyB} style={{ width: '100%' }}
            options={characters.filter((c) => c.id !== historyA).map((c) => ({ value: c.id, label: c.name }))} onChange={setHistoryB} />
          {historyA && historyB && <RelationPairHistory key={`${pid}:${historyA}:${historyB}`} projectId={pid} pair={[historyA, historyB]} />}
        </Space>
      </Modal>
      <RelationTypeManager
        projectId={pid}
        open={typesOpen}
        types={types}
        onClose={() => setTypesOpen(false)}
        onChanged={reloadTypes}
      />
    </Card>
  )
}

interface GraphTabProps {
  projectId: number
  characters: Character[]
}

/** Tabs 设置了 destroyOnHidden：每次切到「图谱」都重新挂载并拉取最新数据 */
function GraphTab({ projectId, characters }: GraphTabProps) {
  const loader = useCallback(() => relationshipApi.graph(projectId), [projectId])
  const { data: graph, loading, error } = useLoader(loader, { projectId, resources: ['relationships', 'characters', 'organizations'] })
  const [filter, setFilter] = useState<GraphFilter>(DEFAULT_GRAPH_FILTER)
  const [cardId, setCardId] = useState<number | null>(null)
  const view = useMemo(() => (graph ? filterGraph(graph, filter) : { nodes: [], edges: [] }), [graph, filter])
  const card = characters.find((c) => c.id === cardId) ?? null

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Space wrap>
        <Select
          allowClear
          placeholder="按组织筛选（含下级）"
          style={{ width: 200 }}
          value={filter.organizationId}
          onChange={(v: number | undefined) => setFilter((f) => ({ ...f, organizationId: v ?? null }))}
          options={(graph?.organizations ?? []).map((o) => ({ value: o.id, label: o.name }))}
        />
        <Select
          mode="multiple"
          allowClear
          placeholder="关系类别"
          style={{ minWidth: 200 }}
          value={filter.categories}
          onChange={(v: string[]) => setFilter((f) => ({ ...f, categories: v }))}
          options={CATEGORY_OPTIONS}
        />
        <Checkbox checked={filter.hideIsolated} onChange={(e) => setFilter((f) => ({ ...f, hideIsolated: e.target.checked }))}>
          隐藏无关系的角色
        </Checkbox>
        <Typography.Text type="secondary">
          {view.nodes.length} 个角色 · {view.edges.length} 条关系；点击角色查看角色卡
        </Typography.Text>
      </Space>
      {error && <Typography.Text type="danger">{error}</Typography.Text>}
      <Spin spinning={loading}>
        {graph && view.nodes.length === 0 ? (
          <Empty description="没有符合条件的角色" />
        ) : (
          <Suspense
            fallback={
              <div style={{ height: 600, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                <Spin tip="正在加载图谱…" />
              </div>
            }
          >
            <RelationGraph nodes={view.nodes} edges={view.edges} onNodeClick={setCardId} />
          </Suspense>
        )}
      </Spin>
      <Modal open={!!card} title={card?.name} footer={null} onCancel={() => setCardId(null)} width={560}>
        {card && <CharacterCard c={card} />}
      </Modal>
    </Space>
  )
}

function CharacterCard({ c }: { c: Character }) {
  const life = lifeStatusLabel(c.life_status)
  const rows: [string, string][] = [
    ['别名', c.aliases.join('、')],
    ['年龄 / 性别', [c.age, c.gender].filter(Boolean).join(' / ')],
    ['外貌', c.appearance],
    ['性格', c.personality],
    ['背景', c.background],
    ['简介', c.profile],
    ['当前状态', c.current_state],
  ]
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Space>
        {c.role_type && <Tag color={roleTypeColor(c.role_type)}>{c.role_type}</Tag>}
        <Tag color={life.color}>{life.label}</Tag>
      </Space>
      <Descriptions size="small" column={1}>
        {rows
          .filter(([, v]) => !!v)
          .map(([label, v]) => (
            <Descriptions.Item key={label} label={label}>
              <span style={{ whiteSpace: 'pre-wrap' }}>{v}</span>
            </Descriptions.Item>
          ))}
      </Descriptions>
    </Space>
  )
}
