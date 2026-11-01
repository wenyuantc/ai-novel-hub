import { PlusOutlined, PushpinFilled, PushpinOutlined } from '@ant-design/icons'
import {
  Alert,
  App,
  Button,
  Card,
  Input,
  InputNumber,
  Popconfirm,
  Rate,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  theme,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useCallback, useMemo, useState } from 'react'
import { errorMessage } from '../../api/client'
import { kbApi } from '../../api/kb'
import { memoryApi } from '../../api/memories'
import { characterApi } from '../../api/story'
import type { KbSettings, Memory, MemoryKind, MemoryListQuery, MemoryOrigin } from '../../api/types'
import IndexStatusBanner from '../../components/kb/IndexStatusBanner'
import MemoryEditModal from '../../components/kb/MemoryEditModal'
import MemorySearchDebug from '../../components/kb/MemorySearchDebug'
import RetrievalSettingsCard from '../../components/kb/RetrievalSettingsCard'
import { useIndexStatus } from '../../hooks/useIndexStatus'
import { useLoader } from '../../hooks/useLoader'
import { useProjectId } from '../../hooks/useProjectId'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import {
  MEMORY_KIND_COLORS,
  MEMORY_KIND_LABELS,
  MEMORY_ORIGIN_LABELS,
  memoryEditable,
  normalizeChapterRange,
} from '../../utils/kb'
import { splitPane, splitRow } from '../../utils/layout'

const PAGE_SIZE = 50
type TriState = 'all' | 'yes' | 'no'

function triBool(v: TriState): boolean | undefined {
  return v === 'all' ? undefined : v === 'yes'
}

