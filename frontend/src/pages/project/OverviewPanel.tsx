import { BulbOutlined, CloudDownloadOutlined, DownloadOutlined, ExperimentOutlined, RocketOutlined } from '@ant-design/icons'
import { App, Button, Card, Descriptions, Form, Input, Progress, Select, Space, Statistic, Switch, Typography } from 'antd'
import { lazy, Suspense, useEffect, useRef, useState, type CSSProperties, type MutableRefObject } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { analysisApi } from '../../api/analysis'
import { errorMessage } from '../../api/client'
import { PROJECT_STATUS, projectApi } from '../../api/projects'
import { penNamesApi } from '../../api/penNames'
import type { Project, ProjectStatus, ProjectUpdate } from '../../api/types'
import ExportModal from '../../components/ExportModal'
import ProjectInfoFields from '../../components/ProjectInfoFields'
import CoverPanel from '../../components/project/CoverPanel'
import DeleteProjectModal from '../../components/project/DeleteProjectModal'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import StyleSelect from '../../components/styles/StyleSelect'
import PenNameSelect from '../../components/shelf/PenNameSelect'
import { captureProjectScope, isProjectScopeCurrent, useProjectStore } from '../../stores/project'
import { useTasksStore } from '../../stores/tasks'
import { formatWordCount, projectInfoBody, wordsProgress, type ProjectInfoValues } from '../../utils/projectFields'
import { splitPane, splitRow } from '../../utils/layout'
import { formatTime } from '../../utils/time'

// 备份弹窗（含 api/transfer.ts、utils/transfer.ts）按需加载，不进入口 chunk
const BackupModal = lazy(() => import('../../components/transfer/BackupModal'))
const SaveNovelTemplateModal = lazy(() => import('../../components/novelTemplates/SaveNovelTemplateModal'))

/** 表单内的分栏：行距交给 Form.Item 自身的下边距。 */
const formRow: CSSProperties = { ...splitRow, rowGap: 0 }

export default function OverviewPanel() {
  const project = useProjectStore((s) => s.project)
  const scopeVersion = useProjectStore((s) => s.scopeVersion)
  const { canUseAI } = useProjectAccess(project?.id ?? NaN)
  const penNameVersionRef = useRef(0)
  const [templateSession, setTemplateSession] = useState<{ projectId: number; scopeVersion: number } | null>(null)
  if (!project) return null
  // 以项目 id + 更新时间为 key，保存后表单以最新数据重建；分析设置不放在其中，切换开关不会重置未保存的表单
  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <ProjectAuthor key={`${project.id}:${scopeVersion}`} project={project} penNameVersionRef={penNameVersionRef} />
      <OverviewInner key={`${project.id}-${project.updated_at}`} project={project} penNameVersionRef={penNameVersionRef} onOpenTemplate={() => setTemplateSession({ projectId: project.id, scopeVersion })} />
      {canUseAI && <AnalysisSettingsCard project={project} />}
      {project.access.role === 'owner' && templateSession?.projectId === project.id && templateSession.scopeVersion === scopeVersion && <Suspense fallback={null}>
        <SaveNovelTemplateModal key={`${project.id}:${scopeVersion}`} projectId={project.id} projectTitle={project.title} onClose={() => setTemplateSession(null)} onSaved={() => setTemplateSession(null)} />
      </Suspense>}
    </Space>
  )
}

/** 署名独立写入：只合并笔名，不重建基本信息草稿。 */
function ProjectAuthor({ project, penNameVersionRef }: { project: Project; penNameVersionRef: MutableRefObject<number> }) {
  const { message } = App.useApp()
  const setProject = useProjectStore((s) => s.setProject)
  const [saving, setSaving] = useState(false)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])

  const change = async (penNameId: number | null) => {
    if (saving || project.access.role !== 'owner') return
    const scope = captureProjectScope(project.id)
    const isCurrent = () => mounted.current && isProjectScopeCurrent(scope)
      && useProjectStore.getState().project?.access.role === 'owner'
    if (!isCurrent()) return
    setSaving(true)
    try {
      const result = await penNamesApi.setProject(project.id, penNameId, { isCurrent })
      const current = useProjectStore.getState().project
      if (!isCurrent() || !current || result.project_id !== current.id) return
      penNameVersionRef.current += 1
      setProject({ ...current, pen_name: result.pen_name }, scope)
      message.success('作品署名已更新')
    } catch (err) {
      if (isCurrent()) message.error(errorMessage(err))
    } finally {
      if (isCurrent()) setSaving(false)
    }
  }

  return (
    <Card size="small" title="作品署名">
      {project.access.role === 'owner' ? <div style={{ maxWidth: 360 }}>
        <PenNameSelect value={project.pen_name?.id ?? null} currentName={project.pen_name?.name} disabled={saving} onChange={(value) => void change(value)} />
        <Typography.Text type="secondary">未设置时显示账号名；可在我的作品中管理笔名。</Typography.Text>
      </div> : <Typography.Text>{project.pen_name?.name ?? project.owner?.username ?? '未设置'}</Typography.Text>}
    </Card>
  )
}

