import { BookOutlined, CloudDownloadOutlined, DashboardOutlined, DeleteOutlined, DownloadOutlined, EditOutlined, MoreOutlined } from '@ant-design/icons'
import { Button, Dropdown, Progress } from 'antd'
import { Link, useNavigate } from 'react-router-dom'
import { coverUrl } from '../../api/projects'
import type { Project } from '../../api/types'
import { canRead, continueTarget, readingLabel, readTarget } from '../../utils/shelf'
import { isProjectAccess } from '../../utils/projectAccess'
import { wordsProgress } from '../../utils/projectFields'
import { formatRelative, formatTime } from '../../utils/time'
import { ProjectBadge } from '../StudioUI'
import BookCover from './BookCover'

interface Props { project: Project; onExport: () => void; onBackup: () => void; onDelete: () => void }

/** 书库中共享的卡片/列表条目，全部操作复用已有作品路由和接口。 */
export default function LibraryBookCard({ project: p, onExport, onBackup, onDelete }: Props) {
  const navigate = useNavigate()
  const overview = `/projects/${p.id}/overview`
  const progress = wordsProgress(p.total_words, p.target_words)
  const access = isProjectAccess(p.access) ? p.access : null
  if (!access?.can_read) return null
  const role = access?.is_collaborator ? access.role === 'editor' ? '编辑者' : '查看者' : null
  const author = p.pen_name?.name ?? p.owner?.username
  return (
    <article className="library-book" aria-labelledby={`library-book-title-${p.id}`}>
      <div className="library-book-main">
        <Link className="library-book-cover" to={overview} aria-label={`打开《${p.title}》`}><BookCover title={p.title} genre={p.genre} author={author} coverUrl={coverUrl(p)} /></Link>
        <div className="library-book-details">
          <div className="library-book-status"><ProjectBadge status={p.status} />{role && <span className="library-collaboration-role">共享 · {role}</span>}</div>
          <h2 className="library-book-heading" id={`library-book-title-${p.id}`}><Link to={overview} title={p.title}>{p.title}</Link></h2>
          {p.series && <Link className="library-book-series" to={`/series/${p.series.id}`} title={`所属系列：${p.series.name}`}>系列：{p.series.name}</Link>}
          {author && <span className="library-book-author">作者：{author}</span>}
          {access?.is_collaborator && <span className="library-book-author">{access.allow_owner_ai ? '作者模型已授权' : '作者模型未授权'}</span>}
          <p className="library-book-description" title={p.synopsis || p.premise}>{p.synopsis || p.premise || '故事尚在构思，写下第一段简介，让这个世界慢慢清晰。'}</p>
          <time className="library-book-updated" dateTime={p.updated_at} title={formatTime(p.updated_at)}>更新于 {formatRelative(p.updated_at)}</time>
        </div>
      </div>
      {progress !== null && <div className="library-book-progress" title={`目标 ${p.target_words?.toLocaleString()} 字`}><Progress percent={progress} showInfo={false} size="small" /></div>}
      <footer className="library-book-footer">
        <div className="library-book-summary">
          <div className="library-book-meta"><span>{p.chapter_count} 章</span><span>{p.total_words.toLocaleString()} 字</span></div>
          <div className="library-book-actions">
            {access?.can_edit_content ? <Link className="library-write-button" to={continueTarget(p)}><EditOutlined />{p.last_edited_chapter_id !== null ? '继续创作' : '开始创作'}</Link>
              : access?.can_read && <Link className="library-write-button" to={readTarget(p)}><BookOutlined />阅读作品</Link>}
            <Dropdown trigger={['click']} menu={{
              items: [
                ...(access?.can_read ? [{ key: 'read', icon: <BookOutlined />, label: readingLabel(p.reading), disabled: !canRead(p) && p.chapter_count === 0 }] : []),
                { key: 'overview', icon: <DashboardOutlined />, label: '作品概览' },
                ...(access?.can_export ? [{ key: 'export', icon: <DownloadOutlined />, label: '导出作品' }] : []),
                ...(access?.can_backup ? [{ key: 'backup', icon: <CloudDownloadOutlined />, label: '备份作品' }] : []),
                ...(access?.can_recycle ? [{ key: 'delete', icon: <DeleteOutlined />, label: '删除作品', danger: true }] : []),
              ],
              onClick: ({ key }) => {
                if (key === 'read') navigate(readTarget(p), { state: { from: '/projects' } })
                else if (key === 'overview') navigate(overview)
                else if (key === 'export') onExport()
                else if (key === 'backup') onBackup()
                else if (key === 'delete') onDelete()
              },
            }}><Button type="text" icon={<MoreOutlined />} aria-label={`《${p.title}》的更多操作`} /></Dropdown>
          </div>
        </div>
      </footer>
    </article>
  )
}
