import { canStreamTask } from '../api/tasks'
import type { Project, ProjectAccess, Task, TaskKind, TaskRetryBlockedReason, User } from '../api/types'

const ACCESS_FIELDS = [
  'is_collaborator', 'allow_owner_ai', 'can_read', 'can_edit_content', 'can_export', 'can_use_ai',
  'can_manage_collaborators', 'can_recycle', 'can_backup', 'can_write_reading_progress',
] as const satisfies readonly (keyof ProjectAccess)[]

/** 不完整的旧响应或格式错误的权限快照一律不可用。 */
export function isProjectAccess(value: unknown): value is ProjectAccess {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const fields = value as Record<string, unknown>
  return (fields.role === null || fields.role === 'owner' || fields.role === 'viewer'
    || fields.role === 'editor' || fields.role === 'admin')
    && ACCESS_FIELDS.every((field) => typeof fields[field] === 'boolean')
    && fields.is_collaborator === (fields.role === 'viewer' || fields.role === 'editor')
    && (fields.is_collaborator
      ? typeof fields.authorization_version === 'string' && fields.authorization_version.length > 0
      : fields.authorization_version === null)
}

/** 只控制界面能力；真实授权仍由服务器按每次请求校验。 */
export function projectAccess(
  project: Pick<Project, 'id' | 'owner' | 'access'> | null | undefined,
  user: Pick<User, 'id' | 'is_super_admin' | 'effective_permissions'> | null | undefined,
  projectId: number,
) {
  const snapshot = project?.access
  const ready = Boolean(project && project.id === projectId && user && isProjectAccess(snapshot)
    && (snapshot.role !== 'owner' || !project.owner || project.owner.id === user.id))
  const current = ready && isProjectAccess(snapshot) ? snapshot : null
  return {
    ready,
    role: current?.role ?? null,
    isCollaborator: current?.is_collaborator === true,
    allowOwnerAI: current?.allow_owner_ai === true,
    managingOther: current?.role != null && current.role !== 'owner',
    canRead: current?.can_read === true,
    canEdit: current?.can_edit_content === true,
    canRecycle: current?.can_recycle === true,
    canExport: current?.can_export === true,
    canManageCollaborators: current?.can_manage_collaborators === true,
    canBackup: current?.can_backup === true,
    canWriteReadingProgress: current?.can_write_reading_progress === true,
    // 服务端 can_use_ai = 作者或 accepted 编辑者；已开放的章节/分析/批量/向导/大纲/实体 AI 及改写按此显示。
    canUseAI: current?.can_use_ai === true,
    /** 与 canUseAI 相同，保留给既有调用方（封面、富文档审核、知识库上传）。 */
    canUseAIEntitled: current?.can_use_ai === true,
    // 知识库/记忆索引与检索调试、灵感等尚未对协作者开放的 AI 入口仍只对作者显示。
    canUseOwnerAI: current?.role === 'owner' && current.can_use_ai,
    /**
     * 项目智能体写能力（新建会话 / 发送 / 确认 / 拒绝 / 撤销）：服务端 can_use_ai 且来源为作者或 accepted 协作者
     * （裁决 1）；管理员人工 projects.edit 不并入。单个会话能否写另看服务端 `writable`（裁决 23）。
     */
    canUseAgent: current?.can_use_ai === true && (current.role === 'owner' || current.is_collaborator),
    /** 打开智能体面板：可写者，或只读查看本人会话的协作者（含查看者、管理员兼查看者，裁决 4）；纯管理员不显示。 */
    canViewAgent: (current?.can_use_ai === true && current.role === 'owner') || current?.is_collaborator === true,
    /** 协作授权轮次（作者 / 非协作者为 null）：智能体状态按作品 + 轮次键控，授权变化不沿用旧可写状态。 */
    authorizationVersion: current?.authorization_version ?? null,
  }
}

export type AIModelSource = 'loading' | 'own' | 'owner' | 'none'

/**
 * 当前上下文发起 AI 时的模型来源（只控制界面，真实选择与费用归属以后端为准）：
 * - access 为 null 表示作品外（账号级）上下文：只看本人配置；
 * - own：本人有该类型配置（显式选择或本人默认）；
 * - owner：本人没有该类型配置，且为已获作者模型授权的 accepted 编辑者，留空即回退作者默认配置；
 * - none：不可发起；loading：本人配置或作品权限快照尚未就绪，不据此禁用。
 * 作者本人永远不走 owner；本人已有配置时不回退（无效配置由后端明确报错）。
 */
