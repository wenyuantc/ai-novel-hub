import { api } from './client'
import type { AuditAction, AuditCleanupPreview, AuditCleanupRequest, AuditCleanupResult, AuditDateRange, AuditField, AuditLogBrief, AuditLogDetail, AuditLogListQuery, AuditResult, AuditSafeState, AuditTargetType, BackupSettingKey, Page } from './types'
import { SYSTEM_SETTING_LABELS } from '../utils/systemSettings'

const base = '/admin/audit-logs'

export const auditLogApi = {
  list: ({ actor_q, ...query }: AuditLogListQuery = {}) =>
    api.get<Page<AuditLogBrief>>(base, { ...query, actor_q: actor_q?.trim() || undefined }),
  get: (id: number) => api.get<AuditLogDetail>(`${base}/${id}`),
  previewCleanup: (range: AuditDateRange) => api.post<AuditCleanupPreview>(`${base}/cleanup-preview`, range),
  cleanup: (body: AuditCleanupRequest) => api.post<AuditCleanupResult>(`${base}/cleanup`, body),
}

export const AUDIT_ACTION: Record<AuditAction, string> = {
  'planning.update': '维护作品规划',
  'role.create': '创建角色', 'role.update': '修改角色', 'role.delete': '删除角色',
  'user.roles.update': '分配用户角色', 'user.roles.bootstrap': '引导超级管理员', 'user.quotas.update': '修改用户配额',
  'system_settings.update': '修改系统设置',
  'backup.settings.update': '保存备份配置', 'backup.run.accept': '受理整库备份', 'backup.download.authorize': '授权下载备份',
  'task.cancel_request': '受理任务取消请求',
  'user.create': '创建用户', 'user.update': '修改用户角色 / 状态', 'user.reset_password': '重置密码', 'user.delete': '删除用户',
  'project.update': '修改作品', 'project.delete': '删除作品',
  'project.restore': '恢复回收站作品', 'project.purge': '彻底删除作品', 'project.expire': '到期自动清理作品',
  'chapter.create': '创建章节', 'chapter.create_from_outlines': '从大纲批量建章', 'chapter.update': '修改章节', 'chapter.delete': '删除章节', 'chapter.cast.update': '调整章节选角',
  'chapter_version.update': '修改版本标星 / 备注', 'chapter_version.delete': '删除章节版本', 'chapter_version.restore': '恢复章节版本',
  'workshop.publish': '发布工坊资源', 'workshop.approve': '通过工坊审核', 'workshop.reject': '驳回工坊审核', 'workshop.update': '修改工坊元数据', 'workshop.hide': '下架工坊资源', 'workshop.unhide': '恢复工坊资源', 'workshop.delete': '删除工坊资源',
  'skill.create': '创建全局 Skill', 'skill.import': '导入全局 Skill', 'skill.update': '修改全局 Skill', 'skill.references.replace': '替换 Skill 参考资料', 'skill.delete': '删除全局 Skill', 'skill.restore_builtin': '恢复内置 Skill',
  'audit.cleanup': '清理审计日志',
}

export const AUDIT_TARGET: Record<AuditTargetType, string> = {
  role: '角色', backup_settings: '备份配置', backup_run: '整库备份',
  user: '用户', project: '作品', chapter: '章节', chapter_version: '章节版本', workshop_item: '工坊资源', workshop_submission: '工坊投稿', skill: '全局 Skill', audit: '审计日志', task: '任务', system_settings: '系统设置',
}

export const AUDIT_SYSTEM_SETTING = SYSTEM_SETTING_LABELS

export const AUDIT_RESULT: Record<AuditResult, { label: string; color: string }> = {
  success: { label: '成功', color: 'success' }, failure: { label: '失败', color: 'error' },
}

/** 受理、保存、下载授权分别表达真实审计边界，不冒充备份或下载完成。 */
export function auditResult(log: Pick<AuditLogBrief, 'action' | 'result'>) {
  if (log.result !== 'success') return AUDIT_RESULT[log.result]
  if (log.action === 'task.cancel_request' || log.action === 'backup.run.accept') return { ...AUDIT_RESULT.success, label: '已受理' }
  if (log.action === 'backup.download.authorize') return { ...AUDIT_RESULT.success, label: '已授权下载' }
  if (log.action === 'backup.settings.update') return { ...AUDIT_RESULT.success, label: '已保存' }
  return AUDIT_RESULT.success
}

export const AUDIT_STATE: Record<AuditSafeState, string> = {
  draft: '草稿', planning: '筹备中', writing: '创作中', completed: '已完结', archived: '已归档', generating: '生成中', done: '已完成', active: '已启用', hidden: '已下架', admin: '管理员', author: '作者', pending: '待审核', approved: '已通过', rejected: '已驳回', cancelled: '已取消',
}

export const AUDIT_FIELD: Record<AuditField, string> = {
  setting_fields: '设定字段值', field_definitions: '设定字段配置',
  setting_categories: '设定分类', setting_tags: '整理标签',
  username: '用户名', is_admin: '管理员角色', is_active: '启用状态', title: '标题', genre: '题材', premise: '创意', synopsis: '简介', theme: '主题', tags: '标签',
  selling_points: '卖点', audience: '目标读者', target_words: '目标字数', status: '状态', style: '写作风格', default_style_id: '默认风格 ID', auto_analyze: '自动分析',
  chapter_no: '章节号', outline_id: '大纲 ID', plan_id: '细纲 ID', style_id: '风格 ID', content: '正文内容', starred: '标星', note: '版本备注', cast: '章节选角',
  slug: '标识', name: '名称', description: '描述', category: '分类', triggers: '触发词', body: '资源正文', references: '参考资料', builtin_key: '内置标识', hidden_by: '下架方',
}


export const AUDIT_BACKUP_FIELD: Record<BackupSettingKey, string> = {
  schedule_enabled: '定时开关', schedule_time: '北京时间', local_enabled: '本地开关', s3_enabled: 'S3开关',
  local_keep: '本地保留份数', s3_keep: 'S3保留份数', encryption_enabled: '文件加密开关', backup_password: '备份密码',
  s3_endpoint: 'S3端点', s3_region: 'S3区域', s3_bucket: 'S3 Bucket', s3_prefix: 'S3前缀',
  s3_access_key: 'S3 Access Key', s3_secret_key: 'S3 Secret Key', smtp_host: 'SMTP主机', smtp_port: 'SMTP端口',
  smtp_security: 'SMTP安全方式', smtp_username: 'SMTP用户名', smtp_password: 'SMTP密码',
  smtp_from_address: '发件人邮箱', smtp_recipients: '接收人邮箱',
}
