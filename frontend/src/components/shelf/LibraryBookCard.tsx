import { BookOutlined, CloudDownloadOutlined, DashboardOutlined, DeleteOutlined, DownloadOutlined, EditOutlined, MoreOutlined } from '@ant-design/icons'
import { Button, Dropdown, Progress } from 'antd'
import { Link, useNavigate } from 'react-router-dom'
import { coverUrl } from '../../api/projects'
import type { Project } from '../../api/types'
import { canRead, continueTarget, readingLabel, readTarget } from '../../utils/shelf'
import { wordsProgress } from '../../utils/projectFields'
import { formatRelative, formatTime } from '../../utils/time'
import { ProjectBadge } from '../StudioUI'
import BookCover from './BookCover'

interface Props { project: Project; author?: string; onExport: () => void; onBackup: () => void; onDelete: () => void }

/** 书库中共享的卡片/列表条目，全部操作复用已有作品路由和接口。 */
export default function LibraryBookCard({ project: p, author, onExport, onBackup, onDelete }: Props) {
  const navigate = useNavigate()
  const overview = `/projects/${p.id}/overview`
  const progress = wordsProgress(p.total_words, p.target_words)
  return (
    <article className="library-book" aria-labelledby={`library-book-title-${p.id}`}>
      <div className="library-book-main">
        <Link className="library-book-cover" to={overview} aria-label={`打开《${p.title}》`}><BookCover title={p.title} genre={p.genre} author={author} coverUrl={coverUrl(p)} /></Link>
        <div className="library-book-details">
          <div className="library-book-status"><ProjectBadge status={p.status} /></div>
          <h2 className="library-book-heading" id={`library-book-title-${p.id}`}><Link to={overview} title={p.title}>{p.title}</Link></h2>
          {p.series && <Link className="library-book-series" to={`/series/${p.series.id}`} title={`所属系列：${p.series.name}`}>系列：{p.series.name}</Link>}
          <p className="library-book-description" title={p.synopsis || p.premise}>{p.synopsis || p.premise || '故事尚在构思，写下第一段简介，让这个世界慢慢清晰。'}</p>
          <time className="library-book-updated" dateTime={p.updated_at} title={formatTime(p.updated_at)}>更新于 {formatRelative(p.updated_at)}</time>
        </div>
      </div>
      {progress !== null && <div className="library-book-progress" title={`目标 ${p.target_words?.toLocaleString()} 字`}><Progress percent={progress} showInfo={false} size="small" /></div>}
      <footer className="library-book-footer">
        <div className="library-book-summary">
          <div className="library-book-meta"><span>{p.chapter_count} 章</span><span>{p.total_words.toLocaleString()} 字</span></div>
          <div className="library-book-actions">
            <Link className="library-write-button" to={continueTarget(p)}><EditOutlined />{p.last_edited_chapter_id !== null ? '继续创作' : '开始创作'}</Link>
            <Dropdown trigger={['click']} menu={{
              items: [
                { key: 'read', icon: <BookOutlined />, label: readingLabel(p.reading), disabled: !canRead(p) },
                { key: 'overview', icon: <DashboardOutlined />, label: '作品概览' },
                { key: 'export', icon: <DownloadOutlined />, label: '导出作品' },
                { key: 'backup', icon: <CloudDownloadOutlined />, label: '备份作品' },
                { type: 'divider' },
                { key: 'delete', icon: <DeleteOutlined />, label: '删除作品', danger: true },
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