export function aiModelSource(
  access: Pick<ReturnType<typeof projectAccess>, 'ready' | 'role' | 'isCollaborator' | 'allowOwnerAI' | 'canUseAI'> | null,
  loaded: boolean,
  configCount: number,
): AIModelSource {
  if (access && !access.ready) return 'loading'
  if (access && !access.canUseAI) return 'none'
  if (!loaded) return 'loading'
  if (configCount > 0) return 'own'
  return access?.role === 'editor' && access.isCollaborator && access.allowOwnerAI ? 'owner' : 'none'
}

/**
 * 个人后台任务面板（只列本人任务，以后端返回为准）：作品外始终显示；作品内对可用 AI 的作者/编辑者，
 * 以及降为查看者后仍可读取本人已结束记录的协作者显示；纯管理员代管他人作品不显示。
 */
export function showsTaskPanel(projectId: number | null, access: { canUseAI: boolean; isCollaborator: boolean }) {
  return projectId === null || access.canUseAI || access.isCollaborator
}

/**
 * 任务面板中重试 / 继续 / 结束 / 取消等会重新使用 AI 授权的操作：属于当前已加载作品时按 canUseAI
 * （查看者、纯管理员会被后端拒绝，直接隐藏）；不属于当前作品或权限快照未就绪时交由后端判定。
 * 删除已结束记录不需要 AI 授权，不受此限制。
 */
export function taskControlsAllowed(
  taskProjectId: number | null,
  project: Pick<Project, 'id' | 'owner' | 'access'> | null | undefined,
  user: Pick<User, 'id' | 'is_super_admin' | 'effective_permissions'> | null | undefined,
) {
  if (taskProjectId === null || !project || project.id !== taskProjectId) return true
  const access = projectAccess(project, user, taskProjectId)
  return !access.ready || access.canUseAI
}

/** 记忆条目绑定的向量任务失败后的补救说明（任务面板与协作者记忆页共用）。 */
export const COLLABORATOR_MEMORY_RETRY_HINT = '无法直接重试；可重新编辑此条或由有权限者全书重建'

const RETRY_BLOCKED_HINTS: Record<TaskRetryBlockedReason, string> = {
  memory_bound: COLLABORATOR_MEMORY_RETRY_HINT,
  legacy_index: '旧版索引任务无法直接重试；请在知识库中重新索引该文档',
  binding_invalid: '原任务执行信息无法重放，无法直接重试；请重新发起',
  request_invalid: '原任务执行信息无法重放，无法直接重试；请重新发起',
}

/**
 * 服务端只读字段 retry_blocked_reason 非空的任务不显示重试，改为补救说明（E6-f 裁决 19）。
 * 只看服务端判定：不按标题识别，也不依赖任务是否属于当前已加载作品（跨作品同样生效）。
 */
export function taskRetryBlockedHint(task: Pick<Task, 'retry_blocked_reason'>): string | null {
  const reason = task.retry_blocked_reason
  return reason ? RETRY_BLOCKED_HINTS[reason] ?? RETRY_BLOCKED_HINTS.binding_invalid : null
}

/**
 * 任务输出弹窗：stream = 实时订阅；output = 读取已结束输出；status = 只显示状态（协作者不可订阅的类型，
 * 或查看者遇到仍在进行的任务）；denied = 会话或作品授权已变化。
 * scoped=false 表示任务不属于当前已加载作品，由后端按本人任务校验。
 */
export function taskOutputMode(input: {
  sessionCurrent: boolean
  scoped: boolean
  live: boolean
  kind: TaskKind
  canRead: boolean
  canUseAI: boolean
  isCollaborator: boolean
}): 'stream' | 'output' | 'status' | 'denied' {
  if (!input.sessionCurrent || (input.scoped && !input.canRead)) return 'denied'
  if (!input.live) return 'output'
  if (!input.scoped) return 'stream'
  return input.canUseAI && canStreamTask(input.kind, input.isCollaborator) ? 'stream' : 'status'
}

/** 危险操作必须输入完整名称，空输入和首尾多余空白均不算确认。 */
export function canConfirmProjectDelete(input: string, title: string) {
  return input.length > 0 && input === title
}
