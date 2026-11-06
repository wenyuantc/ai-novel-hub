import { Alert, Descriptions, Space } from 'antd'
import type { CardImportResult } from '../../api/types'

/** 只在服务端整笔成功后展示导入计数。 */
export default function CardImportResultView({ result }: { result: CardImportResult }) {
  return <Space direction="vertical" style={{ width: '100%' }}>
    <Descriptions size="small" column={{ xs: 1, sm: 2 }} bordered items={[
      { key: 'characters', label: '新建角色', children: result.characters.filter((item) => item.action !== 'skipped').length },
      { key: 'skipped-characters', label: '跳过角色', children: result.characters.filter((item) => item.action === 'skipped').length },
      { key: 'organizations', label: '新建组织', children: result.organizations.filter((item) => item.action !== 'skipped').length },
      { key: 'skipped-organizations', label: '跳过组织', children: result.organizations.filter((item) => item.action === 'skipped').length },
      { key: 'relationships', label: '人物关系', children: result.relationships_created },
      { key: 'members', label: '组织成员', children: result.members_created },
      { key: 'careers', label: '新建职业', children: result.careers.filter((item) => item.action === 'created').length },
      { key: 'reused-careers', label: '复用职业', children: result.careers.filter((item) => item.action === 'reused').length },
      { key: 'stages', label: '追加职业阶段', children: result.careers.reduce((count, item) => count + item.stages_added, 0) },
      { key: 'assignments', label: '职业挂接', children: result.careers_assigned },
    ]} />
    {result.warnings.map((warning, index) => <Alert key={index} type="warning" showIcon message={warning} />)}
  </Space>
}
