import { BookOutlined, EyeOutlined, FileTextOutlined, HistoryOutlined, OrderedListOutlined, PlusOutlined, SettingOutlined } from '@ant-design/icons'
import { Alert, App, Button, Drawer, Input, Modal, Space, Spin, Tabs, Tag, theme, Typography } from 'antd'
import type { TextAreaRef } from 'antd/es/input/TextArea'
import { useEffect, useMemo, useRef, useState } from 'react'
import type { MentionAnchor } from '../../api/mentions'
import { sceneApi } from '../../api/scenes'
import { currentRequestGeneration, errorMessage } from '../../api/client'
import { richDocumentApi, type MarkerTimelineItem } from '../../api/richDocument'
import type { Chapter, StyleChoice } from '../../api/types'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'
import { authorizationKey } from '../../utils/permissions'
import { projectAccess } from '../../utils/projectAccess'
import type { TextSelection } from '../chapter/RewritePanel'
import MentionPreview from '../mentions/MentionPreview'
import type { SceneDraftController, SceneDraftSnapshot } from './sceneDraftController'
import SceneConflictReview from './SceneConflictReview'
import SceneNavigator from './SceneNavigator'
import ScenePlanningPanel from './ScenePlanningPanel'
import SceneSettingPreview from './SceneSettingPreview'
import SceneStructureActions from './SceneStructureActions'
import MarkerTimeline from './MarkerTimeline'
import { markerLocation, type MarkerLocation } from './markerLocation'
import { reviewTextareaOffset, sourceToTextareaOffset } from './reviewOffsets'
import RichSceneEditor, { type RichSceneEditorHandle, type RichSectionSelection } from './RichSceneEditor'
import RichSectionRewriteModal, { type SectionRewriteTarget } from './RichSectionRewriteModal'
import { editorFromSectionDocument, sameSectionDocument } from './richSectionProjection'
import { validSectionDocument } from './richSectionValidity'

