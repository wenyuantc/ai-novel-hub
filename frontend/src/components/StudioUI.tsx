import { BookOutlined } from '@ant-design/icons'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import type { ProjectStatus } from '../api/types'
import { PROJECT_STATUS } from '../api/projects'
import './StudioUI.css'

/** 官网与工作区共用品牌，始终返回公开首页。 */
export function StudioBrand({ onClick }: { onClick?: () => void }) {
  return <Link to="/" className="studio-brand" aria-label="知页小说首页" onClick={onClick}><BookOutlined aria-hidden /><span>知页<small>小说</small></span></Link>
}

/** 设计稿的页面标题与内容容器；表单、按钮和弹窗继续使用 Ant Design。 */
export function StudioPage({ title, hint, action, children }: { title: string; hint?: string; action?: ReactNode; children: ReactNode }) {
  return (
    <main className="studio-page">
      <div className="studio-page-heading">
        <div><h1>{title}</h1>{hint && <p>{hint}</p>}</div>
        {action && <div className="studio-page-action">{action}</div>}
      </div>
      {children}
    </main>
  )
}

/** 普通纸面面板，无静态阴影。 */
export function StudioPanel({ children }: { children: ReactNode }) {
  return <section className="studio-panel">{children}</section>
}

/** 带说明和下一步操作的空状态。 */
export function StudioEmpty({ title, text, action }: { title: string; text: string; action?: ReactNode }) {
  return <div className="studio-empty" role="status"><BookOutlined aria-hidden /><h2>{title}</h2><p>{text}</p>{action}</div>
}

/** 作品状态统一沿用真实 API 枚举与既有中文标签。 */
export function ProjectBadge({ status }: { status: ProjectStatus }) {
  return <span className={`studio-badge studio-badge-${status}`}>{PROJECT_STATUS[status].label}</span>
}