/** 剧情记忆：筛选（章号范围 / 类型 / 来源 / 钉住 / 屏蔽 / 关键字）、行内钉住与屏蔽、手动记忆增删改、召回设置、检索调试、重建横幅。 */
export default function MemoriesPanel() {
  const { token } = theme.useToken()
  const pid = useProjectId()
  const { canUseAI, canEdit, canEditNow } = useProjectAccess(pid)
  const { message } = App.useApp()

  const [chapterFrom, setChapterFrom] = useState<number | null>(null)
  const [chapterTo, setChapterTo] = useState<number | null>(null)
  const [kind, setKind] = useState<MemoryKind | undefined>()
  const [origin, setOrigin] = useState<MemoryOrigin | undefined>()
  const [pinned, setPinned] = useState<TriState>('all')
  const [muted, setMuted] = useState<TriState>('all')
  const [q, setQ] = useState('')
  const [page, setPage] = useState(1)

  const [editing, setEditing] = useState<Memory | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [acting, setActing] = useState<number | null>(null)

  const query = useMemo<MemoryListQuery>(() => {
    const [from, to] = normalizeChapterRange(chapterFrom, chapterTo)
    return {
      chapter_from: from,
      chapter_to: to,
      kind,
      origin,
      pinned: triBool(pinned),
      muted: triBool(muted),
      q,
      page,
      page_size: PAGE_SIZE,
    }
  }, [chapterFrom, chapterTo, kind, origin, pinned, muted, q, page])

  const loader = useCallback(() => memoryApi.list(pid, query), [pid, query])
  const { data, loading, error, reload, setData } = useLoader(loader, { projectId: pid, resources: ['memories', 'characters'] })
  const settingsLoader = useCallback(() => kbApi.getSettings(pid), [pid])
  const { data: settings, setData: setSettings } = useLoader(settingsLoader, { projectId: pid, resources: ['memories', 'characters'] })
  const charLoader = useCallback(() => characterApi.list(pid), [pid])
  const { data: characters } = useLoader(charLoader, { projectId: pid, resources: ['memories', 'characters'] })
  const characterNames = useMemo(() => (characters ?? []).map((c) => c.name), [characters])

  const { status, refresh: refreshStatus } = useIndexStatus(pid, { enabled: canUseAI })

  /** 改筛选条件时回到第 1 页 */
  const filter = <T,>(set: (v: T) => void) => (v: T) => {
    set(v)
    setPage(1)
  }

  const patch = async (m: Memory, body: { pinned?: boolean; muted?: boolean }) => {
    if (!canEditNow()) return
    setActing(m.id)
    try {
      if (!canEditNow()) return
      const updated = await memoryApi.update(pid, m.id, body)
      if (!canEditNow()) return
      setData((prev) => {
        const base = prev ?? { items: [], total: 0, page: 1, page_size: PAGE_SIZE }
        return { ...base, items: base.items.map((x) => (x.id === updated.id ? updated : x)) }
      })
      refreshStatus()
    } catch (err) {
      if (!canEditNow()) return
      message.error(errorMessage(err))
    } finally {
      setActing(null)
    }
  }

  const remove = async (m: Memory) => {
    if (!canEditNow()) return
    setActing(m.id)
    try {
      if (!canEditNow()) return
      await memoryApi.remove(pid, m.id)
      if (!canEditNow()) return
      message.success('已删除')
      reload()
    } catch (err) {
      if (!canEditNow()) return
      message.error(errorMessage(err))
    } finally {
      setActing(null)
    }
  }

  const columns: ColumnsType<Memory> = [
    {
      title: '章',
      dataIndex: 'chapter_no',
      width: 80,
      render: (n: number) => `第 ${n} 章`,
    },
    {
      title: '类型',
      key: 'kind',
      width: 110,
      render: (_, m) => (
        <Space direction="vertical" size={2}>
          <Tag color={MEMORY_KIND_COLORS[m.kind]} style={{ marginInlineEnd: 0 }}>
            {MEMORY_KIND_LABELS[m.kind]}
          </Tag>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {MEMORY_ORIGIN_LABELS[m.origin]}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: '内容',
      key: 'content',
      render: (_, m) => (
        <Space direction="vertical" size={4} style={{ width: '100%' }}>
          <Typography.Paragraph
            type={m.muted ? 'secondary' : undefined}
            delete={m.muted}
            style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}
            ellipsis={{ rows: 3, expandable: true, symbol: '展开' }}
          >
            {m.content}
          </Typography.Paragraph>
          {m.characters.length > 0 && (
            <Space size={4} wrap>
              {m.characters.map((c) => (
                <Tag key={c} bordered={false} style={{ marginInlineEnd: 0 }}>
                  {c}
                </Tag>
              ))}
            </Space>
          )}
        </Space>
      ),
    },
    {
      title: '重要度',
      dataIndex: 'importance',
      width: 120,
      render: (v: number) => <Rate disabled count={5} value={v} style={{ fontSize: 12 }} />,
    },
    {
      title: '钉住',
      key: 'pinned',
      width: 70,
      render: (_, m) => (
        <Tooltip title={m.pinned ? '取消钉住' : '钉住：生成时优先注入'}>
          <Button
            type="text"
            size="small"
            aria-label={m.pinned ? '取消钉住' : '钉住'}
            loading={acting === m.id}
            icon={m.pinned ? <PushpinFilled style={{ color: token.colorWarning }} /> : <PushpinOutlined />}
            onClick={() => void patch(m, { pinned: !m.pinned })} disabled={!canEdit}
          />
        </Tooltip>
      ),
    },
    {
      title: '屏蔽',
      key: 'muted',
      width: 70,
      render: (_, m) => (
        <Tooltip title="屏蔽后不会被召回">
          <Switch
            size="small"
            checked={m.muted}
            disabled={!canEdit || (acting === m.id)}
            onChange={(v) => void patch(m, { muted: v })}
          />
        </Tooltip>
      ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 120,
      render: (_, m) =>
        memoryEditable(m) ? (
          <Space size={0}>
            <Button
              type="link"
              size="small"
              onClick={() => {
                setEditing(m)
                setModalOpen(true)
              }}
            >
              {canEdit ? '编辑' : '查看'}</Button>
            <Popconfirm
              title="删除该手动记忆？"
              okText="删除"
              cancelText="取消"
              okButtonProps={{ danger: true }}
              onConfirm={() => remove(m)}
            >
              <Button type="link" size="small" danger disabled={!canEdit}>
                删除
              </Button>
            </Popconfirm>
          </Space>
        ) : (
          <Tooltip title="自动记忆来自章节分析，重新分析时会更新；只能钉住或屏蔽">
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              自动生成
            </Typography.Text>
          </Tooltip>
        ),
    },
  ]

  return (
    <>
      {canUseAI && <IndexStatusBanner projectId={pid} status={status} scope="memory" onChanged={() => refreshStatus()} />}
      <div style={splitRow}>
        <div style={splitPane(600)}>
          <Card
            size="small"
            title={`剧情记忆（${data?.total ?? 0}）`}
            extra={
              <Button
                type="primary"
                size="small"
                icon={<PlusOutlined />}
                onClick={() => {
                  setEditing(null)
                  setModalOpen(true)
                }} disabled={!canEdit}
              >
                新增手动记忆
              </Button>
            }
          >
            <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
              章节分析完成后自动从摘要、重要情节、钩子、人物变化与伏笔生成记忆；重新分析会替换该章的自动记忆，钉住与屏蔽标记按内容保留。
            </Typography.Paragraph>
            <Space wrap style={{ marginBottom: 12 }}>
              <InputNumber
                min={0}
                precision={0}
                value={chapterFrom}
                onChange={filter(setChapterFrom)}
                placeholder="起始章"
                addonBefore="第"
                style={{ width: 130 }}
              />
              <InputNumber
                min={0}
                precision={0}
                value={chapterTo}
                onChange={filter(setChapterTo)}
                placeholder="结束章"
                addonBefore="至"
                addonAfter="章"
                style={{ width: 160 }}
              />
              <Select<MemoryKind>
                allowClear
                placeholder="类型"
                value={kind}
                onChange={filter(setKind)}
                style={{ width: 100 }}
                options={(Object.keys(MEMORY_KIND_LABELS) as MemoryKind[]).map((k) => ({
                  value: k,
                  label: MEMORY_KIND_LABELS[k],
                }))}
              />
              <Select<MemoryOrigin>
                allowClear
                placeholder="来源"
                value={origin}
                onChange={filter(setOrigin)}
                style={{ width: 90 }}
                options={(Object.keys(MEMORY_ORIGIN_LABELS) as MemoryOrigin[]).map((k) => ({
                  value: k,
                  label: MEMORY_ORIGIN_LABELS[k],
                }))}
              />
              <Select<TriState>
                value={pinned}
                onChange={filter(setPinned)}
                style={{ width: 110 }}
                options={[
                  { value: 'all', label: '钉住：全部' },
                  { value: 'yes', label: '仅钉住' },
                  { value: 'no', label: '未钉住' },
                ]}
              />
              <Select<TriState>
                value={muted}
                onChange={filter(setMuted)}
                style={{ width: 110 }}
                options={[
                  { value: 'all', label: '屏蔽：全部' },
                  { value: 'yes', label: '仅屏蔽' },
                  { value: 'no', label: '未屏蔽' },
                ]}
              />
              <Input.Search
                allowClear
                placeholder="搜索内容"
                maxLength={200}
                onSearch={filter((v: string) => setQ(v.trim()))}
                style={{ width: 180 }}
              />
            </Space>
            {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
            <Table<Memory>
              rowKey="id"
              size="small"
              loading={loading}
              dataSource={data?.items ?? []}
              columns={columns}
              scroll={{ x: 760 }}
              locale={{ emptyText: '没有记忆。分析章节后会自动生成，也可以手动添加' }}
              pagination={{
                current: page,
                pageSize: PAGE_SIZE,
                total: data?.total ?? 0,
                showSizeChanger: false,
                onChange: setPage,
                hideOnSinglePage: true,
              }}
            />
          </Card>
        </div>
        <div style={splitPane(300)}>
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <RetrievalSettingsCard
              projectId={pid}
              settings={settings}
              scope="memory"
              onSaved={(s: KbSettings) => setSettings(() => s)}
            />
            {canUseAI && <MemorySearchDebug projectId={pid} />}
          </Space>
        </div>
      </div>
      <MemoryEditModal
        projectId={pid}
        open={modalOpen}
        memory={editing}
        characterNames={characterNames}
        onClose={() => setModalOpen(false)}
        onSaved={() => {
          setModalOpen(false)
          reload()
          refreshStatus()
        }}
      />
    </>
  )
}
