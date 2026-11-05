import { DeleteOutlined, EditOutlined, EllipsisOutlined, EyeOutlined, PushpinFilled } from '@ant-design/icons'
import { Button, Checkbox, Dropdown, Modal, Popover, Tag, Tooltip } from 'antd'
import { useState, type ReactElement } from 'react'
import type { CharacterCareerBadge } from '../../api/careers'
import { lifeStatusLabel, roleTypeColor } from '../../api/characters'
import type { Character } from '../../api/types'
import { characterAgeLabel } from '../../utils/characterList'

interface Props {
  character: Character
  career?: CharacterCareerBadge
  selectable: boolean
  selected: boolean
  canEdit: boolean
  onSelect: (selected: boolean) => void
  onOpen: () => void
  onDelete: () => Promise<boolean>
}

/** 截断字段的完整内容预览，支持鼠标悬停、键盘聚焦和正文复制。 */
function CharacterTextPreview({ title, text, children }: { title: string; text?: string | null; children: ReactElement }) {
  if (!text) return children
  return <Popover
    title={title}
    content={<p className="character-text-preview-content">{text}</p>}
    classNames={{ root: 'character-text-preview' }}
    placement="top"
    autoAdjustOverflow
    trigger={['hover', 'focus']}
    mouseEnterDelay={0.25}
    mouseLeaveDelay={0.15}
  >{children}</Popover>
}

/** 角色资料卡：身份、别名、属性与状态分层，危险操作收进菜单并二次确认。 */
export default function CharacterCard({ character, career, selectable, selected, canEdit, onSelect, onOpen, onDelete }: Props) {
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const life = lifeStatusLabel(character.life_status)
  const aliases = character.aliases.join(' · ')
  const metadata = [characterAgeLabel(character.age), character.gender.trim()].filter(Boolean).join(' · ')
  const profile = character.profile || character.personality || character.background
  const remove = async () => {
    if (!canEdit || deleting) return
    setDeleting(true)
    try {
      if (await onDelete()) setDeleteOpen(false)
    } finally {
      setDeleting(false)
    }
  }

  return (
    <article className={`character-card${selected ? ' character-card-selected' : ''}`} aria-label={`角色：${character.name}`}>
      <header className="character-card-header">
        {selectable && <Checkbox aria-label={`选择角色：${character.name}`} checked={selected} onChange={(event) => onSelect(event.target.checked)} />}
        <h2 className="character-card-heading">
          <CharacterTextPreview title="角色姓名" text={character.name}>
            <button type="button" className="character-card-name" onClick={onOpen}>{character.name}</button>
          </CharacterTextPreview>
          {character.role_type && <CharacterTextPreview title="角色定位" text={character.role_type}>
            <Tag className="character-card-role" color={roleTypeColor(character.role_type)}>{character.role_type}</Tag>
          </CharacterTextPreview>}
        </h2>
        <div className="character-card-actions">
          <Tooltip title={canEdit ? '编辑角色' : '查看角色'} classNames={{ root: 'character-action-tooltip' }}>
            <Button type="text" size="small" icon={canEdit ? <EditOutlined /> : <EyeOutlined />} aria-label={`${canEdit ? '编辑' : '查看'}角色：${character.name}`} onClick={onOpen} />
          </Tooltip>
          <Dropdown trigger={['click']} menu={{
            items: [{ key: 'delete', label: '删除角色', icon: <DeleteOutlined />, danger: true, disabled: !canEdit }],
            onClick: ({ key }) => { if (key === 'delete' && canEdit) setDeleteOpen(true) },
          }}>
            <Button type="text" size="small" icon={<EllipsisOutlined />} aria-label={`更多角色操作：${character.name}`} />
          </Dropdown>
        </div>
      </header>
      <CharacterTextPreview title="角色别名" text={aliases}>
        <p className="character-card-aliases" tabIndex={aliases ? 0 : undefined}>{aliases || '暂无别名'}</p>
      </CharacterTextPreview>
      <div className="character-card-meta">
        <CharacterTextPreview title="年龄与性别" text={metadata}>
          <span className="character-card-demographics" tabIndex={metadata ? 0 : undefined}>{metadata || '年龄、性别未设定'}</span>
        </CharacterTextPreview>
        {character.life_status !== 'alive' && <Tag color={life.color}>{life.label}</Tag>}
        {character.always_in_context && <span className="character-card-pinned"><PushpinFilled />常驻上下文</span>}
      </div>
      <div className="character-card-body">
        {career && (career.main || career.subs.length > 0) && <div className="character-card-careers">
          {career.main && <CharacterTextPreview title="主职业" text={career.main}>
            <p tabIndex={0}><span>主职业</span>{career.main}</p>
          </CharacterTextPreview>}
          {career.subs.length > 0 && <CharacterTextPreview title="副职业" text={career.subs.join('；')}>
            <p tabIndex={0}><span>副职业</span>{career.subs.join('；')}</p>
          </CharacterTextPreview>}
        </div>}
        <CharacterTextPreview title="角色简介" text={profile}>
          <p className={`character-card-profile${profile ? '' : ' character-card-placeholder'}`} tabIndex={profile ? 0 : undefined}>
            {profile || '尚未填写角色简介'}
          </p>
        </CharacterTextPreview>
      </div>
      <div className="character-card-state">
        <h3>当前状态</h3>
        <CharacterTextPreview title="当前状态" text={character.current_state}>
          <p className={character.current_state ? undefined : 'character-card-placeholder'} tabIndex={character.current_state ? 0 : undefined}>
            {character.current_state || '尚未填写当前状态'}
          </p>
        </CharacterTextPreview>
      </div>
      {deleteOpen && <Modal
        title={`删除角色「${character.name}」？`}
        open={canEdit}
        okText="删除角色"
        cancelText="取消"
        okButtonProps={{ danger: true }}
        confirmLoading={deleting}
        maskClosable={!deleting}
        closable={!deleting}
        cancelButtonProps={{ disabled: deleting }}
        onCancel={() => setDeleteOpen(false)}
        onOk={() => void remove()}
      >
        <p>相关的人物关系、关系历史、组织成员记录、职业挂接与阶段历史会一并删除。</p>
      </Modal>}
    </article>
  )
}
