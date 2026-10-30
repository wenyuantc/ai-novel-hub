import { PlusOutlined, RobotOutlined } from '@ant-design/icons'
import { App, Button, Card, Empty, Popconfirm, Space, Table, Tag, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { errorMessage } from '../../api/client'
import { intimacyColor, relationshipApi, relationshipTypeApi, statusLabel } from '../../api/relationships'
import type { Character, Relationship } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import RelationEditModal from '../relations/RelationEditModal'
import RelationHistoryModal from '../relations/RelationHistoryModal'
import EntityTaskModal from './EntityTaskModal'
import { useProjectAccess } from '../../hooks/useProjectAccess'

interface Props {
  projectId: number
  character: Character
  characters: Character[]
}

/** 角色抽屉「关系」Tab：该角色视角的关系列表（增删改、历史）+ 旧关系备注只读块与「AI 解析为关系」。 */
export default function CharacterRelationsTab({ projectId, character, characters }: Props) {
  const { canUseAI } = useProjectAccess(projectId)
  const { message } = App.useApp()
  const loader = useCallback(() => relationshipApi.list(projectId, character.id), [projectId, character.id])
  const { data: rels = [], loading, reload } = useLoader(loader, { projectId, resources: ['relationships', 'characters'] })
  const typeLoader = useCallback(() => relationshipTypeApi.list(projectId), [projectId])
  const { data: types = [] } = useLoader(typeLoader, { projectId, resources: ['relationships', 'characters'] })
  const [editing, setEditing] = useState<Relationship | null>(null)
  const [editOpen, setEditOpen] = useState(false)
  const [history, setHistory] = useState<Relationship | null>(null)
  const [parseOpen, setParseOpen] = useState(false)

  const handleDelete = async (r: Relationship) => {
    try {
      await relationshipApi.remove(projectId, r.id)
      message.success('已删除')
      reload()
    } catch (err) {
      message.error(errorMessage(err))
    }
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Space>
        <Button
          icon={<PlusOutlined />}
          onClick={() => {
            setEditing(null)
            setEditOpen(true)
          }}
        >
          新增关系
        </Button>
      </Space>
      <Table<Relationship>
        size="small"
        rowKey="id"
        loading={loading}
        dataSource={rels}
        pagination={false}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无结构化关系" /> }}
        columns={[
          {
            title: '关系',
            render: (_, r) => (
              <span>
                <Typography.Text strong>{r.other_role}</Typography.Text> {r.other_name}
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {' '}
                  （我是{r.self_role}）
                </Typography.Text>
              </span>
            ),
          },
          {
            title: '亲密度',
            width: 70,
            render: (_, r) => <span style={{ color: intimacyColor(r.intimacy), fontWeight: 600 }}>{r.intimacy}</span>,
          },
          { title: '状态', width: 70, render: (_, r) => <Tag color={statusLabel(r.status).color}>{statusLabel(r.status).label}</Tag> },
          {
            title: '描述',
            render: (_, r) => (
              <Typography.Paragraph style={{ marginBottom: 0 }} ellipsis={{ rows: 2, expandable: true, symbol: '展开' }}>
                {r.description || '-'}
              </Typography.Paragraph>
            ),
          },
          {
            title: '',
            width: 150,
            render: (_, r) => (
              <Space size={0}>
                <Button
                  type="link"
                  size="small"
                  onClick={() => {
                    setEditing(r)
                    setEditOpen(true)
                  }}
                >
                  编辑
                </Button>
                <Button type="link" size="small" onClick={() => setHistory(r)}>
                  历史
                </Button>
                <Popconfirm title="删除该关系？" okText="删除" cancelText="取消" onConfirm={() => handleDelete(r)}>
                  <Button type="link" size="small" danger>
                    删除
                  </Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
      {character.relations_text.trim() && (
        <Card
          size="small"
          title="旧关系备注（只读）"
          extra={canUseAI && (
            <Button size="small" icon={<RobotOutlined />} onClick={() => setParseOpen(true)}>
              AI 解析为关系
            </Button>
          )}
        >
          <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>{character.relations_text}</Typography.Paragraph>
        </Card>
      )}
      <RelationEditModal
        projectId={projectId}
        open={editOpen}
        characters={characters}
        types={types}
        viewerId={character.id}
        editing={editing}
        onClose={() => setEditOpen(false)}
        onSaved={() => {
          setEditOpen(false)
          reload()
        }}
      />
      {history && <RelationHistoryModal projectId={projectId} relationship={history} onClose={() => setHistory(null)} />}
      {canUseAI && (<EntityTaskModal
        projectId={projectId}
        kind="relations.parse"
        open={parseOpen}
        characterId={character.id}
        characterName={character.name}
        onClose={() => setParseOpen(false)}
        onSaved={reload}
      />)}
    </Space>
  )
}
