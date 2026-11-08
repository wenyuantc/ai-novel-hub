import { useAuthorizationScope } from '../hooks/useAuthorizationScope'
import { can, authorizationKey } from '../utils/permissions'
import { useAuthorizationCleanup } from '../hooks/useAuthorizationCleanup'
import { PlusOutlined, UploadOutlined } from '@ant-design/icons'
import { Alert, App, Button, Col, Empty, Row, Space, Spin, Tabs, Typography, Upload } from 'antd'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
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
  const actor = useAuthStore((s) => s.user)
  const personal = useAuthorizationScope(undefined, false, false)
  const management = useAuthorizationScope()
  const isAdmin = can(actor, 'global_skills.create')
  const confirmation = useRef<{ destroy: () => void; global: boolean } | null>(null)
  const { skills, loading, error } = useSkills()
  const reload = useSkillStore((s) => s.load)
  const { mine, global } = useMemo(() => splitSkills(skills), [skills])
  const [tab, setTab] = useState<SkillScope>('mine')
  // 从组件库的使用关系跳转过来（?skill=id，只会是我的 Skill）：挂载时打开抽屉，随后去掉参数
  const [searchParams, setSearchParams] = useSearchParams()
  const linkedSkill = searchParams.get('skill')
  const [editing, setEditing] = useState<Editing>(() =>
    linkedSkill && /^\d+$/.test(linkedSkill) ? { id: Number(linkedSkill), scope: 'mine' } : null,
  )
  useEffect(() => {
    if (linkedSkill) setSearchParams({}, { replace: true })
  }, [linkedSkill, setSearchParams])
  useAuthorizationCleanup(() => {
    if (useAuthStore.getState().user?.id !== actor?.id || editing?.scope === 'global') setEditing(null)
    if (confirmation.current?.global || useAuthStore.getState().user?.id !== actor?.id) { confirmation.current?.destroy(); confirmation.current = null }
  })
  useEffect(() => () => { confirmation.current?.destroy() }, [])
  const [busy, setBusy] = useState(false)
  const [importing, setImporting] = useState(false)
  const pending = useRef(false)
  const importPending = useRef(false)
  const globalOperation = useRef(false)
  const globalImport = useRef(false)
  useAuthorizationCleanup(() => {
    if (globalOperation.current || useAuthStore.getState().user?.id !== actor?.id) { pending.current = false; setBusy(false) }
    if (globalImport.current || useAuthStore.getState().user?.id !== actor?.id) { importPending.current = false; setImporting(false) }
  })
  // 全局 Tab 上，管理员新建 / 导入的 Skill 归入全局
  const targetScope: SkillScope = tab === 'global' && isAdmin ? 'global' : 'mine'
  const canCreate = tab === 'mine' || isAdmin

  const refresh = () => reload(true)

  const run = async (operation: (current: () => boolean) => Promise<void>, permission?: Parameters<typeof can>[1]) => {
    const scope = permission ? management : personal
    const current = () => scope.allowed() && (!permission || can(useAuthStore.getState().user, permission))
    if (!current() || pending.current) return
    pending.current = true
    globalOperation.current = !!permission
    setBusy(true)
    try {
      await operation(current)
    } catch (err) {
      if (current()) message.error(errorMessage(err))
    } finally {
      if (current()) { pending.current = false; setBusy(false) }
    }
  }

  const copy = (s: Pick<SkillBrief, 'id'>) =>
    run(async (current) => {
      const copied = await skillApi.copy(s.id)
      if (!current()) return
      message.success(`已复制为「${copied.name}」`)
      await refresh()
      if (!current()) return
      setTab('mine')
      setEditing({ id: copied.id, scope: 'mine' })
    })

  const onAction = (s: SkillBrief, action: SkillAction) => {
    if (action === 'open') setEditing({ id: s.id, scope: s.scope })
    else if (action === 'copy') void copy(s)
    else if (action === 'export')
      void run(async (current) => {
        const filename = await skillApi.export(s.id)
        if (!current()) return
        message.success(`已导出 ${filename}`)
      })
    else if (action === 'restore' && can(useAuthStore.getState().user, 'global_skills.restore_builtin'))
      void run(async (current) => {
        await skillApi.restoreBuiltin(s.id)
        if (!current()) return
        message.success('已恢复内置内容')
        await refresh()
      }, 'global_skills.restore_builtin')
    else if (action === 'delete' && (s.scope !== 'global' || can(useAuthStore.getState().user, 'global_skills.delete'))) {
      const identity = `${actor?.id}:${s.scope === 'global' ? authorizationKey(actor) : ''}`
      const current = () => personal.allowed() && `${useAuthStore.getState().user?.id}:${s.scope === 'global' ? authorizationKey(useAuthStore.getState().user) : ''}` === identity && (s.scope !== 'global' || can(useAuthStore.getState().user, 'global_skills.delete'))
      confirmation.current?.destroy()
      confirmation.current = { global: s.scope === 'global', ...modal.confirm({
        title: `删除 Skill「${s.name}」？`,
        content: s.scope === 'global' ? '这是全局 Skill，删除后所有用户都无法再使用。' : '删除后无法恢复。已经创建的任务仍使用当时的快照。',
        okText: '删除',
        okButtonProps: { danger: true },
        onOk: async () => {
          if (!current()) return
          try {
            await skillApi.remove(s.id)
            if (!current()) return
            message.success('已删除')
            void refresh()
          } catch (err) {
            if (current()) message.error(errorMessage(err))
            throw err
          }
        },
      }) }
    }
  }

  const importFile = async (file: File) => {
    const scope = targetScope === 'global' ? management : personal
    const current = () => scope.allowed() && (targetScope !== 'global' || can(useAuthStore.getState().user, 'global_skills.create'))
    if (importPending.current || !current()) return
    if (file.size > SKILL_LIMITS.uploadBytes) {
      message.error('Skill 文件过大，上限为 2MB')
      return
    }
    importPending.current = true
    globalImport.current = targetScope === 'global'
    setImporting(true)
    try {
      const created: Skill = await skillApi.import(file, targetScope)
      if (!current()) return
      message.success(`已导入「${created.name}」`)
      await refresh()
      if (!current()) return
      setEditing({ id: created.id, scope: created.scope })
    } catch (err) {
      if (current()) message.error(errorMessage(err))
    } finally {
      if (current()) { importPending.current = false; setImporting(false) }
    }
  }

  const grid = (list: SkillBrief[], empty: string) =>
    list.length ? (
      <Row gutter={[16, 16]}>
        {list.map((s) => (
          <Col xs={24} md={12} xl={8} key={s.id}>
            <SkillCard skill={s} authorization={actor} busy={busy} onAction={onAction} />
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
