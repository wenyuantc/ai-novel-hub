import { useAuthorizationCleanup } from '../../hooks/useAuthorizationCleanup'
import { StarFilled, StarOutlined } from '@ant-design/icons'
import { App, Button, Drawer, Modal, Popconfirm, Space, Table, Tabs, Tag, theme, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useCallback, useRef, useState } from 'react'
import { versionApi, VERSION_SOURCE } from '../../api/chapterVersions'
import { errorMessage } from '../../api/client'
import type { ChapterVersion, ChapterVersionBrief, SceneSnapshot } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { formatTime } from '../../utils/time'
import TextDiff from '../TextDiff'
import GenerationLogList from './GenerationLogList'
import SceneVersionActions, { type VersionSceneContext } from '../scenes/SceneVersionActions'

interface Props {
  pid: number
  chapterId: number
  open: boolean
  onClose: () => void
  /** 编辑器当前正文（可能含未保存修改），用于「与当前对比」 */
  currentContent: string
  currentDirty: boolean
  /** 生成中 / 保存中 / 局部改写预览中等情况下禁止回滚 */
  restoreDisabled: boolean
  /** 由编辑器执行回滚（含未保存修改时先自动保存），成功返回 true */
  onRestore: (version: ChapterVersionBrief, revision?: number, confirmRichDocumentLoss?: boolean) => Promise<boolean>
  sceneContext?: VersionSceneContext
}

/** 章节版本历史抽屉：版本列表（查看 / 对比 / 恢复 / 标星 / 备注 / 删除）+ 生成记录。 */
export default function VersionDrawer({ open, onClose, ...rest }: Props) {
  const { canUseAI } = useProjectAccess(rest.pid)
  return (
    <Drawer title="版本历史" width={820} open={open} onClose={onClose} destroyOnHidden>
      <Tabs
        items={[
          { key: 'versions', label: '版本', children: <VersionList {...rest} /> },
          ...(canUseAI ? [{
            key: 'logs',
            label: '生成记录',
            children: <GenerationLogList pid={rest.pid} chapterId={rest.chapterId} />,
          }] : []),
        ]}
      />
    </Drawer>
  )
}

interface DiffView {
  title: string
  a: string
  b: string
}

interface ContentView {
  version: ChapterVersionBrief
  content: string
  snapshot: SceneSnapshot | null
}

