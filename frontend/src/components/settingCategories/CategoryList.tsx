import { ReloadOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Empty, Input, Pagination, Select, Skeleton, Space, Table, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useCallback, useState } from 'react'
import { settingCategoriesApi } from '../../api/settingCategories'
import { SETTING_ENTRY_TYPE_LABELS, SETTING_ENTRY_TYPE_OPTIONS, settingFieldsApi } from '../../api/settingFields'
import type { SettingCategory, SettingEntryType } from '../../api/types'
import { useIsMobile } from '../../hooks/useIsMobile'
import { changeCategoryFilters, initialCategoryFilters, withCategoryMetadata } from './categoryQuery'
import CategoryRulesSummary from './CategoryRulesSummary'
import { useCategoryRequest, CATEGORY_CONFIGURATION_RESOURCES } from './useCategoryRequest'

interface Props { projectId: number; actorId: number; selectedId: number | null; editable: boolean; onSelect: (id: number) => void; onOpen: (id: number) => void }

/** 分类配置分页单独读取；成员数量只在结果查询中计算。 */
export default function CategoryList({ projectId, actorId, selectedId, editable, onSelect, onOpen }: Props) {
  const [filters, setFilters] = useState(initialCategoryFilters)
  const { types, q, page, pageSize } = filters
  const loader = useCallback(async (signal: AbortSignal) => {
    const [value, definitions] = await Promise.all([settingCategoriesApi.list(projectId, { types, q: q || undefined, page, page_size: pageSize }, signal), settingFieldsApi.listDefinitions(projectId, { include_archived: true }, signal)])
    return withCategoryMetadata(value, definitions)
  }, [page, pageSize, projectId, q, types])
  const state = useCategoryRequest(projectId, actorId, loader, CATEGORY_CONFIGURATION_RESOURCES)
  const mobile = useIsMobile()
  const operations = (item: SettingCategory) => <Space wrap size={[8, 8]}>
    <Button size="small" type={selectedId === item.id ? 'primary' : 'default'} onClick={() => onSelect(item.id)} aria-label={`查看分类「${item.name}」的结果`}>{selectedId === item.id ? '已选结果' : '查看结果'}</Button>
    <Button size="small" onClick={() => onOpen(item.id)} aria-label={`${editable ? '编辑' : '查看'}分类「${item.name}」的规则`}>{editable ? '编辑规则' : '规则与预览'}</Button>
  </Space>
  const columns: ColumnsType<SettingCategory> = [
    { title: '分类', key: 'name', render: (_, item) => <Space direction="vertical" size={4} style={{ width: '100%', overflowWrap: 'anywhere' }}><Typography.Text strong>{item.name}</Typography.Text><Typography.Text type="secondary">{item.applicable_types.map((type) => SETTING_ENTRY_TYPE_LABELS[type]).join('、')}</Typography.Text><Typography.Text type="secondary">{item.match_mode === 'all' ? '全部条件' : '任一条件'} · {item.rules.length} 条 · 顺序 {item.sort}</Typography.Text></Space> },
    { title: '操作', key: 'operations', width: '42%', render: (_, item) => operations(item) },
  ]
  return <Card title="分类列表" extra={<Button size="small" icon={<ReloadOutlined />} loading={state.loading} onClick={() => { void state.refresh() }}>刷新</Button>} style={{ minWidth: 0 }}>
    <Space direction="vertical" size={16} style={{ width: '100%', minWidth: 0 }}>
      <div><Typography.Text>适用类型</Typography.Text><Select<SettingEntryType[]> mode="multiple" aria-label="筛选分类适用类型" allowClear placeholder="全部设定类型" value={types} options={SETTING_ENTRY_TYPE_OPTIONS} style={{ width: '100%', marginTop: 6 }} onChange={(types) => setFilters((current) => changeCategoryFilters(current, { types }))} /></div>
      <label htmlFor="category-list-search"><Typography.Text>分类名称</Typography.Text><Input.Search id="category-list-search" aria-label="搜索分类名称" allowClear enterButton="搜索" placeholder="输入名称后搜索" onSearch={(q) => setFilters((current) => changeCategoryFilters(current, { q }))} style={{ marginTop: 6 }} /></label>
      {state.error && <Alert type="error" showIcon message={state.error} action={<Button size="small" onClick={() => { void state.refresh() }}>重试</Button>} />}
      {state.loading ? <Skeleton active paragraph={{ rows: 5 }} /> : state.data && (!state.data.value.items.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={state.data.value.total ? '这一页没有分类，可调整页码。' : '还没有符合筛选的分类。'} />
        : mobile ? state.data.value.items.map((item) => <Card key={item.id} size="small" style={{ minWidth: 0 }}><Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Typography.Text strong style={{ overflowWrap: 'anywhere' }}>{item.name}</Typography.Text><CategoryRulesSummary category={item} definitions={state.data?.definitions.items ?? []} />{operations(item)}
        </Space></Card>) : <Table columns={columns} dataSource={state.data.value.items} rowKey="id" pagination={false} tableLayout="fixed" size="small" />)}
      {state.data && state.data.value.total > 0 && <Pagination aria-label="分类列表分页" current={state.data.value.page} pageSize={state.data.value.page_size} total={state.data.value.total} pageSizeOptions={[20, 50, 100]} showSizeChanger responsive onChange={(page, pageSize) => setFilters((current) => changeCategoryFilters(current, { page, pageSize }))} />}
    </Space>
  </Card>
}
