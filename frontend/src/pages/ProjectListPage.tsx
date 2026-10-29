import { AppstoreOutlined, BulbOutlined, CloudUploadOutlined, ImportOutlined, PlusOutlined, ReloadOutlined, SearchOutlined, UnorderedListOutlined } from '@ant-design/icons'
import { Alert, App, Button, Input, Select, Skeleton, Spin } from 'antd'
import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { errorMessage } from '../api/client'
import { PROJECT_STATUS, projectApi } from '../api/projects'
import type { Project } from '../api/types'
import ExportModal from '../components/ExportModal'
import { StudioEmpty, StudioPage, StudioPanel } from '../components/StudioUI'
import ContinueWriting from '../components/shelf/ContinueWriting'
import CreateProjectModal from '../components/shelf/CreateProjectModal'
import LibraryBookCard from '../components/shelf/LibraryBookCard'
import { useLoader } from '../hooks/useLoader'
import { useAuthStore } from '../stores/auth'
import { DEFAULT_SHELF_PREFS, filterAndSort, latestWritingProject, readShelfPrefs, SHELF_SORT_OPTIONS, shelfStats, writeShelfPrefs, type ShelfPrefs, type ShelfStatusFilter } from '../utils/shelf'
import './ProjectListPage.css'

const ImportBackupModal = lazy(() => import('../components/transfer/ImportBackupModal'))
const BackupModal = lazy(() => import('../components/transfer/BackupModal'))
const STATUS_FILTERS: { value: ShelfStatusFilter; label: string }[] = [{ value: 'all', label: '全部作品' }, { value: 'writing', label: PROJECT_STATUS.writing.label }, { value: 'planning', label: PROJECT_STATUS.planning.label }, { value: 'completed', label: PROJECT_STATUS.completed.label }]