function VersionList({
  pid,
  chapterId,
  currentContent,
  currentDirty,
  restoreDisabled,
  onRestore,
  sceneContext,
}: Omit<Props, 'open' | 'onClose'>) {
  const { token } = theme.useToken()
  const { message } = App.useApp()
  const { canEdit, canEditNow, canReadNow } = useProjectAccess(pid)
  const loader = useCallback(() => versionApi.list(pid, chapterId), [pid, chapterId])
  const { data: items = [], loading, reload, setData } = useLoader(loader)
  const [selected, setSelected] = useState<number[]>([])
  const [diff, setDiff] = useState<DiffView | null>(null)
  const [view, setView] = useState<ContentView | null>(null)
  const [pending, setPending] = useState(false)
  useAuthorizationCleanup(() => setPending(false))
  // 版本正文缓存；updated_at 变化（手动保存合并）时失效
  const cache = useRef(new Map<number, { stamp: string; full: ChapterVersion }>())
  const latestId = items[0]?.id

  const fetchVersion = async (v: ChapterVersionBrief): Promise<ChapterVersion> => {
    const hit = cache.current.get(v.id)
    if (hit && hit.stamp === v.updated_at) return hit.full
    const full = await versionApi.get(pid, chapterId, v.id)
    if (canReadNow()) cache.current.set(v.id, { stamp: v.updated_at, full })
    return full
  }
  const fetchContent = async (v: ChapterVersionBrief): Promise<string> => (await fetchVersion(v)).content

  const withPending = async (fn: () => Promise<void>, write = false) => {
    if (!(write ? canEditNow() : canReadNow())) return
    setPending(true)
    try {
      await fn()
    } catch (err) {
      if (write ? canEditNow() : canReadNow()) message.error(errorMessage(err))
    } finally {
      if (write ? canEditNow() : canReadNow()) setPending(false)
    }
  }

  const replaceItem = (updated: ChapterVersionBrief) =>
    setData((prev) => (prev ?? []).map((x) => (x.id === updated.id ? updated : x)))

  const toggleStar = (v: ChapterVersionBrief) =>
    withPending(async () => {
      const updated = await versionApi.update(pid, chapterId, v.id, { starred: !v.starred })
      if (canEditNow()) replaceItem(updated)
    }, true)

  const saveNote = (v: ChapterVersionBrief, note: string) => {
    const trimmed = note.trim()
    if (trimmed === v.note) return
    void withPending(async () => {
      const updated = await versionApi.update(pid, chapterId, v.id, { note: trimmed })
      if (canEditNow()) replaceItem(updated)
    }, true)
  }

  const remove = (v: ChapterVersionBrief) =>
    withPending(async () => {
      await versionApi.remove(pid, chapterId, v.id)
      if (!canEditNow()) return
      setSelected((s) => s.filter((id) => id !== v.id))
      message.success(`已删除 v${v.seq}`)
      reload()
    }, true)

  const showView = (v: ChapterVersionBrief) =>
    withPending(async () => {
      const full = await fetchVersion(v)
      if (canReadNow()) setView({ version: v, content: full.content, snapshot: full.scene_snapshot })
    })

  const compareWithCurrent = (v: ChapterVersionBrief) =>
    withPending(async () => {
      const a = await fetchContent(v)
      if (!canReadNow()) return
      setDiff({ title: `v${v.seq} → 当前正文${currentDirty ? '（含未保存修改）' : ''}`, a, b: currentContent })
    })

  const compareSelected = () =>
    withPending(async () => {
      const [x, y] = items.filter((v) => selected.includes(v.id)).sort((p, q) => p.seq - q.seq)
      if (!x || !y) return
      const [a, b] = await Promise.all([fetchContent(x), fetchContent(y)])
      if (!canReadNow()) return
      setDiff({ title: `v${x.seq} → v${y.seq}`, a, b })
    })

  const columns: ColumnsType<ChapterVersionBrief> = [
    {
      title: '版本',
      key: 'seq',
      width: 110,
      render: (_, v) => (
        <Space size={4}>
          <Tooltip title={v.starred ? '取消标星' : '标星（永久保留）'}>
            <Button
              type="text"
              size="small"
              aria-label={v.starred ? '取消标星' : '标星'}
              disabled={!canEdit}
              icon={v.starred ? <StarFilled style={{ color: token.colorWarning }} /> : <StarOutlined />}
              onClick={() => void toggleStar(v)}
            />
          </Tooltip>
          <span>v{v.seq}</span><Tag>{v.has_scene_snapshot ? '场景结构' : '旧纯文本'}</Tag>
          {v.id === latestId && <Tag color="green">最新</Tag>}
        </Space>
      ),
    },
    {
      title: '来源',
      dataIndex: 'source',
      width: 100,
      render: (s: ChapterVersionBrief['source']) => <Tag color={VERSION_SOURCE[s].color}>{VERSION_SOURCE[s].label}</Tag>,
    },
    { title: '时间', dataIndex: 'updated_at', width: 140, render: (t: string) => formatTime(t) },
    { title: '字数', dataIndex: 'word_count', width: 70 },
    {
      title: '备注',
      dataIndex: 'note',
      render: (note: string, v) => (
        <Typography.Text
          type={note ? undefined : 'secondary'}
          editable={canEdit ? { text: note, maxLength: 200, onChange: (val) => saveNote(v, val), tooltip: '编辑备注' } : false}
        >
          {note || '—'}
        </Typography.Text>
      ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 200,
      render: (_, v) => (
        <Space size={0} wrap>
          <Button type="link" size="small" onClick={() => void showView(v)}>
            查看
          </Button>
          <Button type="link" size="small" onClick={() => void compareWithCurrent(v)}>
            对比当前
          </Button>
          <SceneVersionActions projectId={pid} chapterId={chapterId} version={v} context={sceneContext} disabled={!canEdit || restoreDisabled} onRestore={onRestore} onRestored={() => { setView(null); reload() }} />
          {v.id === latestId ? (
            <Tooltip title="不能删除最新版本">
              <Button type="link" size="small" danger disabled>
                删除
              </Button>
            </Tooltip>
          ) : (
            <Popconfirm title={`删除 v${v.seq}？`} okText="删除" cancelText="取消" onConfirm={() => remove(v)}>
              <Button type="link" size="small" danger disabled={!canEdit}>
                删除
              </Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ]

  return (
    <>
      <Space style={{ marginBottom: 12 }} wrap>
        <Button disabled={selected.length !== 2} loading={pending && selected.length === 2} onClick={() => void compareSelected()}>
          对比所选（{selected.length}/2）
        </Button>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          每章保留最近 50 个未标星版本，标星版本永久保留；同结构的普通手写可在 10 分钟内合并；结构、清空与覆盖操作保留独立边界。
        </Typography.Text>
      </Space>
      <Table
        rowKey="id"
        size="small"
        loading={loading}
        dataSource={items}
        columns={columns}
        pagination={false}
        locale={{ emptyText: '暂无版本：保存正文或 AI 生成后会自动记录' }}
        rowSelection={{
          selectedRowKeys: selected,
          hideSelectAll: true,
          // 只保留最近勾选的两个
          onChange: (keys) => setSelected(keys.map(Number).slice(-2)),
        }}
      />
      <Modal open={diff !== null} title={diff?.title} width={900} footer={null} onCancel={() => setDiff(null)}>
        {diff && <TextDiff a={diff.a} b={diff.b} maxHeight={600} />}
      </Modal>
      <Modal
        open={view !== null}
        title={view ? `v${view.version.seq} · ${VERSION_SOURCE[view.version.source].label} · ${view.version.word_count} 字` : ''}
        width={860}
        onCancel={() => setView(null)}
        footer={
          view && (
            <Space>
              <Button onClick={() => void compareWithCurrent(view.version)}>与当前对比</Button>
              <SceneVersionActions projectId={pid} chapterId={chapterId} version={view.version} context={sceneContext} disabled={!canEdit || restoreDisabled} onRestore={onRestore} onRestored={() => { setView(null); reload() }} />
            </Space>
          )
        }
      >
        {view?.snapshot && <Space direction="vertical" style={{ width: '100%', marginBottom: 12 }}>{view.snapshot.scenes.map((scene, index) => <div key={scene.id}><Tag>场景 {index + 1} · #{scene.id}</Tag><strong>{scene.title || '未命名'}</strong><Typography.Paragraph style={{ margin: 0, whiteSpace: 'pre-wrap' }}>作者摘要：{scene.summary || '无'}</Typography.Paragraph><Typography.Text type="secondary">本地模式：{scene.planning ? Object.entries(scene.planning.raw.modes).map(([field, mode]) => `${field}=${mode === 'inherit' ? '继承' : '覆盖'}`).join(' · ') : '全部继承本章'}</Typography.Text></div>)}</Space>}
        {view && (
          <div
            style={{
              maxHeight: 600,
              overflow: 'auto',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              fontSize: 14,
              lineHeight: 1.8,
            }}
          >
            {view.content || <Typography.Text type="secondary">（空）</Typography.Text>}
          </div>
        )}
      </Modal>
    </>
  )
}