/** 章节分析设置：自动分析开关（PATCH auto_analyze）与一键分析全部未分析 / 已过期章节。 */
function AnalysisSettingsCard({ project }: { project: Project }) {
  const { message } = App.useApp()
  const { canUseAINow, canManageCollaboratorsNow } = useProjectAccess(project.id)
  const setProject = useProjectStore((s) => s.setProject)
  const refreshTasks = useTasksStore((s) => s.refresh)
  const [toggling, setToggling] = useState(false)
  const [queueing, setQueueing] = useState(false)

  const handleToggle = async (checked: boolean) => {
    if (!canUseAINow() || !canManageCollaboratorsNow()) return
    const scope = captureProjectScope(project.id)
    if (!scope) return
    setToggling(true)
    try {
      const updated = await projectApi.update(project.id, { auto_analyze: checked })
      if (!canUseAINow() || !canManageCollaboratorsNow() || !isProjectScopeCurrent(scope)) return
      // 只合并 auto_analyze：若整体替换，updated_at 变化会让以其为 key 的项目信息表单重建、丢掉未保存的修改
      const current = useProjectStore.getState().project
      if (current?.id === updated.id) setProject({ ...current, auto_analyze: updated.auto_analyze }, scope)
      message.success(checked ? '已开启自动分析' : '已关闭自动分析')
    } catch (err) {
      if (isProjectScopeCurrent(scope)) message.error(errorMessage(err))
    } finally {
      setToggling(false)
    }
  }

  const handleAnalyzePending = async () => {
    if (!canUseAINow()) return
    const scope = captureProjectScope(project.id)
    if (!scope) return
    setQueueing(true)
    try {
      const r = await analysisApi.analyzePending(project.id)
      if (!canUseAINow() || !isProjectScopeCurrent(scope)) return
      if (r.queued > 0) message.success(`已加入 ${r.queued} 章分析，按章节顺序依次执行，可在后台任务中查看`)
      else message.info('没有需要分析的章节（未分析或已过期且正文非空的章节会被加入）')
      void refreshTasks()
    } catch (err) {
      if (isProjectScopeCurrent(scope)) message.error(errorMessage(err))
    } finally {
      setQueueing(false)
    }
  }

  return (
    <Card title="章节分析">
      <Space direction="vertical">
        <Space wrap>
          {project.access.role === 'owner' ? <Switch checked={project.auto_analyze} loading={toggling} onChange={(v) => void handleToggle(v)} />
            : <Typography.Text>{project.auto_analyze ? '已开启（由作者管理）' : '已关闭（由作者管理）'}</Typography.Text>}
          <Typography.Text>整章 AI 生成 / 润色 / 按建议重写成功后自动分析一次</Typography.Text>
        </Space>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          开启后，整章全部场景成功并统一保存后，只自动分析本章一次，会增加一次 AI 调用：提炼摘要、情节点与钩子，
          回写角色 / 关系 / 境界 / 组织变化，给出评分与改进建议；下一章生成时用分析摘要作前情提要。
          单场景生成或改稿、手动改稿不会触发自动分析；正文变化会把已有章分析标记为「已过期」，不把局部结果当作全章事实。
        </Typography.Text>
        <Button icon={<ExperimentOutlined />} loading={queueing} onClick={() => void handleAnalyzePending()}>
          分析全部未分析 / 已过期章节
        </Button>
      </Space>
    </Card>
  )
}

