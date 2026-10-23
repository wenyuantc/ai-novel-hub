import { StarFilled, StarOutlined } from '@ant-design/icons'
import { App, Button, Drawer, Modal, Popconfirm, Space, Table, Tabs, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useCallback, useRef, useState } from 'react'
import { versionApi, VERSION_SOURCE } from '../../api/chapterVersions'
import { errorMessage } from '../../api/client'
import type { ChapterVersionBrief } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { formatTime } from '../../utils/time'
import TextDiff from '../TextDiff'
import GenerationLogList from './GenerationLogList'

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
  onRestore: (version: ChapterVersionBrief) => Promise<boolean>
}

/** 章节版本历史抽屉：版本列表（查看 / 对比 / 恢复 / 标星 / 备注 / 删除）+ 生成记录。 */
export default function VersionDrawer({ open, onClose, ...rest }: Props) {
  return (
    <Drawer title="版本历史" width={820} open={open} onClose={onClose} destroyOnHidden>
      <Tabs
        items={[
          { key: 'versions', label: '版本', children: <VersionList {...rest} /> },
          {
            key: 'logs',
            label: '生成记录',
            children: <GenerationLogList pid={rest.pid} chapterId={rest.chapterId} />,
          },
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
}

function VersionList({
  pid,
  chapterId,
  currentContent,
  currentDirty,
  restoreDisabled,
  onRestore,
}: Omit<Props, 'open' | 'onClose'>) {
  const { message, modal } = App.useApp()
  const loader = useCallback(() => versionApi.list(pid, chapterId), [pid, chapterId])
  const { data: items = [], loading, reload, setData } = useLoader(loader)
  const [selected, setSelected] = useState<number[]>([])
  const [diff, setDiff] = useState<DiffView | null>(null)
  const [view, setView] = useState<ContentView | null>(null)
  const [pending, setPending] = useState(false)
  // 版本正文缓存；updated_at 变化（手动保存合并）时失效
  const cache = useRef(new Map<number, { stamp: string; content: string }>())
  const latestId = items[0]?.id

  const fetchContent = async (v: ChapterVersionBrief): Promise<string> => {
    const hit = cache.current.get(v.id)
    if (hit && hit.stamp === v.updated_at) return hit.content
    const full = await versionApi.get(pid, chapterId, v.id)
    cache.current.set(v.id, { stamp: v.updated_at, content: full.content })
    return full.content
  }

  const withPending = async (fn: () => Promise<void>) => {
    setPending(true)
    try {
      await fn()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setPending(false)
    }
  }

  const replaceItem = (updated: ChapterVersionBrief) =>
    setData((prev) => (prev ?? []).map((x) => (x.id === updated.id ? updated : x)))

  const toggleStar = (v: ChapterVersionBrief) =>
    withPending(async () => {
      replaceItem(await versionApi.update(pid, chapterId, v.id, { starred: !v.starred }))
    })

  const saveNote = (v: ChapterVersionBrief, note: string) => {
    const trimmed = note.trim()
    if (trimmed === v.note) return
    void withPending(async () => {
      replaceItem(await versionApi.update(pid, chapterId, v.id, { note: trimmed }))
    })
  }

  const remove = (v: ChapterVersionBrief) =>
    withPending(async () => {
      await versionApi.remove(pid, chapterId, v.id)
      setSelected((s) => s.filter((id) => id !== v.id))
      message.success(`已删除 v${v.seq}`)
      reload()
    })

  const showView = (v: ChapterVersionBrief) =>
    withPending(async () => {
      setView({ version: v, content: await fetchContent(v) })
    })

  const compareWithCurrent = (v: ChapterVersionBrief) =>
    withPending(async () => {
      const a = await fetchContent(v)
      setDiff({ title: `v${v.seq} → 当前正文${currentDirty ? '（含未保存修改）' : ''}`, a, b: currentContent })
    })

  const compareSelected = () =>
    withPending(async () => {
      const [x, y] = items.filter((v) => selected.includes(v.id)).sort((p, q) => p.seq - q.seq)
      if (!x || !y) return
      const [a, b] = await Promise.all([fetchContent(x), fetchContent(y)])
      setDiff({ title: `v${x.seq} → v${y.seq}`, a, b })
    })

  const confirmRestore = (v: ChapterVersionBrief) => {
    modal.confirm({
      title: `恢复到 v${v.seq}？`,
      content: `当前正文会先存为版本，恢复本身也会生成新版本，可随时撤销。${currentDirty ? '未保存的修改将先自动保存。' : ''}`,
      okText: '恢复',
      cancelText: '取消',
      onOk: async () => {
        if (await onRestore(v)) {
          setView(null)
          reload()
        }
      },
    })
  }

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
              icon={v.starred ? <StarFilled style={{ color: '#faad14' }} /> : <StarOutlined />}
              onClick={() => void toggleStar(v)}
            />
          </Tooltip>
          <span>v{v.seq}</span>
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
          editable={{ text: note, maxLength: 200, onChange: (val) => saveNote(v, val), tooltip: '编辑备注' }}
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
          <Button type="link" size="small" disabled={restoreDisabled} onClick={() => confirmRestore(v)}>
            恢复
          </Button>
          {v.id === latestId ? (
            <Tooltip title="不能删除最新版本">
              <Button type="link" size="small" danger disabled>
                删除
              </Button>
            </Tooltip>
          ) : (
            <Popconfirm title={`删除 v${v.seq}？`} okText="删除" cancelText="取消" onConfirm={() => remove(v)}>
              <Button type="link" size="small" danger>
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
          每章保留最近 50 个未标星版本，标星版本永久保留；10 分钟内的连续手动保存合并为一个版本。
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
              <Button type="primary" disabled={restoreDisabled} onClick={() => confirmRestore(view.version)}>
                恢复此版本
              </Button>
            </Space>
          )
        }
      >
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
