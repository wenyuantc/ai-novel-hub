import { Empty, List, Space, Spin, Tag, Typography } from 'antd'
import { useCallback } from 'react'
import { Link } from 'react-router-dom'
import { FORESHADOW_STATUS, foreshadowApi, foreshadowChapterSummary } from '../../api/foreshadows'
import type { Character } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'

interface Props {
  projectId: number
  character: Character
}

/** 角色抽屉「伏笔」Tab：与该角色关联的伏笔（只读），点击跳转伏笔页打开编辑。 */
export default function CharacterForeshadowsTab({ projectId, character }: Props) {
  const loader = useCallback(
    () => foreshadowApi.list(projectId, { character_id: character.id }),
    [projectId, character.id],
  )
  const { data: items = [], loading, error } = useLoader(loader)

  return (
    <Spin spinning={loading}>
      {error && <Typography.Text type="danger">{error}</Typography.Text>}
      {items.length === 0 && !loading ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有关联该角色的伏笔" />
      ) : (
        <List
          size="small"
          dataSource={items}
          renderItem={(f) => {
            const st = FORESHADOW_STATUS[f.status]
            const ch = foreshadowChapterSummary(f)
            return (
              <List.Item key={f.id}>
                <Space direction="vertical" size={0} style={{ minWidth: 0 }}>
                  <Space size={4} wrap>
                    <Link to={`/projects/${projectId}/foreshadows?id=${f.id}`}>
                      #{f.id} {f.title}
                    </Link>
                    <Tag color={st.color}>{st.label}</Tag>
                    {f.overdue_by > 0 && <Tag color="red">超期 {f.overdue_by} 章</Tag>}
                    {f.is_long_term && <Tag color="purple">长线</Tag>}
                  </Space>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    埋：{ch.plant} · 收：{ch.resolve}
                  </Typography.Text>
                </Space>
              </List.Item>
            )
          }}
        />
      )}
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        <Link to={`/projects/${projectId}/foreshadows?character_id=${character.id}`}>在伏笔页查看全部</Link>
      </Typography.Text>
    </Spin>
  )
}
