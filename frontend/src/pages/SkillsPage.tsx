import { PlusOutlined, UploadOutlined } from '@ant-design/icons'
import { Alert, App, Button, Col, Empty, Row, Space, Spin, Tabs, Typography, Upload } from 'antd'
import { useMemo, useState } from 'react'
import { errorMessage } from '../api/client'
import { SKILL_LIMITS, skillApi } from '../api/skills'
import type { Skill, SkillBrief, SkillScope } from '../api/types'
import SkillCard, { type SkillAction } from '../components/skills/SkillCard'
import SkillEditDrawer from '../components/skills/SkillEditDrawer'
import { useIsMobile } from '../hooks/useIsMobile'
import { useAuthStore } from '../stores/auth'
import { useSkillStore, useSkills } from '../stores/skills'
import { splitSkills } from '../utils/skills'

type Editing = { id: number | null; scope: SkillScope } | null

/** Skill 库：我的 / 全局两个 Tab；全局 Skill 普通用户只读（可复制为我的），管理员可增删改并恢复内置内容；SKILL.md / zip 导入导出。 */
export default function SkillsPage() {
  const { message, modal } = App.useApp()
  const isMobile = useIsMobile()
  const isAdmin = useAuthStore((s) => s.user?.is_admin ?? false)
  const { skills, loading, error } = useSkills()
  const reload = useSkillStore((s) => s.load)
  const { mine, global } = useMemo(() => splitSkills(skills), [skills])
  const [tab, setTab] = useState<SkillScope>('mine')
  const [editing, setEditing] = useState<Editing>(null)
  const [busy, setBusy] = useState(false)
  const [importing, setImporting] = useState(false)
  // 全局 Tab 上，管理员新建 / 导入的 Skill 归入全局
  const targetScope: SkillScope = tab === 'global' && isAdmin ? 'global' : 'mine'
  const canCreate = tab === 'mine' || isAdmin

  const refresh = () => reload(true)

  const run = async (operation: () => Promise<void>) => {
    setBusy(true)
    try {
      await operation()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const copy = (s: Pick<SkillBrief, 'id'>) =>
    run(async () => {
      const copied = await skillApi.copy(s.id)
      message.success(`已复制为「${copied.name}」`)
      await refresh()
      setTab('mine')
      setEditing({ id: copied.id, scope: 'mine' })
    })

  const onAction = (s: SkillBrief, action: SkillAction) => {
    if (action === 'open') setEditing({ id: s.id, scope: s.scope })
    else if (action === 'copy') void copy(s)
    else if (action === 'export')
      void run(async () => {
        const filename = await skillApi.export(s.id)
        message.success(`已导出 ${filename}`)
      })
    else if (action === 'restore')
      void run(async () => {
        await skillApi.restoreBuiltin(s.id)
        message.success('已恢复内置内容')
        await refresh()
      })
    else if (action === 'delete')
      modal.confirm({
        title: `删除 Skill「${s.name}」？`,
        content: s.scope === 'global' ? '这是全局 Skill，删除后所有用户都无法再使用。' : '删除后无法恢复。已经创建的任务仍使用当时的快照。',
        okText: '删除',
        okButtonProps: { danger: true },
        onOk: async () => {
          try {
            await skillApi.remove(s.id)
            message.success('已删除')
            void refresh()
          } catch (err) {
            message.error(errorMessage(err))
            throw err
          }
        },
      })
  }

  const importFile = async (file: File) => {
    if (file.size > SKILL_LIMITS.uploadBytes) {
      message.error('Skill 文件过大，上限为 2MB')
      return
    }
    setImporting(true)
    try {
      const created: Skill = await skillApi.import(file, targetScope)
      message.success(`已导入「${created.name}」`)
      await refresh()
      setEditing({ id: created.id, scope: created.scope })
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setImporting(false)
    }
  }

  const grid = (list: SkillBrief[], empty: string) =>
    list.length ? (
      <Row gutter={[16, 16]}>
        {list.map((s) => (
          <Col xs={24} md={12} xl={8} key={s.id}>
            <SkillCard skill={s} isAdmin={isAdmin} busy={busy} onAction={onAction} />
          </Col>
        ))}
      </Row>
    ) : (
      <Empty description={empty} />
    )

  return (
    <div style={{ padding: isMobile ? 12 : 24, maxWidth: 1400, margin: '0 auto' }}>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }} wrap>
        <div>
          <Typography.Title level={3} style={{ margin: 0 }}>
            Skill
          </Typography.Title>
          <Typography.Text type="secondary">
            可复用的写作工作流与参考资料。智能体按需加载，输入 /触发词 可强制启用；章节生成、润色、重写和批量生成也可以选一个作为补充指令。
          </Typography.Text>
        </div>
        {canCreate && (
          <Space wrap>
            <Upload
              accept=".md,.zip"
              showUploadList={false}
              beforeUpload={(file) => {
                void importFile(file)
                return false
              }}
            >
              <Button icon={<UploadOutlined />} loading={importing}>
                导入 SKILL.md / zip
              </Button>
            </Upload>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setEditing({ id: null, scope: targetScope })}>
              {targetScope === 'global' ? '新建全局 Skill' : '新建 Skill'}
            </Button>
          </Space>
        )}
      </Space>
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}
      {loading && skills.length === 0 ? (
        <Spin style={{ display: 'block', margin: 60 }} />
      ) : (
        <Tabs
          activeKey={tab}
          onChange={(k) => setTab(k as SkillScope)}
          items={[
            { key: 'mine', label: `我的（${mine.length}）`, children: grid(mine, '还没有自己的 Skill：新建、导入，或复制一个全局 Skill 后修改') },
            {
              key: 'global',
              label: `全局（${global.length}）`,
              children: (
                <Space direction="vertical" style={{ width: '100%' }}>
                  {!isAdmin && <Typography.Text type="secondary">全局 Skill 由管理员维护，只读；复制为我的之后可以修改。</Typography.Text>}
                  {grid(global, '还没有全局 Skill')}
                </Space>
              ),
            },
          ]}
        />
      )}
      <SkillEditDrawer
        open={editing !== null}
        skillId={editing?.id ?? null}
        scope={editing?.scope ?? 'mine'}
        isAdmin={isAdmin}
        onClose={() => setEditing(null)}
        onSaved={(s) => {
          void refresh()
          setEditing({ id: s.id, scope: s.scope })
        }}
        onCopy={(s) => void copy(s)}
      />
    </div>
  )
}
