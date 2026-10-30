import { Alert, Button } from 'antd'
import { Link } from 'react-router-dom'
import type { ProjectOwner } from '../../api/types'

export default function ProjectManagementNotice({ owner }: { owner: ProjectOwner }) {
  return <Alert type="info" showIcon style={{ marginBottom: 16 }} message={`管理员正在管理「${owner.username}」的项目`} description="修改将直接保存到该项目。当前提供人工内容管理。" action={<Link to="/admin/projects"><Button size="small">返回作品管理</Button></Link>} />
}
