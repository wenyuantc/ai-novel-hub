import { PlusOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Empty, Form, Input, List, Modal, Popconfirm, Select, Space, Spin, Tag, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { errorMessage } from '../api/client'
import { projectApi } from '../api/projects'
import { seriesApi, type Series, type SeriesBook } from '../api/series'
import { StudioPage } from '../components/StudioUI'
import SeriesLibrary from '../components/series/SeriesLibrary'
import SeriesTransitionDialog from '../components/series/SeriesTransitionDialog'
import { seriesNeedsReview, seriesSettingResources } from '../components/series/seriesState'
import { useSeriesAction, useSeriesIdentity } from '../components/series/useSeriesAction'
import { useLoader } from '../hooks/useLoader'
import { invalidateProject } from '../utils/projectRefresh'

export default function SeriesPage() {
  const { sid } = useParams()
  const identity = useSeriesIdentity()
  if (sid === undefined) return <SeriesList key={identity} />
  const id = Number(sid)
  if (!/^\d+$/.test(sid) || !Number.isSafeInteger(id) || id <= 0) {
    return <StudioPage title="系列不存在" action={<Link to="/series">返回系列</Link>}><Empty /></StudioPage>
  }
  return <SeriesDetails key={`${identity}:${id}`} id={id} />
}

function SeriesList() {
  const load = useCallback(() => seriesApi.list(), [])
  const list = useLoader(load)
  const [creating, setCreating] = useState(false)
  const navigate = useNavigate()
  return <StudioPage title="作品系列" hint="为同一世界的多部作品维护一份共享设定。"
    action={<Space wrap><Link to="/projects">返回作品书库</Link><Button type="primary" icon={<PlusOutlined />} onClick={() => setCreating(true)}>新建系列</Button></Space>}>
    <Card>
      {list.error && <Alert type="error" showIcon message={list.error} action={<Button size="small" onClick={list.reload}>重试</Button>} />}
      <Spin spinning={list.loading}>
        <List dataSource={list.data ?? []} rowKey="id" locale={{ emptyText: list.loading || list.error ? null
          : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无系列。新建系列后，可从书库加入已有作品。" /> }}
          renderItem={(series) => <List.Item>
            <List.Item.Meta title={<Link to={`/series/${series.id}`}>{series.name}</Link>}
              description={`${series.book_count} 部成员作品 · ${series.entry_count} 条共享设定`} />
          </List.Item>} />
      </Spin>
    </Card>
    {creating && <NameDialog onClose={() => setCreating(false)} onSaved={(series) => navigate(`/series/${series.id}`)} onRefresh={list.reload} />}
  </StudioPage>
}

function SeriesDetails({ id }: { id: number }) {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const [search, setSearch] = useSearchParams()
  const entryParam = search.get('entry') ?? ''
  const focusedEntryId = /^\d+$/.test(entryParam) && Number.isSafeInteger(Number(entryParam)) && Number(entryParam) > 0 ? Number(entryParam) : undefined
  const clearFocus = () => { setSearch((prev) => { const next = new URLSearchParams(prev); next.delete('entry'); return next }, { replace: true }) }
  const load = useCallback(() => seriesApi.get(id), [id])
  const detail = useLoader(load)
  const [dialog, setDialog] = useState<{ kind: 'rename' | 'join' } | { kind: 'leave'; book: SeriesBook } | null>(null)
  const removal = useSeriesAction()
  const series = detail.data
  const close = () => setDialog(null)
  const done = (text: string) => { detail.reload(); close(); message.success(text) }
  const canChange = !!series && !detail.loading && !detail.error && !removal.busy
  return <StudioPage title={series?.name ?? '作品系列'}
    hint="共享基础统一维护，成员作品分别记录自己的剧情变化。"
    action={<Space wrap><Link to="/series">返回系列</Link><Link to="/projects">作品书库</Link></Space>}>
    <Space direction="vertical" size="middle" style={{ width: '100%', minWidth: 0 }}>
      {detail.error && <Alert type="error" showIcon message={detail.error} action={<Button size="small" onClick={detail.reload}>重试</Button>} />}
      {!!removal.error && <Alert type="error" showIcon message={errorMessage(removal.error)}
        action={<Button size="small" onClick={() => { removal.clearError(); detail.reload() }}>重新读取</Button>} />}
      {!series && !detail.error && <Spin aria-label="正在读取系列" />}
      {series && !detail.error && <>
        <Card title="成员作品" extra={<Space wrap>
          <Button disabled={!canChange} onClick={() => setDialog({ kind: 'rename' })}>重命名</Button>
          <Button type="primary" icon={<PlusOutlined />} disabled={!canChange} onClick={() => setDialog({ kind: 'join' })}>加入作品</Button>
          {series.book_count === 0 && series.entry_count === 0 && <Popconfirm title="删除空系列？"
            okText="删除" cancelText="取消" okButtonProps={{ danger: true }}
            onConfirm={() => removal.run(() => seriesApi.remove(id, series.revision), () => navigate('/series'))}>
            <Button danger disabled={!canChange || !!removal.error}>删除空系列</Button>
          </Popconfirm>}
        </Space>}>
          <List dataSource={series.books} rowKey="id" locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未加入作品" /> }}
            renderItem={(book) => <List.Item actions={book.recycled ? [] : [
              <Button key="leave" type="link" size="small" danger disabled={!canChange} onClick={() => setDialog({ kind: 'leave', book })}>退出系列</Button>,
            ]}>
              <Space wrap>
                {book.recycled ? <Typography.Text>{book.title}</Typography.Text> : <Link to={`/projects/${book.id}/overview`}>{book.title}</Link>}
                {book.recycled && <Tag>回收站 · 保留引用</Tag>}
              </Space>
            </List.Item>} />
        </Card>
        <SeriesLibrary seriesId={id} seriesRevision={series.revision} books={series.books} focusedEntryId={focusedEntryId}
          onClearFocus={clearFocus} onChange={detail.reload} />
      </>}
    </Space>
    {series && dialog?.kind === 'rename' && <NameDialog series={series} onClose={close} onRefresh={detail.reload} onSaved={() => done('系列名称已保存')} />}
    {series && dialog?.kind === 'join' && <JoinDialog series={series} onClose={close} onRefresh={detail.reload}
      onJoined={(pid) => { invalidateProject(pid, ['project']); done('作品已加入系列') }} />}
    {series && dialog?.kind === 'leave' && <LeaveDialog seriesId={id} book={dialog.book} onClose={close}
      onDone={() => { invalidateProject(dialog.book.id, ['project', ...new Set([...seriesSettingResources('character'), ...seriesSettingResources('world')])]); done('作品已退出系列') }} />}
  </StudioPage>
}

function LeaveDialog({ seriesId, book, onClose, onDone }: { seriesId: number; book: SeriesBook; onClose: () => void; onDone: () => void }) {
  const [request] = useState(() => ({ action: 'leave' as const, project_id: book.id }))
  return <SeriesTransitionDialog seriesId={seriesId} request={request} label={book.title} onClose={onClose} onDone={onDone} />
}

function NameDialog({ series, onClose, onSaved, onRefresh }: {
  series?: Series; onClose: () => void; onSaved: (series: Series) => void; onRefresh: () => void
}) {
  const [form] = Form.useForm<{ name: string }>()
  const action = useSeriesAction()
  const [baseRevision, setRevision] = useState(series?.revision ?? 0)
  const [review, setReview] = useState(false)
  const [latest, setLatest] = useState<Series | null>(null)
  const blocked = review || (action.error !== null && seriesNeedsReview(action.error))
  const save = () => {
    if (blocked) return
    void action.run(async (current) => {
      const values = await form.validateFields().catch(() => null)
      if (!values || !current()) return null
      const name = values.name.trim()
      return series ? seriesApi.rename(series.id, baseRevision, name) : seriesApi.create(name)
    }, (result) => { if (result) onSaved(result) })
  }
  return <Modal open title={series ? '重命名系列' : '新建系列'} onCancel={onClose} onOk={save} okText="保存" cancelText="取消"
    confirmLoading={action.busy} okButtonProps={{ disabled: blocked }} maskClosable={false} closable={!action.busy}
    keyboard={!action.busy} cancelButtonProps={{ disabled: action.busy }}>
    {!!action.error && <Alert type="error" showIcon message={errorMessage(action.error)} description="当前输入已保留。" />}
    {blocked && <Alert type="warning" showIcon message="保存结果或版本需要核对" action={series
      ? <Button size="small" loading={action.busy} onClick={() => { setReview(true); void action.run(() => seriesApi.get(series.id), setLatest) }}>读取最新名称</Button>
      : <Button size="small" onClick={onRefresh}>刷新系列列表</Button>} />}
    {latest && <Space wrap style={{ marginBlock: 8 }}>
      <Typography.Text>当前保存的名称：{latest.name}</Typography.Text>
      <Button onClick={() => { setRevision(latest.revision); setLatest(null); setReview(false); action.clearError() }}>保留输入并采用最新版本</Button>
    </Space>}
    <Form form={form} layout="vertical" initialValues={{ name: series?.name ?? '' }} disabled={action.busy}>
      <Form.Item name="name" label="系列名称" rules={[{ required: true, whitespace: true, max: 200, message: '请输入不超过 200 字的名称' }]}>
        <Input autoFocus />
      </Form.Item>
    </Form>
  </Modal>
}

function JoinDialog({ series, onClose, onJoined, onRefresh }: {
  series: Series; onClose: () => void; onJoined: (pid: number) => void; onRefresh: () => void
}) {
  const load = useCallback(() => projectApi.list(), [])
  const projects = useLoader(load)
  const action = useSeriesAction()
  const [projectId, setProjectId] = useState<number>()
  const options = (projects.data ?? []).filter((project) => !project.series).map((project) => ({ value: project.id, label: project.title }))
  const stale = action.error !== null && seriesNeedsReview(action.error)
  const valid = projectId !== undefined && options.some((item) => item.value === projectId)
  return <Modal open title="加入已有作品" onCancel={onClose}
    onOk={() => { if (valid && !stale) void action.run(() => seriesApi.join(series.id, projectId, series.revision), () => onJoined(projectId)) }}
    okText="加入系列" cancelText="取消" confirmLoading={action.busy} okButtonProps={{ disabled: !valid || stale || projects.loading }}
    maskClosable={!action.busy} closable={!action.busy} keyboard={!action.busy} cancelButtonProps={{ disabled: action.busy }}>
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Typography.Text>作品一次只能加入一个系列。加入不会自动将共享条目用于该书。</Typography.Text>
      {projects.error && <Alert type="error" showIcon message={projects.error} action={<Button onClick={projects.reload}>重试</Button>} />}
      {!!action.error && <Alert type="error" showIcon message={errorMessage(action.error)} action={stale
        ? <Button size="small" onClick={() => { onClose(); onRefresh() }}>重新读取系列</Button> : undefined} />}
      <Select<number> aria-label="选择未加入系列的作品" placeholder="选择已有作品" style={{ width: '100%' }}
        showSearch optionFilterProp="label" value={projectId} onChange={setProjectId} options={options}
        notFoundContent={projects.loading ? '正在加载…' : '没有可加入的作品'} disabled={action.busy} />
    </Space>
  </Modal>
}
