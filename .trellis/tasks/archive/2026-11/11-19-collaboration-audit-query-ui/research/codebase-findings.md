# 代码库调研：协作审计与管理员操作日志

## 1. 现有协作审计数据层与写入契约

- 模型：`backend/app/models/collaboration.py:72-98`（`CollaborationAuditEvent`，表 `collaboration_audit_events`）
- 迁移：`backend/alembic/versions/0044_novel_collaborators.py:73-101`
  - 无业务外键，删除用户、作品或系列后审计快照仍保留。
  - 索引：
    - `ix_collaboration_audit_scope_resource (scope, resource_id, occurred_at, id)`
    - `ix_collaboration_audit_actor_time (actor_user_id, occurred_at, id)`
    - `operation_id` 唯一约束
  - 降级保护：`collaboration_audit_events` 有数据时拒绝降级。
  - 备份边界：不进单书 JSON 备份（`project_transfer`），仅随整库备份。
- 字段清单：
  - `id`、`operation_id`（UUID）、`occurred_at`（UTC DateTime）
  - `scope`: `'project' | 'series'`
  - `resource_id`: int（`>= 0`；仅接受/拒绝不存在或他人邀请的失败记录为 `0`）
  - `resource_label`: str ≤ 200 可空（作品标题或系列名快照）
  - `invitation_id`: UUID 可空
  - `actor_user_id` / `actor_username`（≤ 64，可空）
  - `target_user_id` / `target_username`（≤ 64，可空）
  - `action`: `'invite' | 'accept' | 'decline' | 'modify' | 'revoke'`
  - `result`: `'success' | 'failure'`
  - `error_code`: 白名单 12 项（`bad_request`、`unauthorized`、`conflict`、`not_found`、`permission_denied`、`invitee_unavailable`、`stale_invitation`、`cancelled`、`already_collaborator`、`self_invite`、`collaboration_busy`、`system_error`）
  - `summary`: JSON 白名单 6 键（`before_role`、`after_role`、`before_status`、`after_status`、`before_allow_owner_ai`、`after_allow_owner_ai`）；失败记录仅保留 `before_*`。
- 写入位置：`backend/app/services/collaboration_audit.py` 与 `backend/app/services/collaboration_service.py`
  - 成功与业务同事务；失败在回滚后独立写入。
  - 当前没有任何查询 service 或 HTTP 端点。

## 2. 发现的关键安全与数据边界（重要）

1. **禁用账号与不存在账号的防枚举风险**（`backend/app/services/collaboration_service.py:242-254`）：
   - 作者邀请**不存在**用户时：失败审计记录 `target_user_id=None, target_username=None, error_code='invitee_unavailable'`。
   - 作者邀请**已禁用**用户时：因第 247 行在校验 `is_active` 前已执行 `state.update(target_user_id=target.id, target_username=target.username)`，失败审计记录了真实的 `target_user_id` 和 `target_username`，`error_code='invitee_unavailable'`。
   - 现有协作需求（`11-09-novel-collaborators/prd.md` R1）明确要求「不存在和已禁用的用户名返回同一安全提示；不提供模糊搜索或用户目录」。
   - 因此面向作者的查询响应若原样暴露 `invitee_unavailable` 失败记录上的 `target_user_id`/`target_username`，会导致作者通过查审计来枚举某用户名是「不存在」还是「已禁用」。作者视图必须脱敏或抹平两者差异（且需同步评估写入侧或投影侧处理）。
2. **非作者越权尝试记录在目标 `resource_id` 下**（`backend/app/services/collaboration_service.py:242-248, 296`）：
   - 陌生用户或协作者尝试对他人作品/系列调用 `POST/PATCH/DELETE .../collaborators` 时，`_lock_parent(..., owner_only=True)` 抛 `NotFoundError`，失败审计会写入该 `resource_id`、`actor_user_id=stranger.id`、`actor_username=stranger.username`、`error_code='not_found'`（甚至 `invite` 时还带有目标用户名）。
   - 向作者展示此类失败会暴露无关用户的探测与用户名，作者侧需要明确过滤或界定可见范围；而管理员若可看全站协作审计，此类越权尝试反而具有安全审计价值。
3. **`resource_id = 0` 的无法归属记录**（`backend/app/services/collaboration_service.py:358-362, 388`）：
   - 用户尝试接受/拒绝不存在或他人的 `invitation_id` 时，记录 `resource_id = 0`、`resource_label = None`、`error_code = 'not_found'`（或禁用/失效账号的 `unauthorized`）。
   - 这类记录不属于任何作品或系列，作者侧查询永远查不到，只有全站管理员视角才能查看。
4. **已删除/回收站作品的审计保留**：
   - `collaboration_service.list_members` 对回收站/删除中作品返回 404（`project_lifecycle.ensure_active(parent)`）。
   - 作品被彻底删除或系列被删除后，审计快照仍留在 `collaboration_audit_events` 中（按 `11-09-novel-collaborators/prd.md` R3/R6 设计），但作者已无法打开该作品；只有全站管理员视图能核查已删除作品/系列的协作历史。

## 3. 现有前端协作入口与管理员操作日志

- **作品协作者管理**：`frontend/src/pages/project/ProjectLayout.tsx:51, 63-65`
  - 仅 `canManageCollaborators`（即作品作者）可见顶部「协作者」按钮，打开 520px 宽抽屉「作品协作者」，内部渲染 `<MemberPanel scope="project" resourceId={pid} canManage />`。
  - 左侧导航定义在 `frontend/src/components/StudioNavigation.tsx`。
- **系列设定协作者管理**：`frontend/src/pages/SeriesPage.tsx:107-109`
  - 系列作者在系列详情页底部 `<Card>` 内直接渲染 `<MemberPanel scope="series" resourceId={id} canManage />`。
- **管理员操作日志**：
  - 后端：`backend/app/api/v1/admin_audit.py`（`/api/v1/admin/audit-logs`）、`backend/app/services/audit_service.py`、`backend/app/schemas/audit.py`
  - 权限：`backend/app/core/permissions.py:33-34` 已有 `audit.read`（查看操作日志摘要）和 `audit.cleanup`（清理操作日志，依赖 `audit.read`）；当前共 38 项权限。
  - 前端：`frontend/src/pages/AdminAuditLogsPage.tsx`（路由 `/admin/audit-logs`，`RequirePermission("audit.read")`）、`frontend/src/utils/auditLogs.ts`（本地日期转 UTC 半开区间 `[from_at, to_at)`，含结束日整天）、`frontend/src/api/auditLogs.ts`。
  - 清理机制：`audit.cleanup` 仅按日期 + 高水位删除 `admin_audit_logs` 中 `action != 'audit.cleanup'` 的记录，不触碰 `collaboration_audit_events`。
  - 规范：`.trellis/spec/backend/admin-audit.md` 明确 `admin_audit_logs` 不自动采集普通用户日常行为；`collaboration_audit_events` 已是独立表。