/** 真实作品书库：设计稿的纸面书架，沿用作品 API、阅读进度及全部原有作品操作。 */
export default function ProjectListPage() {
  const { message, modal } = App.useApp()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const author = useAuthStore((s) => s.user?.username)
  const loader = useCallback(() => projectApi.list(), [])
  const { data: projects = [], loading, error, reload } = useLoader(loader)
  const [importOpen, setImportOpen] = useState(false)
  const [exportId, setExportId] = useState<number | null>(null)
  const [backupId, setBackupId] = useState<number | null>(null)
  const [prefs, setPrefs] = useState<ShelfPrefs>(readShelfPrefs)
  useEffect(() => writeShelfPrefs(prefs), [prefs])
  const visible = useMemo(() => filterAndSort(projects, prefs), [projects, prefs])
  const stats = useMemo(() => shelfStats(projects), [projects])
  const recent = useMemo(() => latestWritingProject(projects), [projects])
  const filtering = prefs.query.trim() !== '' || prefs.status !== 'all'
  const createOpen = searchParams.get('create') === '1'
  const setCreateOpen = (open: boolean) => {
    const next = new URLSearchParams(searchParams)
    if (open) next.set('create', '1')
    else next.delete('create')
    setSearchParams(next, { replace: !open })
  }
  const remove = (project: Project) => modal.confirm({
    title: `删除《${project.title}》？`, content: '作品下的世界观、角色、大纲和全部章节都将被删除，且无法恢复。请先保存需要的备份。', okText: '删除', okButtonProps: { danger: true }, cancelText: '取消',
    onOk: async () => {
      try { await projectApi.remove(project.id); message.success('已删除'); reload() }
      catch (err) { message.error(errorMessage(err)); throw err }
    },
  })
  const hint = loading && !projects.length ? '正在载入你的作品…' : error && !projects.length ? '暂时无法读取作品，请重试。' : `${stats.count} 部作品 · ${stats.words.toLocaleString()} 字，每个世界都在这里。`

  return (
    <StudioPage title="我的作品" hint={hint} action={<Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>新建作品</Button>}>
      {recent && <ContinueWriting key={recent.id} project={recent} />}
      <div className="library-toolbar">
        <div className="library-filters" role="group" aria-label="作品状态筛选">{STATUS_FILTERS.map((item) => <button key={item.value} type="button" aria-pressed={prefs.status === item.value} onClick={() => setPrefs((p) => ({ ...p, status: item.value }))}>{item.label}</button>)}</div>
        <div className="library-controls">
          <Input className="library-search" allowClear prefix={<SearchOutlined />} placeholder="搜索作品、题材或标签" aria-label="搜索作品、题材或标签" maxLength={100} value={prefs.query} onChange={(e) => setPrefs((p) => ({ ...p, query: e.target.value }))} />
          <Select className="library-sort" value={prefs.sort} onChange={(sort) => setPrefs((p) => ({ ...p, sort }))} options={SHELF_SORT_OPTIONS} aria-label="作品排序" />
          <Button className="library-view-toggle" aria-label={prefs.view === 'shelf' ? '切换为列表视图' : '切换为书架视图'} title={prefs.view === 'shelf' ? '列表视图' : '书架视图'} icon={prefs.view === 'shelf' ? <UnorderedListOutlined /> : <AppstoreOutlined />} onClick={() => setPrefs((p) => ({ ...p, view: p.view === 'shelf' ? 'list' : 'shelf' }))} />
        </div>
      </div>
      {filtering && !loading && <p className="library-filter-count" role="status">显示 {visible.length} / {projects.length} 部作品</p>}
      {error && <Alert className="library-error" type="error" showIcon message={error} action={<Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={reload}>重试</Button>} />}
      <section aria-label="作品书库" aria-busy={loading}>
        {loading && !projects.length ? <div className="library-grid library-loading" role="status" aria-label="正在加载作品">{[0, 1, 2, 3].map((i) => <div key={i}><Skeleton.Node active style={{ width: '100%', height: 174 }} /><Skeleton active title paragraph={{ rows: 3 }} /></div>)}</div> : !error || projects.length > 0 ? (
          visible.length ? <Spin spinning={loading}><div className={prefs.view === 'shelf' ? 'library-grid' : 'library-list'}>{visible.map((project) => <LibraryBookCard key={project.id} project={project} author={author} onExport={() => setExportId(project.id)} onBackup={() => setBackupId(project.id)} onDelete={() => remove(project)} />)}</div></Spin> : <StudioPanel><StudioEmpty title={projects.length ? '没有符合条件的作品' : '这里还没有故事'} text={projects.length ? '试试其他关键词，或清除筛选，重新看看你的故事。' : '新建一部作品，或从一个灵感开始，让第一个念头落在纸上。'} action={projects.length ? <Button onClick={() => setPrefs((p) => ({ ...p, query: DEFAULT_SHELF_PREFS.query, status: 'all' }))}>清除筛选</Button> : <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>新建作品</Button>} /></StudioPanel>
        ) : null}
      </section>
      <footer className="library-footer">
        <Link to="/inspiration"><BulbOutlined />从一个灵感开始</Link>
        <Link to="/book-import"><ImportOutlined />导入已有小说</Link>
        <button type="button" onClick={() => setImportOpen(true)}><CloudUploadOutlined />恢复项目备份</button>
      </footer>
      {exportId !== null && <ExportModal projectId={exportId} open onClose={() => setExportId(null)} />}
      {backupId !== null && <Suspense fallback={null}><BackupModal projectId={backupId} onClose={() => setBackupId(null)} /></Suspense>}
      {importOpen && <Suspense fallback={null}><ImportBackupModal onClose={() => setImportOpen(false)} onImported={(project) => { setImportOpen(false); navigate(`/projects/${project.id}`) }} /></Suspense>}
      <CreateProjectModal open={createOpen} onClose={() => setCreateOpen(false)} onCreated={(id) => navigate(`/projects/${id}/wizard`)} />
    </StudioPage>
  )
}
