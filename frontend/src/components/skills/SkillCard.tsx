import { CopyOutlined, DeleteOutlined, DownloadOutlined, EditOutlined, EyeOutlined, RollbackOutlined } from '@ant-design/icons'
import { Button, Card, Popconfirm, Space, Tag, Typography } from 'antd'
import type { SkillBrief } from '../../api/types'

export type SkillAction = 'open' | 'copy' | 'export' | 'delete' | 'restore'

interface Props {
  skill: SkillBrief
  isAdmin: boolean
  busy?: boolean
  onAction: (skill: SkillBrief, action: SkillAction) => void
}

/**
 * Skill 卡片：可写（editable，我的或管理员看全局）显示编辑 / 删除；
 * 只读的全局 Skill 只能查看、复制为我的、导出。内置 Skill 对管理员另有「恢复内置内容」。
 */
export default function SkillCard({ skill, isAdmin, busy, onAction }: Props) {
  const readOnly = !skill.editable
  const actions = [
    <Button
      key="open"
      type="link"
      size="small"
      icon={readOnly ? <EyeOutlined /> : <EditOutlined />}
      onClick={() => onAction(skill, 'open')}
    >
      {readOnly ? '查看' : '编辑'}
    </Button>,
    <Button key="copy" type="link" size="small" icon={<CopyOutlined />} disabled={busy} onClick={() => onAction(skill, 'copy')}>
      {skill.scope === 'global' ? '复制为我的' : '复制'}
    </Button>,
    <Button key="export" type="link" size="small" icon={<DownloadOutlined />} onClick={() => onAction(skill, 'export')}>
      导出
    </Button>,
  ]
  if (skill.editable && skill.builtin_key && isAdmin)
    actions.push(
      <Popconfirm
        key="restore"
        title="恢复内置内容？"
        description="正文与参考资料会被替换为系统自带的版本。"
        onConfirm={() => onAction(skill, 'restore')}
      >
        <Button type="link" size="small" icon={<RollbackOutlined />} disabled={busy}>
          恢复内置
        </Button>
      </Popconfirm>,
    )
  if (skill.editable)
    actions.push(
      <Button key="delete" type="link" size="small" danger icon={<DeleteOutlined />} disabled={busy} onClick={() => onAction(skill, 'delete')}>
        删除
      </Button>,
    )
  return (
    <Card
      size="small"
      style={{ height: '100%' }}
      title={
        <Space wrap size={4} style={{ whiteSpace: 'normal' }}>
          <span style={{ overflowWrap: 'anywhere' }}>{skill.name}</span>
          {skill.scope === 'global' && <Tag color="purple">全局</Tag>}
          {skill.builtin_key && <Tag>内置</Tag>}
          {readOnly && <Tag>只读</Tag>}
        </Space>
      }
      actions={actions}
      styles={{ actions: { flexWrap: 'wrap' } }}
    >
      <Typography.Paragraph type={skill.description ? undefined : 'secondary'} ellipsis={{ rows: 2 }}>
        {skill.description || '（没有说明）'}
      </Typography.Paragraph>
      <Space wrap size={[4, 4]}>
        <Typography.Text type="secondary" style={{ fontSize: 12, overflowWrap: 'anywhere' }}>
          {skill.slug}
        </Typography.Text>
        {skill.category && <Tag color="blue">{skill.category}</Tag>}
        {skill.triggers.map((t) => (
          <Tag key={t}>/{t}</Tag>
        ))}
      </Space>
      <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 4 }}>
        正文 {skill.body_chars} 字 · 参考资料 {skill.reference_titles.length} 篇
      </Typography.Text>
    </Card>
  )
}
