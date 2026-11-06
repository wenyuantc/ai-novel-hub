import { PlusOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Empty, Modal, Skeleton, Space, Typography } from 'antd'
import { useCallback, useEffect, useMemo } from 'react'
import { useBlocker, useLocation, useSearchParams } from 'react-router-dom'
import { chapterApi } from '../../api/chapters'
import CategoryEditorDrawer from '../../components/settingCategories/CategoryEditorDrawer'
import CategoryList from '../../components/settingCategories/CategoryList'
import CategoryResults from '../../components/settingCategories/CategoryResults'
import { categoryEditorDirty } from '../../components/settingCategories/categoryEditor'
import { categoryDirty } from '../../components/settingCategories/categoryDraft'
import { categoryLocation, changeCategoryFilters, readCategoryLocation, type CategoryFilters } from '../../components/settingCategories/categoryQuery'
import { useCategoryEditor } from '../../components/settingCategories/useCategoryEditor'
import { useCategoryRequest } from '../../components/settingCategories/useCategoryRequest'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useProjectId } from '../../hooks/useProjectId'
import { useAuthStore } from '../../stores/auth'
import { splitPane, splitRow } from '../../utils/layout'

/** 自动分类只保存规则，结果由当前标签和按位置求值的下拉字段共同决定。 */
export default function SettingCategoriesPanel() {
  const projectId = useProjectId()
  const user = useAuthStore((state) => state.user)
  const { ready, canRead } = useProjectAccess(projectId)
  if (!ready) return <Skeleton active />
  if (!canRead || !user) return <Alert type="warning" message="当前账号无权查看设定分类" />
  return <CategoryWorkspace key={`${projectId}:${user.id}`} projectId={projectId} actorId={user.id} />
}

function CategoryWorkspace({ projectId, actorId }: { projectId: number; actorId: number }) {
  const { canEdit, canEditNow, canReadNow } = useProjectAccess(projectId)
  const location = useLocation()
  const [, setSearchParams] = useSearchParams()
  const selected = useMemo(() => readCategoryLocation(location.search), [location.search])
  const select = useCallback((id: number | null) => {
    setSearchParams((params) => { const next = new URLSearchParams(params); if (id === null) next.delete('id'); else next.set('id', String(id)); next.delete('page'); return next })
  }, [setSearchParams])
  const { state, controller } = useCategoryEditor(projectId, actorId, select)
  const chaptersLoader = useCallback(() => chapterApi.list(projectId), [projectId])
  const chapters = useCategoryRequest(projectId, actorId, chaptersLoader, ['chapters', 'outlines', 'project'])
  const dirty = categoryEditorDirty(state)
  const busy = Object.values(state.sessions).some((session) => session.saving)
  const blocker = useBlocker(({ currentLocation, nextLocation }) => dirty && currentLocation.pathname !== nextLocation.pathname)
  useEffect(() => {
    if (!dirty) return
    const beforeUnload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('beforeunload', beforeUnload)
    return () => window.removeEventListener('beforeunload', beforeUnload)
  }, [dirty])
  const session = state.activeKey ? state.sessions[state.activeKey] : null
  const drafts = Object.values(state.sessions).filter((value) => categoryDirty(value.draft, value.original))
  const changeFilters = (patch: Partial<CategoryFilters>) => setSearchParams(categoryLocation(selected.categoryId, changeCategoryFilters(selected.filters, patch)).slice(1), { replace: true })
  const open = (id: number) => { if (canReadNow()) void controller.open(id) }
  return <Space direction="vertical" size={16} style={{ width: '100%', minWidth: 0 }}>
    <Space wrap size={16} style={{ width: '100%', justifyContent: 'space-between' }}>
      <div style={{ minWidth: 0 }}><Typography.Title level={3} style={{ margin: 0 }}>设定分类</Typography.Title><Typography.Paragraph type="secondary" style={{ margin: '8px 0 0' }}>通过整理标签和下拉字段自动归类角色、世界观、组织及职业。</Typography.Paragraph></div>
      {canEdit && <Button type="primary" icon={<PlusOutlined />} disabled={busy} onClick={() => { if (canEditNow()) void controller.open(null) }}>新建分类</Button>}
    </Space>
    <Alert type="info" showIcon message="下拉字段按所选章节计算，标签和分类规则使用当前版本" />
    {!canEdit && <Alert type="info" message="当前为只读，可查看完整规则、匹配结果和草稿预览。" />}
    {state.notice && <Alert type="success" showIcon message={state.notice} />}
    {drafts.length > 0 && <Alert type="warning" showIcon message={`有 ${drafts.length} 份未保存的分类草稿`} description={<Space wrap size={[8, 8]}>{drafts.map((value) => <Button key={value.key} size="small" disabled={busy} style={{ maxWidth: '100%', height: 'auto', whiteSpace: 'normal', overflowWrap: 'anywhere' }} onClick={() => { void controller.open(value.categoryId) }}>继续：{value.draft.name || '新建分类'}</Button>)}</Space>} />}
    {chapters.error && <Alert type="error" message={`已有章节读取失败：${chapters.error}`} action={<Button size="small" onClick={() => { void chapters.refresh() }}>重试章节</Button>} />}
    <div style={splitRow}>
      <div style={splitPane(400)}><CategoryList projectId={projectId} actorId={actorId} selectedId={selected.categoryId} editable={canEdit} onSelect={select} onOpen={open} /></div>
      <div style={splitPane(640)}>{selected.categoryId === null ? <Card><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="选择一个分类查看自动匹配结果；新分类可先编辑条件再预览。" /></Card>
        : <CategoryResults projectId={projectId} actorId={actorId} categoryId={selected.categoryId} filters={selected.filters} chapters={chapters.data ?? []} onChange={changeFilters} editable={canEdit} onOpen={() => { if (selected.categoryId !== null) open(selected.categoryId) }} />}</div>
    </div>
    {session && <CategoryEditorDrawer key={session.key} projectId={projectId} session={session} controller={controller} initialFilters={selected.filters} chapters={chapters.data ?? []} chaptersError={chapters.error} retryChapters={() => { void chapters.refresh() }} />}
    <Modal title="分类有未保存草稿" open={blocker.state === 'blocked'} okText="放弃草稿并离开" cancelText="继续编辑" okButtonProps={{ danger: true, disabled: busy }}
      onCancel={() => { if (blocker.state === 'blocked') blocker.reset() }} onOk={() => { if (!busy && blocker.state === 'blocked') blocker.proceed() }}>
      {busy ? '正在保存，请等待完成。' : '离开页面会丢弃此页尚未保存的分类草稿。'}
    </Modal>
  </Space>
}