interface Props {
  projectId: number; chapter: Chapter; state: SceneDraftSnapshot; controller: SceneDraftController
  disabled: boolean; selection: TextSelection; onSelection: (selection: TextSelection) => void
  ensureSaved: () => Promise<boolean>; onChanged: () => void; onSelect: (sid: number) => Promise<boolean>
  mentionAnchor: MentionAnchor | null
  onStructureDirtyChange: (dirty: boolean) => void
  markerJump: MarkerLocation | null
  onNavigateMarker: (location: MarkerLocation) => Promise<boolean>
  aiConfigId: number | null; styleChoice: StyleChoice; skillId: number | null
  onSectionLockChange: (locked: boolean) => void
  onOpenInspectorTab?: (tab: 'ai' | 'scene' | 'chapter') => void
}
/** 当前场景独立编辑；整章仅通读服务端投影，不在浏览器重造正文拼接规则。 */
export default function SceneWritingPanel({ projectId, chapter, state, controller, disabled, selection, onSelection, ensureSaved, onChanged, onSelect, mentionAnchor, onStructureDirtyChange, markerJump, onNavigateMarker, aiConfigId, styleChoice, skillId, onSectionLockChange, onOpenInspectorTab }: Props) {
  const { canEdit, canUseAI, canReadNow, canEditNow } = useProjectAccess(projectId)
  const { message, modal } = App.useApp()
  const { token } = theme.useToken()
  const [orderDrawerOpen, setOrderDrawerOpen] = useState(false)
  const [planningDrawerOpen, setPlanningDrawerOpen] = useState(false)
  const [summaryOpen, setSummaryOpen] = useState(false)
  const [reading, setReading] = useState(!!mentionAnchor)
  const [mentions, setMentions] = useState(false)
  const [timelineOpen, setTimelineOpen] = useState(false)
  const [locating, setLocating] = useState(false)
  const [locationMessage, setLocationMessage] = useState('')
  const [sectionBusy, setSectionBusy] = useState(false)
  const [sectionTarget, setSectionTarget] = useState<SectionRewriteTarget | null>(null)
  const input = useRef<TextAreaRef>(null)
  const richEditor = useRef<RichSceneEditorHandle>(null)
  const pendingTimelineFocus = useRef<(TextSelection & { sectionKey?: string }) | null>(null)
  const [focus, setFocus] = useState<(TextSelection & { sectionKey?: string }) | null>(null) // 各目标 Unicode 码点
  const scene = state.scene
  const processedMarker = useRef('')
  const unsupported = useMemo(() => {
    if (!state.documentDraft) return '完整场景文档尚未加载，不能编辑正文。'
    try {
      if (!validSectionDocument(state.documentDraft)) return '文档坐标或格式无效。'
      editorFromSectionDocument(state.documentDraft); return ''
    } catch (error) { return errorMessage(error) }
  }, [state.documentDraft])
  useEffect(() => {
    if (!focus || mentions || state.loading) return
    const frame = requestAnimationFrame(() => {
      if (richEditor.current) {
        const selected = focus.sectionKey ? richEditor.current.selectSection(focus.sectionKey, focus.start, focus.end)
          : richEditor.current.select(focus.start, focus.end)
        if (!selected) message.warning('目标位置不能可靠映射，请重新定位。')
        setFocus(null)
      } else {
        if (focus.sectionKey) { message.warning('区块编辑器不可用，请重新加载后定位。'); setFocus(null); return }
        const el = input.current?.resizableTextArea?.textArea
        const current = controller.getSnapshot().scene
        if (!el || !current) return
        const start = sourceToTextareaOffset(current.content, focus.start)
        const end = sourceToTextareaOffset(current.content, focus.end)
        if (start === null || end === null) { message.warning('正文位置不能可靠映射，请重新定位。'); return }
        el.focus(); el.setSelectionRange(start, end); onSelection({ start, end }); setFocus(null)
      }
    })
    return () => cancelAnimationFrame(frame)
  }, [focus, mentions, state.loading, state.documentDraft, controller, message, onSelection])
  useEffect(() => {
    if (!markerJump || !scene || state.loading || processedMarker.current) return
    const frame = requestAnimationFrame(() => {
      processedMarker.current = `${markerJump.sceneId}:${markerJump.revision}:${markerJump.sectionKey ?? ''}:${markerJump.start}:${markerJump.end}`
      if (scene.id !== markerJump.sceneId || scene.content_revision !== markerJump.revision) {
        message.warning('标记来源已变化，请刷新时间线后重新定位。'); return
      }
      const text = markerJump.sectionKey ? state.documentDraft?.sections.find((item) => item.key === markerJump.sectionKey)?.content : scene.content
      if (text === undefined || sourceToTextareaOffset(text, markerJump.start) === null || sourceToTextareaOffset(text, markerJump.end) === null) { message.warning('标记位置已失效，请重新核对。'); return }
      setMentions(false); setFocus({ start: markerJump.start, end: markerJump.end, sectionKey: markerJump.sectionKey })
    })
    return () => cancelAnimationFrame(frame)
  }, [markerJump, scene, state.loading, state.documentDraft, message, onSelection])
  const sync = (el: HTMLTextAreaElement) => onSelection({ start: el.selectionStart, end: el.selectionEnd })
  const jump = async (item: MarkerTimelineItem) => {
    const location = markerLocation(item)
    if (!location) { message.warning('标记已失效，不能自动定位。'); return false }
    if (location.chapterId !== chapter.id) return onNavigateMarker(location)
    if (disabled || !(await onSelect(location.sceneId)) || !canReadNow()) return false
    const current = controller.getSnapshot().scene
    if (!current || current.id !== location.sceneId || current.content_revision !== location.revision) {
      message.warning('标记来源已变化，请刷新时间线后重新定位。'); return false
    }
    const text = location.sectionKey ? controller.getSnapshot().documentDraft?.sections.find((item) => item.key === location.sectionKey)?.content : current.content
    if (text === undefined || sourceToTextareaOffset(text, location.start) === null || sourceToTextareaOffset(text, location.end) === null) { message.warning('标记位置已失效，请重新核对。'); return false }
    // Drawer 关闭动画会自动把焦点交还按钮；动画结束后再定位，避免覆盖正文焦点。
    pendingTimelineFocus.current = { start: location.start, end: location.end, sectionKey: location.sectionKey }
    setMentions(false); setTimelineOpen(false)
    return true
  }
  const relink = async (item: MarkerTimelineItem) => {
    if (!canEditNow() || disabled || !(await ensureSaved()) || !canEditNow()) return false
    const current = controller.getSnapshot().scene
    if (!current) { message.warning('请先在当前场景正文中选中要关联的文字。'); return false }
    const el = input.current?.resizableTextArea?.textArea
    const section = richEditor.current?.sectionSelection()
    const selectedRange = richEditor.current?.selection()
    const start = section?.start ?? selectedRange?.start ?? (el ? reviewTextareaOffset(current.content, el.value, selection.start) : null)
    const end = section?.end ?? selectedRange?.end ?? (el ? reviewTextareaOffset(current.content, el.value, selection.end) : null)
    if (start === null || end === null || start >= end) { message.warning('请先在当前场景正文中选中完整文字，再打开标记时间线。'); return false }
    if (item.chapter_id === chapter.id && item.content_revision !== current.content_revision) {
      message.warning('标记目录已过期，请从第一页刷新后重试。'); return false
    }
    const selected = section?.selectedText ?? Array.from(current.content).slice(start, end).join('')
    const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(selected))
    const latest = controller.getSnapshot()
    const currentSectionSelection = richEditor.current?.sectionSelection()
    const currentProseSelection = richEditor.current?.selection()
    if (!canEditNow() || latest.scene?.id !== current.id || latest.scene.content_revision !== current.content_revision
      || (section && (!currentSectionSelection || currentSectionSelection.sectionKey !== section.sectionKey
        || currentSectionSelection.start !== start || currentSectionSelection.end !== end || currentSectionSelection.selectedText !== selected))
      || (!section && selectedRange && (!currentProseSelection || currentProseSelection.start !== start || currentProseSelection.end !== end))) {
      message.warning('计算期间场景、选区或权限已变化，请重新选择。'); return false
    }
    await richDocumentApi.relinkMarker(projectId, item, {
      expected_source_revision: item.content_revision, target_chapter_id: chapter.id, target_scene_id: current.id,
      expected_target_revision: current.content_revision, target_section_key: section?.sectionKey ?? null, start, end,
      selected_hash: Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, '0')).join(''),
    })
    if (!canReadNow()) return false
    await controller.load(); onChanged(); message.success('标记已重新关联至所选正文'); return true
  }
  const deleteMarker = async (item: MarkerTimelineItem) => {
    if (!canEditNow() || disabled || !(await ensureSaved()) || !canEditNow()) return false
    const current = controller.getSnapshot().scene
    if (item.chapter_id === chapter.id && item.content_revision !== current?.content_revision) {
      message.warning('标记目录已过期，请从第一页刷新后重试。'); return false
    }
    await richDocumentApi.deleteMarker(projectId, item)
    if (!canReadNow()) return false
    if (item.chapter_id === chapter.id) await controller.load()
    onChanged(); message.success('标记已删除，正文未改变'); return true
  }
  const convertSection = (key: string) => {
    const initial = controller.getSnapshot(); const originalScene = initial.scene; const draft = initial.documentDraft
    const section = draft?.sections.find((item) => item.key === key)
    if (!originalScene || !draft || !section || disabled || sectionBusy || !canEditNow()) return
    modal.confirm({ title: `将“${section.title}”转为正文？`,
      content: '将先保存当前草稿，再把区块文字和可保留格式插入其当前位置；关联标记随之迁移，阅读和导出会显示转换后的正文。转换会保存并清空当前编辑器撤销历史，可通过章节版本恢复。',
      okText: '保存并转换', cancelText: '取消', onOk: async () => {
        const now = controller.getSnapshot()
        if (!canEditNow() || now.scene?.id !== originalScene.id || now.documentDraft !== draft) {
          message.warning('场景、区块或权限已变化，请重新确认。'); return
        }
        setSectionBusy(true)
        let locked = false
        try {
          if (!(await ensureSaved()) || !canEditNow()) return
          const latest = controller.getSnapshot(); const savedSection = latest.document?.document.sections.find((item) => item.key === key)
          if (latest.scene?.id !== originalScene.id || !savedSection || !latest.document || !latest.documentDraft
            || !sameSectionDocument(latest.document.document, latest.documentDraft)) {
            message.warning('区块或草稿已变化，请重新确认。'); return
          }
          onSectionLockChange(true); locked = true
          const result = await richDocumentApi.convert(projectId, chapter.id, originalScene.id, key, latest.document.content_revision)
          if (!canReadNow()) return
          const loaded = await controller.load(originalScene.id, false)
          onChanged()
          if (!loaded || controller.getSnapshot().document?.content_revision !== result.content_revision) {
            message.warning('转换已提交，但最新文档已变化或无法读取；请重新核对。'); return
          }
          message.success('区块已转为正文。')
        } catch (error) { if (canReadNow()) message.error(`转换未确认：${errorMessage(error)}。请保留草稿并核对最新文档。`) }
        finally { if (locked) onSectionLockChange(false); setSectionBusy(false) }
      } })
  }
  const openSectionRewrite = async (selected: RichSectionSelection) => {
    const initial = controller.getSnapshot(); const originalScene = initial.scene
    const auth = useAuthStore.getState()
    const generation = currentRequestGeneration()
    const actor = authorizationKey(auth.user)
    const canUseAINow = () => {
      const current = useAuthStore.getState()
      return current.status === 'authenticated' && currentRequestGeneration() === generation
        && current.user?.id === auth.user?.id && authorizationKey(current.user) === actor && canEditNow()
        && projectAccess(useProjectStore.getState().project, current.user, projectId).canUseAI
    }
    if (!originalScene || disabled || sectionBusy || !canUseAINow()) return
    setSectionBusy(true)
    try {
      if (!(await ensureSaved()) || !canUseAINow()) return
      const latest = controller.getSnapshot(); const currentSelection = richEditor.current?.sectionSelection()
      const section = latest.document?.document.sections.find((item) => item.key === selected.sectionKey)
      if (latest.scene?.id !== originalScene.id || !section || !latest.document || !currentSelection
        || currentSelection.sectionKey !== selected.sectionKey || currentSelection.start !== selected.start
        || currentSelection.end !== selected.end || currentSelection.selectedText !== selected.selectedText
        || Array.from(section.content).slice(selected.start, selected.end).join('') !== selected.selectedText
        || !latest.documentDraft || !sameSectionDocument(latest.document.document, latest.documentDraft)) {
        message.warning('保存期间选区、区块或场景已变化，请重新选择。'); return
      }
      setSectionTarget({ projectId, chapterId: chapter.id, sceneId: originalScene.id, sectionKey: section.key,
        sectionTitle: section.title, includeInAi: section.include_in_ai, contentRevision: latest.document.content_revision,
        planningRevision: latest.document.planning_revision, start: selected.start, end: selected.end, selectedText: selected.selectedText })
    } catch (error) { if (canReadNow()) message.error(`无法准备区块选区：${errorMessage(error)}`) }
    finally { setSectionBusy(false) }
  }
  const sectionApplied = (result: import('../../api/richDocument').RichDocumentOut) => {
    const target = sectionTarget
    if (!target) return
    void (async () => {
      const loaded = await controller.load(target.sceneId, false)
      if (!canReadNow()) return
      onChanged()
      if (!loaded || controller.getSnapshot().document?.content_revision !== result.content_revision) {
        message.warning('区块结果已提交，但最新文档已变化或无法读取；请重新核对。'); return
      }
      message.success('区块选区已更新。')
    })()
  }
  const locate = async () => {
    if (!mentionAnchor || !canReadNow()) return
    setLocating(true); setLocationMessage('')
    try {
      const resolved = await sceneApi.resolvePosition(projectId, chapter.id, mentionAnchor.content_hash, mentionAnchor.start, mentionAnchor.end)
      if (!canReadNow()) return
      if (!resolved.scene_id || resolved.start === null || resolved.end === null) { setLocationMessage(resolved.reason === 'stale' ? '原文或顺序已变化，旧位置已失效，请回提及列表重新定位。' : '该命中跨场景或结构分隔，仅在整章通读中展示，不猜测场景归属。'); return }
      if (!(await onSelect(resolved.scene_id)) || !canReadNow()) return
      const current = controller.getSnapshot().scene
      if (!current || current.id !== resolved.scene_id || current.content_revision !== resolved.content_revision) { setLocationMessage('定位期间原文又有变化，请重新定位。'); return }
      setMentions(false); setFocus({ start: resolved.start, end: resolved.end }); setReading(false)
    } catch (err) { if (canReadNow()) setLocationMessage(errorMessage(err)) }
    finally { if (canReadNow()) setLocating(false) }
  }
  const handleCreateScene = async () => {
    if (!canEditNow() || disabled || sectionBusy || state.loading || state.composing || !(await ensureSaved())) return
    const originalScene = controller.getSnapshot().scene
    if (!originalScene || originalScene.content_revision === undefined) return
    try {
      const added = await sceneApi.create(projectId, chapter.id, {
        expected_content_revision: originalScene.content_revision,
        title: '新场景',
        after_scene_id: originalScene.id,
      })
      await controller.load(added.id, false)
      onChanged()
      message.success('已创建独立场景')
    } catch (err) {
      if (canEditNow()) message.error(errorMessage(err))
    }
  }

  const sceneTabs = (state.page?.items ?? []).map((item, index) => {
    const isSelected = item.id === scene?.id
    return {
      key: String(item.id),
      label: (
        <Space size={6} style={{ fontWeight: isSelected ? 600 : 400 }}>
          <span>{`场景 ${index + 1}${item.title ? ` · ${item.title}` : ''}`}</span>
          <Tag
            bordered={false}
            color={isSelected ? 'processing' : 'default'}
            style={{ marginInlineEnd: 0, fontSize: 12, paddingInline: 6 }}
          >
            {item.word_count ? `${item.word_count} 字` : '未写'}
          </Tag>
        </Space>
      ),
    }
  })

  return (
    <div style={{ width: '100%' }}>
      {/* 水平场景导航栏 */}
      <div style={{ marginBottom: 8, borderBottom: `1px solid ${token.colorBorderSecondary}` }}>
        <Tabs
          type="card"
          size="small"
          activeKey={scene ? String(scene.id) : undefined}
          onChange={(k) => void onSelect(Number(k))}
          items={sceneTabs}
          style={{ marginBottom: 0 }}
          tabBarStyle={{ marginBottom: 0 }}
          tabBarExtraContent={
            <Space size={6} wrap style={{ paddingBottom: 4 }}>
              <Button
                size="small"
                type="dashed"
                icon={<PlusOutlined />}
                disabled={!canEdit || disabled || sectionBusy || state.loading || state.composing}
                onClick={() => void handleCreateScene()}
              >
                新场景
              </Button>
              <Button
                size="small"
                icon={<OrderedListOutlined />}
                disabled={disabled || sectionBusy || state.loading}
                onClick={() => setOrderDrawerOpen(true)}
              >
                场景排序管理
              </Button>
              <Button
                size="small"
                icon={<BookOutlined />}
                disabled={state.composing || sectionBusy}
                onClick={() => setReading(true)}
              >
                整章通读
              </Button>
              <Button
                size="small"
                icon={<HistoryOutlined />}
                disabled={state.composing || sectionBusy}
                onClick={() => setTimelineOpen(true)}
              >
                全书标记
              </Button>
              <Button
                size="small"
                icon={<EyeOutlined />}
                disabled={state.composing || sectionBusy}
                aria-pressed={mentions}
                onClick={() => setMentions((old) => !old)}
              >
                {mentions ? '返回编辑' : '提及标记'}
              </Button>
            </Space>
          }
        />
      </div>

      {/* 场景子栏 */}
      {scene && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 12,
            padding: '6px 12px',
            background: token.colorFillQuaternary,
            borderRadius: 6,
            marginBottom: 8,
            flexWrap: 'wrap',
          }}
        >
          <Space size={8} style={{ flex: '1 1 240px', minWidth: 180 }}>
            <Typography.Text type="secondary" style={{ whiteSpace: 'nowrap', fontSize: 13, fontWeight: 500 }}>
              场景 {scene.sort + 1} 标题：
            </Typography.Text>
            <Input
              aria-label="场景标题"
              variant="borderless"
              placeholder="输入场景标题"
              value={state.draft.title}
              maxLength={200}
              onChange={(event) => controller.change({ title: event.target.value })}
              disabled={!canEdit || disabled || sectionBusy || state.loading}
              style={{ fontWeight: 600, fontSize: 14, padding: 0 }}
            />
          </Space>
          <Space size={8} wrap>
            {scene.planning?.effective && (
              <Tag color="cyan" style={{ marginInlineEnd: 0 }}>
                POV: {scene.planning.effective.pov_character_name ?? '未指定'}（{scene.planning.effective.sources.pov === 'chapter' ? '继承' : '覆盖'}）
              </Tag>
            )}
            <Button
              size="small"
              type={summaryOpen ? 'primary' : 'default'}
              icon={<FileTextOutlined />}
              onClick={() => setSummaryOpen((v) => !v)}
            >
              {summaryOpen ? '收起摘要' : '作者摘要'}
            </Button>
            <SceneStructureActions
              key={`structure:${scene.id}`}
              projectId={projectId}
              chapterId={chapter.id}
              state={state}
              controller={controller}
              selection={selection}
              disabled={disabled || sectionBusy || state.loading || state.composing || !!unsupported}
              ensureSaved={ensureSaved}
              onChanged={onChanged}
            />
            <Button
              size="small"
              icon={<SettingOutlined />}
              onClick={() => {
                if (onOpenInspectorTab) {
                  onOpenInspectorTab('scene')
                } else {
                  setPlanningDrawerOpen(true)
                }
              }}
            >
              规划与设定
            </Button>
          </Space>
        </div>
      )}

      {/* 展开的场景摘要 */}
      {summaryOpen && scene && (
        <div
          style={{
            padding: '8px 12px',
            background: token.colorFillAlter,
            border: `1px solid ${token.colorBorderSecondary}`,
            borderRadius: 6,
            marginBottom: 8,
          }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              作者摘要（独立保存，不自动推断，作为 AI 写作和整章结构上下文参考）
            </Typography.Text>
            <Button type="text" size="small" onClick={() => setSummaryOpen(false)}>
              收起
            </Button>
          </div>
          <Input.TextArea
            aria-label="场景摘要"
            value={state.draft.summary}
            maxLength={200000}
            placeholder="在此输入当前场景摘要、情节推进或注意事项..."
            autoSize={{ minRows: 2, maxRows: 6 }}
            onChange={(event) => controller.change({ summary: event.target.value })}
            disabled={!canEdit || disabled || sectionBusy || state.loading}
          />
        </div>
      )}

      {/* 状态与冲突提示 */}
      {state.error && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 8 }}
          message={state.error}
          action={<Button disabled={state.saving || disabled} onClick={() => void controller.load()}>重新读取目录（保留草稿）</Button>}
        />
      )}
      <SceneConflictReview state={state} controller={controller} disabled={!canEdit || disabled} />
      {state.loading && <Spin style={{ display: 'block', margin: '16px auto' }} />}

      {/* 核心正文编辑器 */}
      {scene && (
        <>
          {mentions ? (
            <div style={{ padding: 8, fontSize: 16, lineHeight: 1.9 }}>
              <MentionPreview projectId={projectId} text={state.draft.content} draft />
            </div>
          ) : unsupported || !state.documentDraft ? (
            <>
              <Alert type="warning" showIcon style={{ marginBottom: 8 }} message="正文暂为只读" description={`${unsupported} 原文与作者资料未改变。`} />
              <Input.TextArea
                ref={input}
                aria-label="当前场景正文"
                value={state.draft.content}
                readOnly
                onSelect={(event) => sync(event.currentTarget)}
                onKeyUp={(event) => sync(event.currentTarget)}
                onMouseUp={(event) => sync(event.currentTarget)}
                autoSize={{ minRows: 20 }}
                style={{ fontSize: 16, lineHeight: 1.9, resize: 'none' }}
              />
            </>
          ) : (
            <RichSceneEditor
              key={scene.id}
              ref={richEditor}
              document={state.documentDraft}
              savedDocument={state.document?.document}
              readOnly={!canEdit || disabled || sectionBusy || state.loading}
              onDocument={(next, evidence) => controller.changeDocument(next, evidence)}
              onCompositionChange={(value) => controller.setComposing(value)}
              onSelection={onSelection}
              onSectionConvert={convertSection}
              onSectionRewrite={canUseAI ? (value) => void openSectionRewrite(value) : undefined}
            />
          )}
        </>
      )}

      {/* 场景管理与排序抽屉 */}
      <SceneNavigator
        projectId={projectId}
        chapterId={chapter.id}
        page={state.page}
        sceneId={scene?.id}
        disabled={disabled || sectionBusy || state.loading || state.composing}
        controller={controller}
        ensureSaved={ensureSaved}
        onChanged={onChanged}
        onSelect={onSelect}
        onDraftChange={onStructureDirtyChange}
        open={orderDrawerOpen}
        onClose={() => setOrderDrawerOpen(false)}
      />

      {/* 场景规划与有效设定抽屉 */}
      <Drawer
        title={scene ? `场景 ${scene.sort + 1} · 规划与有效设定` : '场景规划与有效设定'}
        open={planningDrawerOpen}
        onClose={() => setPlanningDrawerOpen(false)}
        width="min(540px, 100vw)"
      >
        {scene && (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <div>
              <Typography.Text strong>{`场景 ${scene.sort + 1} · ${scene.title || '未命名'}`}</Typography.Text>
              <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
                字数：{scene.word_count ? `${scene.word_count} 字` : '未写'} · 内容版本 rev.{scene.content_revision}
              </Typography.Paragraph>
            </div>
            <ScenePlanningPanel
              key={`planning:${scene.id}`}
              projectId={projectId}
              scene={scene}
              disabled={disabled || sectionBusy || state.loading || state.composing}
              ensureSaved={ensureSaved}
              onChanged={() => { void controller.load(); onChanged() }}
            />
            <SceneSettingPreview
              key={`setting:${scene.id}:${scene.content_revision}`}
              projectId={projectId}
              chapterId={chapter.id}
              sceneId={scene.id}
            />
          </Space>
        )}
      </Drawer>

      {/* 整章通读弹窗 */}
      <Modal
        open={reading}
        title="整章通读 · 只读已保存正文"
        width={900}
        footer={
          <Space wrap>
            {mentionAnchor && <Button loading={locating} disabled={disabled} onClick={() => void locate()}>定位对应场景并编辑</Button>}
            <Button onClick={() => setReading(false)}>关闭通读</Button>
          </Space>
        }
        onCancel={() => setReading(false)}
      >
        <Alert type="info" message="按服务端保存的场景顺序通读；当前未保存的正文或摘要不在此预览中。" style={{ marginBottom: 12 }} />
        {locationMessage && <Alert type="warning" showIcon message={locationMessage} style={{ marginBottom: 12 }} />}
        <div style={{ maxHeight: '65vh', overflow: 'auto', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 16, lineHeight: 1.9 }}>
          <MentionPreview projectId={projectId} text={chapter.content} anchor={mentionAnchor} />
        </div>
      </Modal>

      {/* 全书标记时间线 */}
      <Drawer
        title="全书标记"
        open={timelineOpen}
        onClose={() => setTimelineOpen(false)}
        width="min(560px, 100vw)"
        afterOpenChange={(open) => {
          if (!open && pendingTimelineFocus.current) {
            setFocus(pendingTimelineFocus.current)
            pendingTimelineFocus.current = null
          }
        }}
      >
        <Typography.Paragraph type="secondary">
          重新关联前，请先在当前场景的正文选中文字；旧片段不会自动匹配。
        </Typography.Paragraph>
        {timelineOpen && (
          <MarkerTimeline
            projectId={projectId}
            refreshKey={scene?.content_revision}
            editable={canEdit && !disabled && !sectionBusy}
            onJump={jump}
            onRelink={relink}
            onDelete={deleteMarker}
          />
        )}
      </Drawer>

      {/* 局部区块改写弹窗 */}
      <RichSectionRewriteModal
        target={sectionTarget}
        aiConfigId={aiConfigId}
        styleChoice={styleChoice}
        skillId={skillId}
        onClose={() => setSectionTarget(null)}
        onApplied={sectionApplied}
        onLockChange={onSectionLockChange}
      />
    </div>
  )
}
