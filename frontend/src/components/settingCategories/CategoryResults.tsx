import { ReloadOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Skeleton, Space, Typography } from 'antd'
import { useCallback } from 'react'
import { settingCategoriesApi } from '../../api/settingCategories'
import { settingFieldsApi } from '../../api/settingFields'
import type { ChapterReference } from '../progressions/position'
import { categoryQueryRequest, withCategoryMetadata, type CategoryFilters } from './categoryQuery'
import CategoryEntriesView from './CategoryEntriesView'
import CategoryResultFilters from './CategoryResultFilters'
import CategoryRulesSummary from './CategoryRulesSummary'
import { useCategoryRequest } from './useCategoryRequest'

interface Props { projectId: number; actorId: number; categoryId: number; filters: CategoryFilters; chapters: readonly ChapterReference[]; onChange: (patch: Partial<CategoryFilters>) => void; onOpen: () => void; editable: boolean }

/** 分类、匹配理由和字段名称来自同版本快照，不在客户端逐项拼合求值。 */
export default function CategoryResults({ projectId, actorId, categoryId, filters, chapters, onChange, onOpen, editable }: Props) {
  const loader = useCallback(async (signal: AbortSignal) => {
    const request = categoryQueryRequest(filters)
    if (!request.ok) throw new Error(request.error)
    const [value, definitions] = await Promise.all([settingCategoriesApi.entries(projectId, categoryId, request.query, signal), settingFieldsApi.listDefinitions(projectId, { include_archived: true }, signal)])
    return { ...withCategoryMetadata(value, definitions), position: request.position }
  }, [categoryId, filters, projectId])
  const state = useCategoryRequest(projectId, actorId, loader)
  return <Card title="分类结果" extra={<Button size="small" icon={<ReloadOutlined />} loading={state.loading} onClick={() => { void state.refresh() }}>刷新</Button>} style={{ minWidth: 0 }}>
    <Space direction="vertical" size={16} style={{ width: '100%', minWidth: 0 }}>
      {state.data && <>
        <Space wrap size={12} style={{ width: '100%', justifyContent: 'space-between' }}><Typography.Title level={4} style={{ margin: 0, overflowWrap: 'anywhere' }}>{state.data.value.category.name}</Typography.Title><Button onClick={onOpen}>{editable ? '编辑规则 / 草稿预览' : '查看规则 / 预览'}</Button></Space>
        <CategoryRulesSummary category={state.data.value.category} definitions={state.data.definitions.items} />
      </>}
      <CategoryResultFilters value={filters} chapters={chapters} onChange={onChange} />
      {state.loading && <Skeleton active paragraph={{ rows: 6 }} />}
      {state.error && <Alert type="error" showIcon message={state.error} action={<Button size="small" onClick={() => { void state.refresh() }}>重试读取</Button>} />}
      {state.data && <CategoryEntriesView projectId={projectId} data={state.data.value} position={state.data.position} onPage={(page, pageSize) => onChange({ page, pageSize })} />}
    </Space>
  </Card>
}
