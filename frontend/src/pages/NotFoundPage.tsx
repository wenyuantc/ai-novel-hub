import { Result } from 'antd'
import { Link } from 'react-router-dom'

export default function NotFoundPage() {
  return <Result status="404" title="页面不存在" extra={<Link to="/projects">返回首页</Link>} />
}