function OverviewInner({ project, penNameVersionRef, onOpenTemplate }: { project: Project; penNameVersionRef: MutableRefObject<number>; onOpenTemplate: () => void }) {
  const { message } = App.useApp()
  const { managingOther, isCollaborator, canUseAI, canUseOwnerAI, canUseOwnerAINow, canEdit, canEditNow, canRecycle, canExport, canBackup } = useProjectAccess(project.id)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const navigate = useNavigate()
  const setProject = useProjectStore((s) => s.setProject)
  const [form] = Form.useForm<ProjectUpdate & ProjectInfoValues>()
  const [saving, setSaving] = useState(false)
  const [exportOpen, setExportOpen] = useState(false)
  const [backupOpen, setBackupOpen] = useState(false)
  const [inspiring, setInspiring] = useState(false)
  const progress = wordsProgress(project.total_words, project.target_words)

  /** 从本项目发起灵感会话（已有字段预填为已定；同一项目已有进行中的会话时直接打开它） */
  const handleInspire = async () => {
    if (!canUseOwnerAINow()) return
    const scope = captureProjectScope(project.id)
    if (!scope) return
    setInspiring(true)
    try {
      // 按需加载：灵感模式的接口模块不进入口 chunk
      const { inspirationApi } = await import('../../api/inspiration')
      if (!canUseOwnerAINow() || !isProjectScopeCurrent(scope)) return
      const s = await inspirationApi.fromProject(project.id)
      if (!canUseOwnerAINow() || !isProjectScopeCurrent(scope)) return
      navigate(`/inspiration/${s.id}`)
    } catch (err) {
      if (isProjectScopeCurrent(scope)) message.error(errorMessage(err))
    } finally {
      setInspiring(false)
    }
  }

  const handleSave = async () => {
    if (!canEditNow()) return
    const v = await form.validateFields()
    if (!canEditNow()) return
    const scope = captureProjectScope(project.id)
    if (!scope) return
    const currentPenNameVersion = penNameVersionRef.current
    setSaving(true)
    try {
      if (!canEditNow()) return
      const updated = await projectApi.update(project.id, {
        title: v.title?.trim(),
        genre: v.genre?.trim() ?? '',
        premise: v.premise?.trim() ?? '',
        status: v.status,
        // 显式 null = 取消项目默认风格
        ...(!managingOther ? { default_style_id: v.default_style_id ?? null } : {}),
        // 目标总字数清空时显式发送 null
        ...projectInfoBody(v),
      })
      if (!canEditNow() || !isProjectScopeCurrent(scope)) return
      const current = useProjectStore.getState().project
      // 署名保存先于本请求返回时，保留本轮已确认的署名，避免旧表单响应覆盖。
      setProject(current && current.id === updated.id && currentPenNameVersion !== penNameVersionRef.current
        ? { ...updated, pen_name: current.pen_name } : updated, scope)
      message.success('已保存')
      if (updated.notice) message.info(updated.notice)
    } catch (err) {
      if (!canEditNow() || !isProjectScopeCurrent(scope)) return
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }


  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <div style={splitRow}>
        <div style={splitPane(200)}>
          <Card>
            <Statistic title="章节数" value={project.chapter_count} />
          </Card>
        </div>
        <div style={splitPane(200)}>
          <Card>
            <Statistic
              title="总字数"
              value={project.total_words}
              suffix={
                project.target_words ? (
                  <Typography.Text type="secondary" style={{ fontSize: 14 }}>
                    / {formatWordCount(project.target_words)}
                  </Typography.Text>
                ) : undefined
              }
            />
            {progress !== null ? (
              <Progress percent={progress} size="small" style={{ marginBottom: 0 }} />
            ) : (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                设置目标总字数后显示写作进度
              </Typography.Text>
            )}
          </Card>
        </div>
        {(canUseAI || canExport || canBackup || project.access.role === 'owner') && <div style={splitPane(400)}>
          <Card>
            <Space wrap>
              {canUseAI && <Button icon={<RocketOutlined />} onClick={() => navigate(`/projects/${project.id}/wizard`)}>
                创作向导
              </Button>}
              {canUseOwnerAI && <Button icon={<BulbOutlined />} loading={inspiring} onClick={() => void handleInspire()}>
                用灵感模式补全
              </Button>}
              {canExport && <Button type="primary" icon={<DownloadOutlined />} onClick={() => setExportOpen(true)}>
                导出 TXT / Markdown / Word / EPUB
              </Button>}
              {canBackup && <Button icon={<CloudDownloadOutlined />} onClick={() => setBackupOpen(true)}>
                备份项目
              </Button>}
              {project.access.role === 'owner' && <Button onClick={onOpenTemplate}>保存为小说模板</Button>}
            </Space>
          </Card>
        </div>}
      </div>
      {!managingOther && <Card size="small" title="作品系列">
        {project.series ? <Link to={`/series/${project.series.id}`}>{project.series.name} · 管理成员与共享设定</Link>
          : <Link to="/series">尚未加入系列 · 前往系列管理</Link>}
      </Card>}
      <Card title="基本信息">
        <div style={splitRow}>
          <div style={{ flex: '0 0 180px', maxWidth: '100%' }}>
            <CoverPanel project={project} />
          </div>
          <div style={splitPane(360)}>
            <Form
              form={form}
              layout="vertical"
              initialValues={{
                title: project.title,
                genre: project.genre,
                premise: project.premise,
                default_style_id: project.default_style_id,
                status: project.status,
                synopsis: project.synopsis,
                theme: project.theme,
                tags: project.tags,
                selling_points: project.selling_points,
                audience: project.audience,
                target_words: project.target_words,
              }}
              style={{ maxWidth: 760 }} disabled={!canEdit}
            >
              <div style={formRow}>
                <div style={splitPane(360)}>
                  <Form.Item
                    name="title"
                    label="书名"
                    rules={[
                      { required: true, whitespace: true, message: '请输入书名' },
                      { max: 200, message: '不超过 200 字' },
                    ]}
                  >
                    <Input />
                  </Form.Item>
                </div>
                <div style={{ ...splitPane(360), ...formRow }}>
                  <div style={splitPane(172)}>
                    <Form.Item name="genre" label="题材" rules={[{ max: 100, message: '不超过 100 字' }]}>
                      <Input />
                    </Form.Item>
                  </div>
                  <div style={splitPane(172)}>
                    <Form.Item name="status" label="状态">
                      <Select<ProjectStatus>
                        options={(Object.keys(PROJECT_STATUS) as ProjectStatus[]).map((s) => ({
                          value: s,
                          label: PROJECT_STATUS[s].label,
                        }))}
                      />
                    </Form.Item>
                  </div>
                </div>
              </div>
              <Form.Item name="premise" label="一句话设定" rules={[{ max: 5000, message: '不超过 5000 字' }]}>
                <Input.TextArea autoSize={{ minRows: 2, maxRows: 8 }} />
              </Form.Item>
              <ProjectInfoFields />
              {!managingOther ? <Form.Item
                name="default_style_id"
                label="默认写作风格"
                extra={
                  <span>
                    章节生成、润色、重写与局部改写默认使用该风格（章节可单独覆盖）；向导与设定生成只带它的一句话描述。
                    <Link to="/styles">去风格库管理</Link>
                  </span>
                }
              >
                <StyleSelect placeholder="不用风格" />
              </Form.Item> : <Form.Item label="默认写作风格"><Typography.Text>{project.default_style?.name ?? (project.default_style_id ? `风格 #${project.default_style_id}` : '不用风格')}</Typography.Text><Typography.Text type="secondary">（保留当前绑定）</Typography.Text></Form.Item>}
              <Space>
                <Button type="primary" loading={saving} onClick={() => void handleSave()} disabled={!canEdit}>
                  保存
                </Button>
                {canRecycle && <Button danger onClick={() => setDeleteOpen(true)}>
                  删除项目
                </Button>}
              </Space>
            </Form>
            <Descriptions size="small" style={{ marginTop: 24 }} column={{ xs: 1, sm: 2 }}>
              <Descriptions.Item label="创建时间">{formatTime(project.created_at)}</Descriptions.Item>
              <Descriptions.Item label="更新时间">{formatTime(project.updated_at)}</Descriptions.Item>
            </Descriptions>
          </div>
        </div>
      </Card>
      {canExport && <ExportModal projectId={project.id} open={exportOpen} onClose={() => setExportOpen(false)} />}
      {deleteOpen && canRecycle && <DeleteProjectModal project={{ id: project.id, title: project.title, owner: project.owner, owner_username: project.owner?.username }} onClose={() => setDeleteOpen(false)} onDeleted={() => navigate(managingOther && !isCollaborator ? '/admin/projects' : '/projects', { replace: true })} />}
      {canBackup && backupOpen && (
        <Suspense fallback={null}>
          <BackupModal projectId={project.id} onClose={() => setBackupOpen(false)} />
        </Suspense>
      )}
    </Space>
  )
}
