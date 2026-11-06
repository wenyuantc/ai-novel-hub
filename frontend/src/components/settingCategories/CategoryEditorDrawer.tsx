import { DeleteOutlined, ReloadOutlined } from '@ant-design/icons'
import { Alert, App, Button, Divider, Drawer, Skeleton, Space, Typography } from 'antd'
import { useState } from 'react'
import { useModalWidth } from '../../hooks/useIsMobile'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import type { ChapterReference } from '../progressions/position'
import { categoryDirty } from './categoryDraft'
import { categoryQueryRequest, changeCategoryFilters, type CategoryFilters } from './categoryQuery'
import type { CategoryEditor, CategorySession } from './categoryEditor'
import CategoryDraftFields from './CategoryDraftFields'
import CategoryEntriesView from './CategoryEntriesView'
import CategoryResultFilters from './CategoryResultFilters'
import CategoryReview from './CategoryReview'

interface Props { projectId: number; session: CategorySession; controller: CategoryEditor; initialFilters: CategoryFilters; chapters: readonly ChapterReference[]; chaptersError: string | null; retryChapters: () => void }

/** 编辑与只读预览共用完整规则；关闭仅隐藏，重新打开仍保留本地草稿。 */
export default function CategoryEditorDrawer({ projectId, session, controller, initialFilters, chapters, chaptersError, retryChapters }: Props) {
  const width = useModalWidth(900)
  const { modal } = App.useApp()
  const { canEdit, canEditNow } = useProjectAccess(projectId)
  const [filters, setFilters] = useState(() => ({ ...initialFilters, page: 1 }))
  const busy = session.loading || session.saving || session.reviewing
  const disabled = !canEdit || busy
  const request = categoryQueryRequest(filters)
  const changeFilters = (patch: Partial<CategoryFilters>) => {
    const next = changeCategoryFilters(filters, patch)
    const refresh = !!session.preview || session.previewLoading
    controller.expirePreview(); setFilters(next)
    const query = categoryQueryRequest(next)
    if (refresh && query.ok) void controller.preview(query.query)
  }
  const remove = () => {
    if (!canEditNow()) return
    const token = controller.deleteToken()
    if (!token) return
    modal.confirm({ title: '删除此自动分类？', content: '只删除分类规则，设定条目、整理标签和字段值都会保留。', okText: '删除分类', cancelText: '保留分类', okButtonProps: { danger: true },
      onOk: () => { if (canEditNow()) return controller.remove(token) } })
  }
  return <Drawer open={session.open} width={width} title={session.categoryId === null ? '新建自动分类' : canEdit ? '编辑自动分类' : '分类规则与只读预览'}
    onClose={controller.close} maskClosable={!session.saving} closable={!session.saving}
    footer={<Space wrap size={[12, 12]} style={{ width: '100%', justifyContent: 'space-between' }}>
      <Space wrap>{canEdit && session.original && <Button danger icon={<DeleteOutlined />} disabled={busy || session.reviewRequired} onClick={remove}>删除分类</Button>}
        {categoryDirty(session.draft, session.original) && <Button disabled={busy} onClick={controller.discard}>放弃此草稿</Button>}</Space>
      <Space wrap size={12}><Button disabled={session.saving} onClick={controller.close}>关闭并保留草稿</Button>{canEdit && <Button type="primary" loading={session.saving} disabled={busy || !session.base || session.reviewRequired || !categoryDirty(session.draft, session.original)} onClick={() => { if (canEditNow()) void controller.save() }}>保存分类</Button>}</Space>
    </Space>}>
    <Space direction="vertical" size={16} style={{ width: '100%', minWidth: 0 }}>
      <Alert type="info" showIcon message="下拉字段按所选章节计算，标签和分类规则使用当前版本" description="分类成员完全由条件自动计算，可在保存前预览；这里不维护手工成员。" />
      {!canEdit && <Alert type="info" message="当前只读，可查看完整规则与预览匹配结果。" />}
      {session.error && <Alert type="error" showIcon message={session.issueIndex === null ? session.error : `条件 ${session.issueIndex + 1}：${session.error}`} />}
      {session.reviewRequired && <Alert type="warning" showIcon message="配置已有变化，原草稿和保存依据已保留" description="请读取最新对照并明确采用后再保存。" action={<Button icon={<ReloadOutlined />} loading={session.reviewing} disabled={session.saving} onClick={() => { void controller.review() }}>读取最新对照</Button>} />}
      <CategoryReview session={session} controller={controller} editable={canEdit} />
      {session.loading ? <Skeleton active paragraph={{ rows: 8 }} /> : !session.base ? <Alert type="warning" message="尚未取得完整分类与字段配置" action={<Button onClick={() => { void controller.open(session.categoryId) }}>重试读取</Button>} />
        : <><CategoryDraftFields session={session} controller={controller} disabled={disabled} />
          <Divider style={{ margin: '8px 0' }} />
          <Typography.Title level={4} style={{ margin: 0 }}>草稿匹配预览</Typography.Title>
          {chaptersError && <Alert type="error" message={`已有章节读取失败：${chaptersError}`} action={<Button size="small" onClick={retryChapters}>重试章节</Button>} />}
          <CategoryResultFilters value={filters} chapters={chapters} disabled={busy} onChange={changeFilters} />
          {!request.ok && <Alert type="warning" message={request.error} />}
          <Button loading={session.previewLoading} disabled={busy || !request.ok || session.reviewRequired} onClick={() => { if (request.ok) void controller.preview(request.query) }}>预览草稿匹配</Button>
          {session.previewLoading && <Skeleton active paragraph={{ rows: 4 }} />}
          {session.previewError && <Alert type="error" showIcon message={session.previewError} action={<Button size="small" disabled={busy || !request.ok || session.reviewRequired} onClick={() => { if (request.ok) void controller.preview(request.query) }}>重试预览</Button>} />}
          {session.preview && request.ok && <CategoryEntriesView projectId={projectId} data={session.preview.value} position={request.position} onPage={(page, pageSize) => changeFilters({ page, pageSize })} />}
        </>}
    </Space>
  </Drawer>
}
