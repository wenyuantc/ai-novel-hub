import { PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import { Alert, Badge, Button, Col, Empty, Input, List, Pagination, Row, Select, Space, Spin, Tabs, Tag, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { workshopApi } from '../api/workshop'
import type { SubmissionQuery, WorkshopQuery } from '../api/workshop'
import type { WorkshopItem, WorkshopSubmissionStatus, WorkshopSubmit } from '../api/types'
import ReviewPanel from '../components/workshop/ReviewPanel'
import ShareToWorkshopModal from '../components/workshop/ShareToWorkshopModal'
import WorkshopItemCard from '../components/workshop/WorkshopItemCard'
import WorkshopItemDrawer from '../components/workshop/WorkshopItemDrawer'
import { useIsMobile } from '../hooks/useIsMobile'
import { useLoader } from '../hooks/useLoader'
import { useAuthStore } from '../stores/auth'
import { SUBMISSION_STATUS, emptyWorkshopShare, updateShare, workshopCategoryOptions, workshopSubmissionKindLabel, workshopTab, WORKSHOP_KINDS } from '../utils/workshop'
import { formatTime } from '../utils/time'

/** 实例内共享：浏览、本人条目与提交记录、管理员审核。 */
export default function WorkshopPage() {
  const isAdmin = useAuthStore((s) => s.user?.is_admin ?? false)
  const isMobile = useIsMobile()
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = workshopTab(searchParams.get('tab'), isAdmin)
  const setTab = (value: string) => setSearchParams((previous) => {
    const next = new URLSearchParams(previous)
    if (value === 'browse') next.delete('tab')
    else next.set('tab', workshopTab(value, isAdmin))
    return next
  })
  const [version, setVersion] = useState(0)
  const [itemId, setItemId] = useState<number | null>(null)
  const [submission, setSubmission] = useState<{ id: number; review: boolean } | null>(null)
  const [share, setShare] = useState<{ initial: WorkshopSubmit; direct: boolean } | null>(null)
  const refresh = () => setVersion((v) => v + 1)
  const pendingLoader = useCallback(() => {
    void version // 变更成功后刷新数量，同时保留当前筛选与分页。
    return isAdmin ? workshopApi.pending({ status: 'pending', page: 1, page_size: 1 }) : Promise.resolve(null)
  }, [isAdmin, version])
  const pending = useLoader(pendingLoader)
  const openItem = (item: WorkshopItem) => setItemId(item.id)
  const openSubmission = (id: number, review: boolean) => setSubmission({ id, review })
  const reviewTab = { key: 'review', label: <Space>审核<Badge count={pending.data?.total ?? 0} showZero /></Space>, children: <Submissions version={version} review onOpen={(id) => openSubmission(id, true)} /> }
  return <div style={{ padding: isMobile ? 16 : 24, maxWidth: 1400, margin: '0 auto' }}>
    <Space wrap style={{ justifyContent: 'space-between', width: '100%', marginBottom: 16 }}>
      <div><Typography.Title level={3} style={{ margin: 0 }}>提示词工坊</Typography.Title><Typography.Text type="secondary">浏览本实例的写作风格与提示词模板，导入后用于自己的创作。</Typography.Text></div>
      <Space wrap><Button icon={<ReloadOutlined />} onClick={refresh}>刷新</Button>
        {isAdmin && <><Button icon={<PlusOutlined />} onClick={() => setShare({ initial: emptyWorkshopShare('style'), direct: true })}>发布官方风格</Button><Button icon={<PlusOutlined />} onClick={() => setShare({ initial: emptyWorkshopShare('prompt'), direct: true })}>发布官方模板</Button></>}
      </Space>
    </Space>
    {pending.error && <Alert type="error" showIcon message={`待审核数量加载失败：${pending.error}`} style={{ marginBottom: 12 }} />}
    <Tabs activeKey={tab} onChange={setTab} destroyOnHidden items={[
      { key: 'browse', label: '浏览', children: <Browse version={version} onOpen={openItem} /> },
      { key: 'mine', label: '我的分享', children: <Space direction="vertical" size="large" style={{ width: '100%' }}>
        <Alert type="info" showIcon message={<Space wrap><span>从自己的内容开始分享：</span><Link to="/styles">打开写作风格库</Link><Link to="/prompt-templates">打开提示词模板</Link></Space>} />
        <MyItems version={version} onOpen={openItem} />
        <Submissions version={version} review={false} onOpen={(id) => openSubmission(id, false)} />
      </Space> },
      ...(isAdmin ? [reviewTab] : []),
    ]} />
    {itemId !== null && <WorkshopItemDrawer key={itemId} itemId={itemId} onClose={() => setItemId(null)} onChanged={refresh}
      onUpdate={(item) => setShare({ initial: updateShare(item), direct: false })} />}
    {submission && <ReviewPanel key={`${submission.id}:${submission.review}`} submissionId={submission.id} review={submission.review}
      onClose={() => setSubmission(null)} onChanged={refresh} onResubmit={(initial) => { setSubmission(null); setShare({ initial, direct: false }) }}
      onOpenItem={(id) => { setSubmission(null); setItemId(id) }} />}
    {share && <ShareToWorkshopModal initial={share.initial} direct={share.direct} onClose={() => setShare(null)} onSaved={refresh} />}
  </div>
}

function ItemGrid({ items, onOpen }: { items: WorkshopItem[]; onOpen: (item: WorkshopItem) => void }) {
  return items.length ? <Row gutter={[16, 16]}>{items.map((item) => <Col key={item.id} xs={24} md={12} xl={8}><WorkshopItemCard item={item} onOpen={onOpen} /></Col>)}</Row> : <Empty description="暂无分享" />
}
function Browse({ version, onOpen }: { version: number; onOpen: (item: WorkshopItem) => void }) {
  const [query, setQuery] = useState<WorkshopQuery>({ page: 1, page_size: 12, sort: 'newest' })
  const loader = useCallback(() => { void version; return workshopApi.list(query) }, [query, version])
  const { data, loading, error } = useLoader(loader)
  const filter = (patch: Partial<WorkshopQuery>) => setQuery((q) => ({ ...q, ...patch, page: 1 }))
  return <Space direction="vertical" size="middle" style={{ width: '100%' }}>
    <Row gutter={[12, 12]}>
      <Col xs={24} sm={12} lg={6}><Input.Search aria-label="搜索分享" placeholder="搜索名称或说明" allowClear onSearch={(q) => filter({ q: q.trim() || undefined })} /></Col>
      <Col xs={12} lg={4}><Select aria-label="分享类型" style={{ width: '100%' }} placeholder="全部类型" allowClear value={query.kind} onChange={(kind) => filter({ kind })}
        options={Object.entries(WORKSHOP_KINDS).map(([value, label]) => ({ value, label }))} /></Col>
      <Col xs={12} lg={4}><Select aria-label="分享分类" style={{ width: '100%' }} placeholder="全部分类" allowClear value={query.category} onChange={(category) => filter({ category })} options={workshopCategoryOptions} /></Col>
      <Col xs={24} sm={12} lg={5}><Input.Search aria-label="标签筛选" placeholder="按完整标签筛选" allowClear onSearch={(tag) => filter({ tag: tag.trim() || undefined })} /></Col>
      <Col xs={24} sm={12} lg={5}><Input.Search aria-label="模板 key 筛选" placeholder="模板 key，例如 chapter.generate" allowClear onSearch={(key) => filter({ prompt_key: key.trim() || undefined })} /></Col>
    </Row>
    <Select aria-label="排序方式" value={query.sort} onChange={(sort) => filter({ sort })} style={{ minWidth: 130 }}
      options={[{ value: 'newest', label: '最新分享' }, { value: 'likes', label: '最多点赞' }, { value: 'downloads', label: '最多下载' }]} />
    {error && <Alert type="error" showIcon message={error} />}
    <Spin spinning={loading}><ItemGrid items={data?.items ?? []} onOpen={onOpen} /></Spin>
    <Pagination current={query.page} pageSize={query.page_size} total={data?.total ?? 0} showSizeChanger pageSizeOptions={[12, 24, 48]} responsive
      showTotal={(n) => `共 ${n} 条`} onChange={(page, page_size) => setQuery((q) => ({ ...q, page: page_size !== q.page_size ? 1 : page, page_size }))} />
  </Space>
}
function MyItems({ version, onOpen }: { version: number; onOpen: (item: WorkshopItem) => void }) {
  const [page, setPage] = useState(1)
  const loader = useCallback(() => { void version; return workshopApi.mine(page) }, [page, version])
  const { data, loading, error } = useLoader(loader)
  return <div><Typography.Title level={4}>已发布的条目</Typography.Title>
    {error && <Alert type="error" showIcon message={error} />}
    <Spin spinning={loading}><ItemGrid items={data?.items ?? []} onOpen={onOpen} /></Spin>
    <Pagination current={page} pageSize={12} total={data?.total ?? 0} onChange={setPage} showSizeChanger={false} responsive style={{ marginTop: 16 }} />
  </div>
}
function Submissions({ version, review, onOpen }: { version: number; review: boolean; onOpen: (id: number) => void }) {
  const [query, setQuery] = useState<SubmissionQuery>({ page: 1, page_size: 12, status: review ? 'pending' : undefined })
  const loader = useCallback(() => { void version; return review ? workshopApi.pending(query) : workshopApi.submissions(query) }, [review, query, version])
  const { data, loading, error } = useLoader(loader)
  return <div>
    <Space wrap style={{ marginBottom: 12 }}><Typography.Title level={4} style={{ margin: 0 }}>{review ? '审核提交' : '提交记录'}</Typography.Title>
      <Select aria-label="提交状态" style={{ minWidth: 130 }} value={query.status} allowClear={!review} placeholder="全部状态"
        onChange={(status: WorkshopSubmissionStatus | undefined) => setQuery((q) => ({ ...q, status, page: 1 }))}
        options={Object.entries(SUBMISSION_STATUS).map(([value, status]) => ({ value, label: status.label }))} />
    </Space>
    {error && <Alert showIcon type="error" message={error} />}
    <List loading={loading} dataSource={data?.items ?? []} locale={{ emptyText: review ? '没有待处理的提交' : '暂无提交记录' }} renderItem={(s) => <List.Item key={s.id} style={{ display: 'block' }}>
      <Space wrap><Button type="link" style={{ whiteSpace: 'normal', height: 'auto', paddingInline: 0, overflowWrap: 'anywhere', textAlign: 'left' }} onClick={() => onOpen(s.id)}>{s.name}</Button>
        <Tag color={SUBMISSION_STATUS[s.status].color}>{SUBMISSION_STATUS[s.status].label}</Tag><Tag>{WORKSHOP_KINDS[s.kind]}</Tag><Tag>{workshopSubmissionKindLabel(s)}</Tag>
      </Space>
      <Typography.Paragraph type="secondary" style={{ marginBottom: 4 }}>{s.author_username ?? s.author_name ?? '匿名'} · {formatTime(s.created_at)}</Typography.Paragraph>
      {s.review_note && <Typography.Paragraph type={s.status === 'rejected' ? 'danger' : 'secondary'} style={{ overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' }}>审核说明：{s.review_note}</Typography.Paragraph>}
    </List.Item>} />
    <Pagination current={query.page} pageSize={12} total={data?.total ?? 0} showSizeChanger={false} responsive onChange={(page) => setQuery((q) => ({ ...q, page }))} />
  </div>
}
