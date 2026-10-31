import { api } from './client'
import type { AuditAction, AuditCleanupPreview, AuditCleanupRequest, AuditCleanupResult, AuditDateRange, AuditField, AuditLogBrief, AuditLogDetail, AuditLogListQuery, AuditResult, AuditSafeState, AuditTargetType, Page } from './types'

const base = '/admin/audit-logs'

export const auditLogApi = {
  list: ({ actor_q, ...query }: AuditLogListQuery = {}) =>
    api.get<Page<AuditLogBrief>>(base, { ...query, actor_q: actor_q?.trim() || undefined }),
  get: (id: number) => api.get<AuditLogDetail>(`${base}/${id}`),
  previewCleanup: (range: AuditDateRange) => api.post<AuditCleanupPreview>(`${base}/cleanup-preview`, range),
  cleanup: (body: AuditCleanupRequest) => api.post<AuditCleanupResult>(`${base}/cleanup`, body),
}

export const AUDIT_ACTION: Record<AuditAction, string> = {
  'user.create': '创建用户', 'user.update': '修改用户角色 / 状态', 'user.reset_password': '重置密码', 'user.delete': '删除用户',
  'project.update': '修改作品', 'project.delete': '删除作品',
  'chapter.create': '创建章节', 'chapter.create_from_outlines': '从大纲批量建章', 'chapter.update': '修改章节', 'chapter.delete': '删除章节', 'chapter.cast.update': '调整章节选角',
  'chapter_version.update': '修改版本标星 / 备注', 'chapter_version.delete': '删除章节版本', 'chapter_version.restore': '恢复章节版本',
  'workshop.publish': '发布工坊资源', 'workshop.approve': '通过工坊审核', 'workshop.reject': '驳回工坊审核', 'workshop.update': '修改工坊元数据', 'workshop.hide': '下架工坊资源', 'workshop.unhide': '恢复工坊资源', 'workshop.delete': '删除工坊资源',
  'skill.create': '创建全局 Skill', 'skill.import': '导入全局 Skill', 'skill.update': '修改全局 Skill', 'skill.references.replace': '替换 Skill 参考资料', 'skill.delete': '删除全局 Skill', 'skill.restore_builtin': '恢复内置 Skill',
  'audit.cleanup': '清理审计日志',
}

export const AUDIT_TARGET: Record<AuditTargetType, string> = {
  user: '用户', project: '作品', chapter: '章节', chapter_version: '章节版本', workshop_item: '工坊资源', workshop_submission: '工坊投稿', skill: '全局 Skill', audit: '审计日志',
}

export const AUDIT_RESULT: Record<AuditResult, { label: string; color: string }> = {
  success: { label: '成功', color: 'success' }, failure: { label: '失败', color: 'error' },
}

export const AUDIT_STATE: Record<AuditSafeState, string> = {
  draft: '草稿', planning: '筹备中', writing: '创作中', completed: '已完结', archived: '已归档', generating: '生成中', done: '已完成', active: '已启用', hidden: '已下架', admin: '管理员', author: '作者', pending: '待审核', approved: '已通过', rejected: '已驳回', cancelled: '已取消',
}

export const AUDIT_FIELD: Record<AuditField, string> = {
  username: '用户名', is_admin: '管理员角色', is_active: '启用状态', title: '标题', genre: '题材', premise: '创意', synopsis: '简介', theme: '主题', tags: '标签',
  selling_points: '卖点', audience: '目标读者', target_words: '目标字数', status: '状态', style: '写作风格', default_style_id: '默认风格 ID', auto_analyze: '自动分析',
  chapter_no: '章节号', outline_id: '大纲 ID', plan_id: '细纲 ID', style_id: '风格 ID', content: '正文内容', starred: '标星', note: '版本备注', cast: '章节选角',
  slug: '标识', name: '名称', description: '描述', category: '分类', triggers: '触发词', body: '资源正文', references: '参考资料', builtin_key: '内置标识', hidden_by: '下架方',
}
