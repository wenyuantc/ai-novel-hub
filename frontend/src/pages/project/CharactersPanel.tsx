import { AppstoreOutlined, DownloadOutlined, DownOutlined, PlusOutlined, RobotOutlined, SearchOutlined, UnorderedListOutlined, UploadOutlined } from '@ant-design/icons'
import {
  Alert,
  App,
  Button,
  Checkbox,
  Drawer,
  Dropdown,
  Empty,
  Form,
  Input,
  Pagination,
  Popconfirm,
  Spin,
  Tabs,
  Typography,
} from 'antd'
import { lazy, Suspense, useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { assignmentApi, careerBadges } from '../../api/careers'
import { ApiError, errorMessage } from '../../api/client'
import { baselineEditorLabel, progressionApi } from '../../api/progressions'
import { characterApi } from '../../api/story'
import type { Character, SettingBaseline } from '../../api/types'
import CharacterCareersTab from '../../components/careers/CharacterCareersTab'
import CardExportModal from '../../components/entities/CardExportModal'
import CardImportModal from '../../components/entities/CardImportModal'
import CharacterCard from '../../components/entities/CharacterCard'
import {
  characterBaselineMatches,
  characterToBaselineForm,
  createCharacterDraftCache,
  createCharacterRequestGate,
  EMPTY_CHARACTER,
  formToCharacterInput,
  formToCharacterPatch,
  type CharacterEditorTicket,
  type CharacterFormValues,
} from '../../components/entities/characterForm'
import { CharacterBasicFields, CharacterDescFields, CharacterStateFields } from '../../components/entities/CharacterFormFields'
import CharacterMembershipsTab from '../../components/entities/CharacterMembershipsTab'
import CharacterRelationsTab from '../../components/entities/CharacterRelationsTab'
import EntityTaskModal from '../../components/entities/EntityTaskModal'
import SettingProgressionsPanel from '../../components/progressions/SettingProgressionsPanel'
import WizardModal from '../../components/WizardModal'
import { usePublishAgentContext } from '../../hooks/useAgentPageContext'
import { useLoader } from '../../hooks/useLoader'
import { useProjectId } from '../../hooks/useProjectId'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'
import { authorizationKey } from '../../utils/permissions'
import { invalidateProject } from '../../utils/projectRefresh'
import { useEntityDeepLink } from '../../hooks/useEntityDeepLink'
import EntityMentions from '../../components/mentions/EntityMentions'
import { ALL_CHARACTERS, characterPage, characterRoleFilters, filterCharacters } from '../../utils/characterList'
import './CharactersPanel.css'

// 伏笔 Tab 只在打开角色抽屉并切到该 Tab 时才需要：按需加载，伏笔页的 API / 标签代码不进入口 chunk
const CharacterForeshadowsTab = lazy(() => import('../../components/foreshadows/CharacterForeshadowsTab'))

/** 角色：卡片列表（按定位筛选、勾选导出、主职业·阶段）+ 分 Tab 编辑抽屉（基础 / 描写 / 状态 / 关系 / 组织 / 职业 / 伏笔）。 */
export default function CharactersPanel() {
  const pid = useProjectId()
  const { canUseAI, canEdit, canReadNow, canEditNow } = useProjectAccess(pid)
  const { message } = App.useApp()
  const user = useAuthStore((s) => s.user)
  const authStatus = useAuthStore((s) => s.status)
  const identity = `${user?.id ?? 'none'}:${authStatus}:${authorizationKey(user)}`
  const projectTitle = useProjectStore((s) => s.project?.title ?? '')
  const loader = useCallback(() => characterApi.list(pid), [pid])
  const { data: loadedItems, loading, error: listError, reload } = useLoader(loader, { projectId: pid, resources: ['characters', 'careers'] })
  const items = useMemo(() => loadedItems ?? [], [loadedItems])
  // 卡片上的主职业·阶段：项目级挂接列表一次取回
  const badgeLoader = useCallback(async () => careerBadges(await assignmentApi.listProject(pid)), [pid])
  const { data: badges, reload: reloadBadges } = useLoader(badgeLoader, { projectId: pid, resources: ['characters', 'careers'] })
  const [editing, setEditing] = useState<Character | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [tab, setTab] = useState('basic')
  const [wizardOpen, setWizardOpen] = useState(false)
  const [generateOpen, setGenerateOpen] = useState(false)
  const [exportOpen, setExportOpen] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [filter, setFilter] = useState(ALL_CHARACTERS)
  const [query, setQuery] = useState('')
  const [view, setView] = useState<'grid' | 'list'>('grid')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(8)
  const [selected, setSelected] = useState<number[]>([])
  const [baseline, setBaseline] = useState<SettingBaseline | null>(null)
  const [baselineLoading, setBaselineLoading] = useState(false)
  const [baselineError, setBaselineError] = useState('')
  const [baselineConflict, setBaselineConflict] = useState(false)
  const [editorTicket, setEditorTicket] = useState<CharacterEditorTicket | null>(null)
  const [form] = Form.useForm<CharacterFormValues>()
  const gate = useRef(createCharacterRequestGate())
  const drafts = useRef(createCharacterDraftCache())
  const session = useRef<CharacterEditorTicket | null>(null)
  const editingRef = useRef<Character | null>(null)
  const initialValues = useRef<CharacterFormValues | null>(null)
  const baselineRef = useRef<SettingBaseline | null>(null)
  const baselineAbort = useRef<AbortController | null>(null)
  const baselineBlocked = useRef(true)
  const savingRef = useRef(false)
  const projectRef = useRef(pid)
  const scopeRef = useRef(`${pid}:${identity}`)

  useLayoutEffect(() => {
    const scope = `${pid}:${identity}`
    if (scopeRef.current !== scope) {
      scopeRef.current = scope
      editingRef.current = null
      initialValues.current = null
      form.resetFields()
      setEditorTicket(null)
      setDrawerOpen(false)
    }
    projectRef.current = pid
    const currentGate = gate.current
    const currentDrafts = drafts.current
    return () => {
      currentGate.close()
      currentDrafts.clear()
      baselineAbort.current?.abort()
      session.current = null
    }
  }, [pid, identity, form])
  usePublishAgentContext(pid, { character_id: drawerOpen && editorTicket?.projectId === pid ? editing?.id : undefined })

  const roleFilters = useMemo(() => characterRoleFilters(items), [items])
  const shown = useMemo(() => filterCharacters(items, filter, query), [items, filter, query])
  const currentPage = characterPage(shown, page, pageSize)
  // 只保留仍存在的勾选
  const selectedIds = useMemo(() => selected.filter((id) => items.some((c) => c.id === id)), [selected, items])

  const isCurrent = (ticket: CharacterEditorTicket) =>
    gate.current.isCurrent(ticket) && projectRef.current === ticket.projectId && canReadNow()

  const setCurrentBaseline = (value: SettingBaseline | null) => {
    baselineRef.current = value
    setBaseline(value)
  }

  const loadBaseline = async (ticket: CharacterEditorTicket, character: Character, mode: 'replace' | 'reconcile') => {
    const load = gate.current.beginLoad(ticket)
    if (!load) return
    baselineAbort.current?.abort()
    const controller = new AbortController()
    baselineAbort.current = controller
    const current = () => gate.current.isCurrentLoad(load) && isCurrent(ticket)
    baselineBlocked.current = true
    setBaselineLoading(true)
    try {
      const timeline = await progressionApi.findTimeline(pid, { subject_kind: 'character', character_id: character.id }, controller.signal)
      if (!current()) return
      if (!timeline) throw new Error('角色基础设定暂不可用，请重试')
      const next = await progressionApi.getBaseline(pid, timeline.id, controller.signal)
      if (!current()) return
      const currentCharacter = editingRef.current
      if (!currentCharacter || currentCharacter.id !== character.id) return
      if (mode === 'reconcile' && initialValues.current && !characterBaselineMatches(initialValues.current, next.baseline)) {
        // 保留原草稿供复制，但禁止用新版本提交旧草稿。
        setCurrentBaseline(next)
        setBaselineConflict(true)
        setBaselineError('基础设定已在其他位置更改，当前草稿已保留。请复制需要的内容后重新加载基础设定。')
        return
      }
      if (mode === 'replace' || !initialValues.current) {
        const values = characterToBaselineForm(currentCharacter, next.baseline)
        initialValues.current = values
        form.setFieldsValue(values)
      }
      setCurrentBaseline(next)
      setBaselineConflict(false)
      setBaselineError('')
      baselineBlocked.current = false
    } catch (err) {
      if (!current() || controller.signal.aborted) return
      setBaselineError(errorMessage(err))
    } finally {
      if (current()) setBaselineLoading(false)
    }
  }

  const rememberDraft = () => {
    const ticket = session.current
    const initial = initialValues.current
    if (ticket && initial && isCurrent(ticket) && canEditNow()) {
      drafts.current.remember(ticket.characterId, initial, form.getFieldsValue(true))
    }
  }

  const openEdit = (item: Character | null) => {
    if (!canReadNow() || (item && item.project_id !== pid)) return
    rememberDraft()
    baselineAbort.current?.abort()
    const ticket = gate.current.open(pid, item?.id ?? null)
    session.current = ticket
    editingRef.current = item
    setEditorTicket(ticket)
    setEditing(item)
    setTab('basic')
    savingRef.current = false
    setSaving(false)
    baselineBlocked.current = !!item
    setCurrentBaseline(null)
    setBaselineLoading(false)
    setBaselineError('')
    setBaselineConflict(false)
    const draft = drafts.current.recall(item?.id ?? null)
    initialValues.current = draft?.initial ?? (item ? null : { ...EMPTY_CHARACTER, aliases: [] })
    form.resetFields()
    if (draft) form.setFieldsValue(draft.values)
    else if (!item) form.setFieldsValue({ ...EMPTY_CHARACTER, aliases: [] })
    setDrawerOpen(true)
    if (item) void loadBaseline(ticket, item, draft ? 'reconcile' : 'replace')
  }

  useEntityDeepLink(pid, loadedItems, loading, openEdit, canReadNow)

  const handleSave = async () => {
    if (!canEditNow() || savingRef.current || baselineBlocked.current) return
    const ticket = session.current
    if (!ticket || !isCurrent(ticket) || (editing && (baselineLoading || baselineError || baselineConflict || !baselineRef.current))) return
    let v: CharacterFormValues
    try {
      if (!canEditNow()) return
      v = await form.validateFields()
      if (!isCurrent(ticket) || !canEditNow()) return
    } catch {
      if (!isCurrent(ticket) || !canEditNow()) return
      message.warning('请检查表单中的必填项')
      return
    }
    if (!gate.current.isCurrent(ticket) || !canEditNow() || savingRef.current || baselineBlocked.current) return
    const initial = initialValues.current
    if (editing && !initial) return
    const patch = initial && editing ? formToCharacterPatch(v, initial) : null
    if (patch && Object.keys(patch).length === 0) {
      message.info('没有需要保存的修改')
      return
    }
    savingRef.current = true
    setSaving(true)
    let activeTicket = ticket
    try {
      if (!canEditNow()) return
      const saved = editing
        ? await characterApi.update(pid, editing.id, {
            ...patch,
            edit_mode: 'baseline',
            expected_revision: baselineRef.current?.timeline_revision,
          })
        : await characterApi.create(pid, formToCharacterInput(v))
      if (!isCurrent(ticket) || !canEditNow()) return
      drafts.current.forget(ticket.characterId)
      if (!editing) {
        activeTicket = gate.current.open(pid, saved.id)
        session.current = activeTicket
        setEditorTicket(activeTicket)
      }
      message.success('已保存')
      editingRef.current = saved
      setEditing(saved)
      invalidateProject(pid, ['characters'])
      // 保存结果是最新投影，不可用作基础快照；重新读取前暂停下一次保存。
      baselineBlocked.current = true
      setCurrentBaseline(null)
      initialValues.current = null
      await loadBaseline(activeTicket, saved, 'replace')
    } catch (err) {
      if (!isCurrent(activeTicket) || !canEditNow()) return
      if (err instanceof ApiError && err.status === 409 && err.body?.code === 'stale_revision') {
        baselineBlocked.current = true
        setBaselineError('设定已在其他位置更新，当前草稿已保留。请先刷新基础设定，再继续保存。')
        setBaselineConflict(false)
      } else {
        message.error(errorMessage(err))
      }
    } finally {
      if (isCurrent(activeTicket)) {
        savingRef.current = false
        setSaving(false)
      }
    }
  }

  const handleProgressionChanged = (ticket: CharacterEditorTicket, character: Character, revision: number) => {
    if (ticket.characterId !== character.id || !isCurrent(ticket)) return
    if (baselineRef.current?.timeline_revision !== revision) void loadBaseline(ticket, character, 'reconcile')
  }

  const confirmBaseline = async (ticket: CharacterEditorTicket) => {
    const currentBaseline = baselineRef.current
    const initial = initialValues.current
    if (!currentBaseline || !initial || baselineBlocked.current || savingRef.current || !isCurrent(ticket) || !canEditNow()) return
    const patch = formToCharacterPatch(form.getFieldsValue(true), initial)
    const temporalFields = ['age', 'gender', 'appearance', 'personality', 'background', 'profile', 'current_state', 'life_status']
    if (temporalFields.some((key) => key in patch)) {
      message.warning('请先保存修改后的基础设定，再确认校准')
      return
    }
    const load = gate.current.beginLoad(ticket)
    if (!load) return
    baselineAbort.current?.abort()
    baselineBlocked.current = true
    setBaselineLoading(true)
    const current = () => gate.current.isCurrentLoad(load) && isCurrent(ticket) && canEditNow()
    try {
      const next = await progressionApi.updateBaseline(pid, currentBaseline.timeline_id, {
        expected_revision: currentBaseline.timeline_revision,
        changes: {},
        confirm: true,
      })
      if (!current()) return
      if (!characterBaselineMatches(initial, next.baseline)) {
        setBaselineConflict(true)
        setBaselineError('基础设定已变化，当前草稿已保留。请复制需要的内容后重新加载。')
        return
      }
      setCurrentBaseline(next)
      setBaselineError('')
      baselineBlocked.current = false
      message.success('基础设定已确认')
      invalidateProject(pid, ['characters'])
    } catch (err) {
      if (!current()) return
      if (err instanceof ApiError && err.status === 409 && err.body?.code === 'stale_revision') {
        setBaselineError('设定已在其他位置更新，当前草稿已保留。请先刷新基础设定，再继续确认。')
        setBaselineConflict(false)
      } else {
        setBaselineError(errorMessage(err))
      }
    } finally {
      if (gate.current.isCurrentLoad(load) && isCurrent(ticket)) setBaselineLoading(false)
    }
  }

  const handleDelete = async (item: Character): Promise<boolean> => {
    if (!canEditNow()) return false
    try {
      if (!canEditNow()) return false
      await characterApi.remove(pid, item.id)
      if (!canEditNow()) return false
      message.success('已删除（相关关系、组织成员与职业挂接一并清理）')
      invalidateProject(pid, ['characters', 'relationships', 'organizations', 'careers'])
      return true
    } catch (err) {
      if (!canEditNow()) return false
      message.error(errorMessage(err))
      return false
    }
  }

  const toggle = (id: number, on: boolean) =>
    setSelected((list) => (on ? [...list.filter((x) => x !== id), id] : list.filter((x) => x !== id)))

  return (
    <section className="characters-page" aria-labelledby="characters-title">
      <header className="characters-page-heading">
        <div className="characters-page-intro">
          <div className="characters-page-title"><h1 id="characters-title">角色</h1><span>{items.length}</span></div>
          <p>管理人物设定，让每个角色鲜活而立体。</p>
        </div>
        <div className="characters-page-actions">
          {canUseAI && <>
            <Button icon={<UploadOutlined />} onClick={() => setImportOpen(true)}>导入卡片</Button>
            <Button icon={<DownloadOutlined />} onClick={() => setExportOpen(true)} disabled={items.length === 0}>
              导出卡片{selectedIds.length > 0 ? `（${selectedIds.length}）` : ''}
            </Button>
            <Dropdown trigger={['click']} menu={{
              items: [{ key: 'regenerate', label: 'AI 重新生成全部' }],
              onClick: ({ key }) => { if (key === 'regenerate') setWizardOpen(true) },
            }}>
              <Button>更多<DownOutlined /></Button>
            </Dropdown>
            <Button icon={<RobotOutlined />} onClick={() => setGenerateOpen(true)}>AI 生成角色</Button>
          </>}
          <Button type="primary" icon={<PlusOutlined />} onClick={() => openEdit(null)} disabled={!canEdit}>
            新增角色
          </Button>
        </div>
      </header>
      {listError && <Alert type="error" showIcon message={listError} action={<Button onClick={reload}>重试</Button>} style={{ marginBottom: 12 }} />}
      <div className="characters-toolbar">
        <Tabs
          className="characters-role-tabs"
          activeKey={filter}
          onChange={(value) => { setFilter(value); setPage(1) }}
          items={roleFilters.map((role) => ({
            key: role.value,
            label: <span className="characters-filter-label">{role.label}<span>{role.count}</span></span>,
          }))}
        />
        <div className="characters-search-controls">
          <Input
            aria-label="搜索角色"
            placeholder="搜索姓名、别名或设定…"
            prefix={<SearchOutlined />}
            allowClear
            value={query}
            onChange={(event) => { setQuery(event.target.value); setPage(1) }}
          />
          <div className="characters-view-switch" role="group" aria-label="角色展示方式">
            <Button type={view === 'grid' ? 'primary' : 'default'} icon={<AppstoreOutlined />} aria-label="卡片视图" aria-pressed={view === 'grid'} onClick={() => setView('grid')} />
            <Button type={view === 'list' ? 'primary' : 'default'} icon={<UnorderedListOutlined />} aria-label="列表视图" aria-pressed={view === 'list'} onClick={() => setView('list')} />
          </div>
        </div>
      </div>
      <div className="characters-list-summary">
        <div className="characters-selection">
          {canUseAI && <Checkbox
            checked={currentPage.items.length > 0 && currentPage.items.every((character) => selectedIds.includes(character.id))}
            indeterminate={currentPage.items.some((character) => selectedIds.includes(character.id)) && !currentPage.items.every((character) => selectedIds.includes(character.id))}
            onChange={(event) => {
              const ids = currentPage.items.map((character) => character.id)
              setSelected((list) => event.target.checked ? [...new Set([...list, ...ids])] : list.filter((id) => !ids.includes(id)))
            }}
            disabled={currentPage.items.length === 0}
          >全选当前</Checkbox>}
          {canUseAI && selectedIds.length > 0 && <>
            <span>已选 {selectedIds.length} 位</span>
            <Button type="text" size="small" onClick={() => setSelected([])}>清空选择</Button>
          </>}
        </div>
        <span aria-live="polite">{filter !== ALL_CHARACTERS || query.trim() ? `找到 ${shown.length} 位角色` : `共 ${items.length} 位角色`}</span>
      </div>
      <Spin spinning={loading}>
        {items.length === 0 ? (
          <div className="characters-empty"><Empty description={listError ? '角色列表暂不可用，请重试' : canUseAI ? '暂无角色，可手动新增、导入卡片或使用 AI 生成' : '暂无角色，可手动新增'} /></div>
        ) : shown.length === 0 ? (
          <div className="characters-empty"><Empty description="没有找到匹配的角色">
            <Button onClick={() => { setQuery(''); setFilter(ALL_CHARACTERS); setPage(1) }}>清除筛选</Button>
          </Empty></div>
        ) : (
          <div className={`characters-grid${view === 'list' ? ' characters-grid-list' : ''}`}>
            {currentPage.items.map((character) => <CharacterCard
              key={character.id}
              character={character}
              career={badges?.get(character.id)}
              selectable={canUseAI}
              selected={selectedIds.includes(character.id)}
              canEdit={canEdit}
              onSelect={(selected) => toggle(character.id, selected)}
              onOpen={() => openEdit(character)}
              onDelete={() => handleDelete(character)}
            />)}
          </div>
        )}
      </Spin>
      {shown.length > 0 && <Pagination
        className="characters-pagination"
        current={currentPage.page}
        pageSize={pageSize}
        total={shown.length}
        pageSizeOptions={[8, 16, 32]}
        locale={{ items_per_page: '条/页', prev_page: '上一页', next_page: '下一页' }}
        showSizeChanger
        responsive
        showTotal={(total) => `显示 ${currentPage.start}–${currentPage.end}，共 ${total} 位角色`}
        onChange={(nextPage, nextSize) => { setPage(nextSize === pageSize ? nextPage : 1); setPageSize(nextSize) }}
      />}

      <Drawer
        title={editing ? `编辑角色：${editing.name}` : '新增角色'}
        open={drawerOpen && editorTicket?.projectId === pid}
        onClose={() => {
          rememberDraft()
          gate.current.close()
          baselineAbort.current?.abort()
          session.current = null
          editingRef.current = null
          baselineBlocked.current = true
          savingRef.current = false
          setEditorTicket(null)
          setDrawerOpen(false)
          setSaving(false)
          reloadBadges()
        }}
        width="min(760px, 100vw)"
        forceRender
        extra={
          <Button
            type="primary"
            loading={saving}
            onClick={() => void handleSave()}
            disabled={!canEdit || (!!editing && (!baseline || !!baselineError || baselineConflict || baselineLoading))}
          >
            {editing ? '保存基础设定' : '保存'}
          </Button>
        }
      >
        {editing && baseline && !baselineError && (
          <Alert
            showIcon
            type={baseline.baseline_status === 'legacy_unverified' ? 'warning' : 'info'}
            message={baselineEditorLabel(baseline.baseline_status, canEdit)}
            description={baseline.baseline_status === 'legacy_unverified'
              ? canEdit ? '现有档案暂作为故事开始时的设定。旧历史可能不完整，请检查并校准；后续变化可在「剧情变化」中维护。' : '现有档案暂作为故事开始时的设定，旧历史可能不完整。'
              : canEdit ? '这里维护故事开始时的角色资料；随剧情发生的变化可在「剧情变化」中维护。' : '这里展示故事开始时的角色资料；随剧情发生的变化可在「剧情变化」中查看。'}
            action={baseline.baseline_status === 'legacy_unverified' && canEdit && editorTicket && (
              <Popconfirm
                title="确认基础设定已校准？"
                description="确认前请先保存对基础设定的修改。已有剧情变化不会改变。"
                okText="确认"
                cancelText="取消"
                onConfirm={() => void confirmBaseline(editorTicket)}
              >
                <Button size="small" disabled={saving || baselineLoading}>确认已校准</Button>
              </Popconfirm>
            )}
            style={{ marginBottom: 16 }}
          />
        )}
        {editing && baselineError && (
          <Alert
            showIcon
            type="error"
            message={baselineConflict ? '基础设定发生冲突' : baseline ? '基础设定待刷新' : '无法读取基础设定'}
            description={baselineError}
            action={editorTicket && (baselineConflict ? (
              <Popconfirm
                title="重新加载基础设定？"
                description="重新加载会覆盖尚未保存的角色资料，请先复制需要保留的内容。"
                okText="重新加载"
                cancelText="取消"
                onConfirm={() => void loadBaseline(editorTicket, editing, 'replace')}
              >
                <Button size="small" disabled={baselineLoading}>重新加载</Button>
              </Popconfirm>
            ) : (
              <Button
                size="small"
                disabled={baselineLoading}
                onClick={() => void loadBaseline(editorTicket, editing, baseline ? 'reconcile' : 'replace')}
              >
                重试
              </Button>
            ))}
            style={{ marginBottom: 16 }}
          />
        )}
        <Spin spinning={!!editing && baselineLoading}>
        <Form form={form} layout="vertical" disabled={!canEdit || saving || baselineLoading || (!!editing && !baseline)}>
          <Tabs
            activeKey={tab}
            onChange={setTab}
            items={[
              { key: 'basic', label: '基础', forceRender: true, children: <CharacterBasicFields form={form} creating={!editing} /> },
              { key: 'desc', label: '描写', forceRender: true, children: <CharacterDescFields /> },
              { key: 'state', label: '状态', forceRender: true, children: <CharacterStateFields isBaseline={!!editing} /> },
              ...(editing
                ? [
                    { key: 'progression', label: '剧情变化', children: null },
                    { key: 'mentions', label: '提及', children: null },
                    {
                      key: 'rel',
                      label: '关系',
                      children: <CharacterRelationsTab key={`${pid}:relation:${editing.id}`} projectId={pid} character={editing} characters={items} />,
                    },
                    {
                      key: 'org',
                      label: '组织',
                      children: <CharacterMembershipsTab key={`${pid}:membership:${editing.id}`} projectId={pid} character={editing} />,
                    },
                    {
                      key: 'career',
                      label: '职业',
                      children: <CharacterCareersTab key={`${pid}:career:${editing.id}`} projectId={pid} character={editing} />,
                    },
                    {
                      key: 'foreshadow',
                      label: '伏笔',
                      children: (
                        <Suspense fallback={<Spin />}>
                          <CharacterForeshadowsTab key={`${pid}:foreshadow:${editing.id}`} projectId={pid} character={editing} />
                        </Suspense>
                      ),
                    },
                  ]
                : []),
            ]}
          />
        </Form>
        </Spin>
        {editing && editorTicket?.projectId === pid && drawerOpen && tab === 'mentions' && <EntityMentions projectId={pid} entity={{ kind: 'character', id: editing.id }} />}
        {editing && editorTicket?.projectId === pid && drawerOpen && tab === 'progression' && (
          <SettingProgressionsPanel
            key={`${pid}:character:${editing.id}`}
            projectId={pid}
            target={{ subject_kind: 'character', character_id: editing.id }}
            onChanged={(revision) => handleProgressionChanged(editorTicket, editing, revision)}
            onRequestConfirmBaseline={() => void confirmBaseline(editorTicket)}
          />
        )}
        {!editing && (
          <Typography.Text type="secondary">保存后可在「关系」「组织」「职业」Tab 中设置人物关系、所属组织与职业阶段。</Typography.Text>
        )}
      </Drawer>

      {canUseAI && (<WizardModal
        projectId={pid}
        kind="characters"
        open={wizardOpen}
        existingCount={items.length}
        onClose={() => setWizardOpen(false)}
        onDone={() => {
          reload()
          reloadBadges()
        }}
      />)}
      {canUseAI && (<EntityTaskModal
        projectId={pid}
        kind="character.generate"
        open={generateOpen}
        onClose={() => setGenerateOpen(false)}
        onSaved={() => {
          reload()
          reloadBadges()
        }}
      />)}
      {canUseAI && (<CardExportModal
        projectId={pid}
        projectTitle={projectTitle}
        open={exportOpen}
        characters={items}
        initialCharacterIds={selectedIds}
        onClose={() => setExportOpen(false)}
      />)}
      {canUseAI && (<CardImportModal
        projectId={pid}
        open={importOpen}
        onClose={() => setImportOpen(false)}
        onImported={() => {
          reload()
          reloadBadges()
        }}
      />)}
    </section>
  )
}
