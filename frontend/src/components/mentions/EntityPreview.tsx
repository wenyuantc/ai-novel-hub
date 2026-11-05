import { Alert, Button, Drawer, Space, Spin, Tag, Typography } from 'antd'
import { useCallback } from 'react'
import { Link } from 'react-router-dom'
import { mentionApi, MENTION_CATEGORY_LABELS, type MentionEntity } from '../../api/mentions'
import { useMentionQuery } from '../../hooks/useMentionQuery'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { entityHref } from '../../utils/mentions'

export default function EntityPreview({ projectId, entity, onClose }: { projectId: number; entity: MentionEntity; onClose: () => void }) {
  const load = useCallback(async (signal: AbortSignal) => {
    if (entity.kind === 'character') {
      const item = await mentionApi.character(projectId, entity.id, signal)
      return { name: item.name, aliases: item.aliases, text: [item.profile, item.appearance, item.personality, item.background].filter(Boolean).join('\n\n') }
    }
    const item = await mentionApi.world(projectId, entity.id, signal)
    return { name: item.title, aliases: item.aliases ?? [], text: item.content }
  }, [projectId, entity.kind, entity.id])
  const result = useMentionQuery(projectId, `${entity.kind}:${entity.id}`, load)
  useProjectRefresh(projectId, ['characters', 'world'], result.reload)
  return <Drawer open title="设定预览" width={480} onClose={onClose}>
    {result.loading && <Spin />}
    {result.error && <Alert type="error" showIcon message={result.error} action={<Button onClick={result.reload}>重试</Button>} />}
    {result.data && <Space direction="vertical" style={{ width: '100%' }}>
      <Typography.Title level={4} style={{ margin: 0 }}>{result.data.name}</Typography.Title>
      <Tag>{MENTION_CATEGORY_LABELS[entity.category]}</Tag>
      {!!result.data.aliases.length && <Typography.Text type="secondary">别名：{result.data.aliases.join('、')}</Typography.Text>}
      <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{result.data.text || '暂无描述'}</Typography.Paragraph>
      <Link to={entityHref(projectId, entity)} onClick={onClose}>查看完整设定与提及</Link>
    </Space>}
  </Drawer>
}
