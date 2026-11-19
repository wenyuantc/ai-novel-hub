# 技术设计：协作审计查询界面

## 1. 架构概览与边界

本次改动为纯只读查询层 + 前端界面，**不改动数据库表结构（无 Alembic 迁移）**，**不改动 RBAC 38 项权限目录**，**不改动协作写入事务路径**（`collaboration_service._write` 与 `collaboration_audit.add_success/record_failure` 保持原契约）。

```text
作者侧（作品抽屉 / 系列卡片）
  MemberPanel (Tabs: 成员管理 | 协作审计)
    └─ CollaborationAuditPanel
         ├─ GET /api/v1/projects/{project_id}/collaborators/audit-logs
         └─ GET /api/v1/series/{sid}/collaborators/audit-logs
              └─ collaboration_audit_query.list_for_owner(db, actor_id, scope, resource_id, filters)
                   ├─ authz.require_authenticated(db, actor_id)
                   ├─ 严格 owner 校验 + project_lifecycle.ensure_active (仅 project)
                   ├─ 行级范围过滤：排除非作者越权 invite/modify/revoke
                   └─ 目标脱敏：SQL 筛选与序列化统一抹平未确认目标的失败邀请

管理员侧（/admin/audit-logs）
  AdminAuditLogsPage (Tabs: 管理操作日志 | 协作审计)
    ├─ Tab 1: 原管理操作日志（audit.read / audit.cleanup，仅清理 admin_audit_logs）
    └─ Tab 2: AdminCollaborationAuditPanel（仅 audit.read，只读，无清理入口）
         └─ GET /api/v1/admin/audit-logs/collaboration
              └─ collaboration_audit_query.list_for_admin(db, filters)
                   └─ 依赖 require_permission("audit.read")（自动设置 Cache-Control: no-store）
```

### 为什么不新增数据库迁移？
- `collaboration_audit_events` 已在 `0044` 建立联合索引 `ix_collaboration_audit_scope_resource (scope, resource_id, occurred_at, id)` 和 `ix_collaboration_audit_actor_time (actor_user_id, occurred_at, id)`。
- `backend/tests/test_collaboration_migration.py:53-54` 冻结了 `0044` 表的结构契约；现有索引已覆盖作者侧高频按资源时间倒序查询及管理员按操作人筛选，协作事件本身为低频授权动作，无需增加表结构迁移成本与回滚风险。

---

## 2. 后端设计

### 2.1 Schema：`backend/app/schemas/collaboration_audit.py`

与 `backend/app/services/collaboration_audit.py` 共享动作、错误码与摘要白名单常量，避免两处枚举漂移：

- `CollaborationAuditAction = Literal["invite", "accept", "decline", "modify", "revoke"]`
- `CollaborationAuditResult = Literal["success", "failure"]`
- `CollaborationAuditErrorCode = Literal["bad_request", "unauthorized", "conflict", "not_found", "permission_denied", "invitee_unavailable", "stale_invitation", "cancelled", "already_collaborator", "self_invite", "collaboration_busy", "system_error"]`
- `MemberStatus = Literal["pending", "accepted", "declined", "revoked"]`
- `DbInt = Annotated[int, Field(gt=0, le=2_147_483_647)]`、`NonNegativeDbInt = Annotated[int, Field(ge=0, le=2_147_483_647)]`
- `CollaborationAuditSummary`（`extra="forbid"`）：
  - `before_role: Role | None = None`
  - `after_role: Role | None = None`
  - `before_status: MemberStatus | None = None`
  - `after_status: MemberStatus | None = None`
  - `before_allow_owner_ai: bool | None = None`
  - `after_allow_owner_ai: bool | None = None`
- `CollaborationAuditEventOut`：
  - `id: int`、`operation_id: str`、`occurred_at: datetime`（validator 归一化为 UTC aware）
  - `scope: Scope`、`resource_id: int`、`resource_label: str | None`、`invitation_id: str | None`
  - `actor_user_id: int | None`、`actor_username: str | None`
  - `target_user_id: int | None`、`target_username: str | None`
  - `action: CollaborationAuditAction`、`result: CollaborationAuditResult`、`error_code: CollaborationAuditErrorCode | None`
  - `summary: CollaborationAuditSummary`
- `CollaborationAuditList`：`{items: list[CollaborationAuditEventOut], total: int}`

#### 筛选参数模型（复用 `AuditDateRange` 成对 UTC 校验）
- `AuthorCollaborationAuditFilters`：
  - `member_user_id: DbInt | None = None`
  - `member_q: str | None = Field(default=None, max_length=64)`（strip 后为空转为 `None`）
  - `action: CollaborationAuditAction | None = None`
  - `result: CollaborationAuditResult | None = None`
  - `from_at: AwareDatetime | None = None`
  - `to_at: AwareDatetime | None = None`
  - `page: int = Field(default=1, ge=1, le=2_147_483_647)`
  - `page_size: int = Field(default=20, ge=1, le=100)`
- `AdminCollaborationAuditFilters`：
  - `scope: Scope | None = None`
  - `resource_id: NonNegativeDbInt | None = None`（允许 `0` 以查询无法归属的无效邀请失败）
  - `resource_q: str | None = Field(default=None, max_length=200)`（strip 后为空转为 `None`）
  - `actor_user_id: DbInt | None = None`
  - `actor_q: str | None = Field(default=None, max_length=64)`
  - `target_user_id: DbInt | None = None`
  - `target_q: str | None = Field(default=None, max_length=64)`
  - `action: CollaborationAuditAction | None = None`
  - `result: CollaborationAuditResult | None = None`
  - `from_at: AwareDatetime | None = None`
  - `to_at: AwareDatetime | None = None`
  - `page: int = Field(default=1, ge=1, le=2_147_483_647)`
  - `page_size: int = Field(default=20, ge=1, le=100)`

### 2.2 查询服务：`backend/app/services/collaboration_audit_query.py`

将只读查询逻辑放在独立模块，避免膨胀 `collaboration_service.py`（388 行写路径）或 `audit_service.py`（管理操作日志与清理逻辑）。

#### 2.2.1 权限与生命周期校验（作者侧）
```python
async def list_for_owner(
    db: AsyncSession, actor_id: int, scope: Scope, resource_id: int, filters: AuthorCollaborationAuditFilters,
) -> CollaborationAuditList:
    with db.no_autoflush:
        await authz.require_authenticated(db, actor_id)
        model = Project if scope == "project" else Series
        parent = await db.scalar(select(model).where(model.id == resource_id).execution_options(populate_existing=True))
    if parent is None or (parent.user_id if isinstance(parent, Project) else parent.owner_id) != actor_id:
        raise NotFoundError("作品或系列不存在")
    if isinstance(parent, Project):
        project_lifecycle.ensure_active(parent)
    ...
```
- 严格遵守 `db.no_autoflush` 规范（`.trellis/spec/backend/rbac-quotas.md:75`）。
- 仅 `owner` 可通过；协作者、非作者管理员、陌生人一律 `404`；回收站作品 `404`；删除中作品 `409 (project_deleting)`。

#### 2.2.2 作者侧行级安全与防枚举 SQL 语义（关键设计）

1. **合法参与方行过滤（排除陌生人越权探测）**：
   ```python
   _RESPONSE_ACTIONS = ("accept", "decline")
   owner_visible_rows = or_(
       CollaborationAuditEvent.action.in_(_RESPONSE_ACTIONS),
       CollaborationAuditEvent.actor_user_id == actor_id,
   )
   ```
   - 依据：`collaboration_service.respond` 仅在查到 `(invitation_id, actor.id)` 属于该资源后才将 `resource_id` 设为真实正整数，因此 `resource_id > 0` 的 `accept`/`decline` 必来自真实受邀人；而 `invite`/`modify`/`revoke` 只有 `actor_user_id == owner_id` 才属于作者本人行为，非作者越权尝试被完全排除。

2. **防枚举目标可见性谓词（同时作用于 SQL 筛选与序列化）**：
   - 哪些行在作者侧必须隐藏 `target_user_id` 和 `target_username`？
     - `action == 'invite'` 且 `result == 'failure'` 且 `error_code` 不是 `'self_invite'` 或 `'already_collaborator'`（包含 `error_code IS NULL` 的防御性兜底）。
   - 注意 SQL 三值逻辑（`NULL NOT IN (...)` 为 UNKNOWN）：
     ```python
     _SAFE_INVITE_FAILURE_CODES = ("self_invite", "already_collaborator")

     def _author_target_visible_expr():
         return or_(
             CollaborationAuditEvent.action != "invite",
             CollaborationAuditEvent.result != "failure",
             CollaborationAuditEvent.error_code.in_(_SAFE_INVITE_FAILURE_CODES),
         )

     def _is_author_target_visible(row: CollaborationAuditEvent) -> bool:
         return (
             row.action != "invite"
             or row.result != "failure"
             or row.error_code in _SAFE_INVITE_FAILURE_CODES
         )
     ```
   - 成员筛选条件：
     - `member_user_id`:
       `or_(CollaborationAuditEvent.actor_user_id == uid, and_(_author_target_visible_expr(), CollaborationAuditEvent.target_user_id == uid))`
     - `member_q`:
       `pattern = f"%{_escape_like(member_q)}%"`
       `or_(CollaborationAuditEvent.actor_username.ilike(pattern, escape="\\"), and_(_author_target_visible_expr(), CollaborationAuditEvent.target_username.ilike(pattern, escape="\\")))`
   - 响应投影：当 `not _is_author_target_visible(row)` 时，强制输出 `target_user_id=None, target_username=None`。
   - 结论：无论历史数据还是新写入数据，作者搜索禁用用户或查看 `invitee_unavailable` 行时，都与不存在用户完全一致。

#### 2.2.3 SQLite / PostgreSQL 时间范围与排序一致性（关键边界修复）

`CollaborationAuditEvent.occurred_at` 使用 `server_default=func.now()`：
- PostgreSQL 为原生 `TIMESTAMPTZ`，直接用 `occurred_at >= from_at`、`occurred_at < to_at` 和 `ORDER BY occurred_at DESC, id DESC`。
- SQLite 的 `CURRENT_TIMESTAMP` 存为 19 字符 `'YYYY-MM-DD HH:MM:SS'`，而 SQLAlchemy 绑定 UTC aware `datetime` 为 26 字符 `'YYYY-MM-DD HH:MM:SS.ffffff'`；字符串比较时 `'...00:00:00' < '...00:00:00.000000'` 会导致整秒边界出错。
- 处理方式（沿用 `ai_usage_storage.py:407-412` 思路，扩展到半开区间）：
  ```python
  def _time_range_conditions(db: AsyncSession, from_at: datetime, to_at: datetime):
      col = CollaborationAuditEvent.occurred_at
      if db.get_bind().dialect.name == "sqlite":
          from_cond = (
              or_(col >= from_at, cast(col, String) == from_at.strftime("%Y-%m-%d %H:%M:%S"))
              if from_at.microsecond == 0 else col >= from_at
          )
          to_cond = (
              and_(col < to_at, cast(col, String) != to_at.strftime("%Y-%m-%d %H:%M:%S"))
              if to_at.microsecond == 0 else col < to_at
          )
          return [from_cond, to_cond]
      return [col >= from_at, col < to_at]
  ```
- 排序：
  - PostgreSQL：`ORDER BY occurred_at DESC, id DESC`
  - SQLite：`ORDER BY func.strftime('%Y-%m-%d %H:%M:%f', occurred_at) DESC, id DESC`（同时兼容 `server_default` 19 字符行与测试/显式插入的 26 字符微秒行，避免同秒不同格式乱序）。
- `count` 与列表分页共用完全相同的 `conditions` 列表。

#### 2.2.4 路由与响应头

1. `backend/app/api/v1/collaboration.py` 新增：
   - `GET /projects/{project_id}/collaborators/audit-logs`（`response_model=CollaborationAuditList`，`response.headers["Cache-Control"] = "no-store"`）
   - `GET /series/{sid}/collaborators/audit-logs`（`response_model=CollaborationAuditList`，`response.headers["Cache-Control"] = "no-store"`）
2. `backend/app/api/v1/admin_audit.py` 新增（**必须注册在 `GET /{log_id}` 之前！**）：
   - `GET /admin/audit-logs/collaboration`（`admin: PermissionAuditRead`，`response_model=CollaborationAuditList`，`require_permission("audit.read")` 自动设置 `Cache-Control: no-store`）

#### 2.2.5 错误矩阵

| 场景 | 状态码 |
| --- | --- |
| 未登录、禁用账号、失效 token | `401` |
| 非作者（含协作者、无权/有权管理员）调用作者审计接口 | `404` |
| 回收站作品调用作者审计接口 | `404` |
| 删除中作品调用作者审计接口 | `409`（`code = project_deleting`） |
| 普通用户或无 `audit.read` 管理员调用 `/admin/audit-logs/collaboration` | `403`（`code = permission_denied`, `required_permission = "audit.read"`） |
| 非正 ID、超 `2_147_483_647` 整数、`resource_id < 0`、非法枚举、不成对/无时区/起止颠倒日期、非法分页 | `422` |

---

## 3. 前端设计

### 3.1 API 与纯工具层

1. `frontend/src/api/collaborationAudit.ts`（或在 `collaboration.ts` 旁独立模块，保持单一职责与标签唯一来源）：
   - 类型镜像后端 `CollaborationAuditAction`、`CollaborationAuditResult`、`CollaborationAuditErrorCode`、`CollaborationAuditSummary`、`CollaborationAuditEvent`、`AuthorCollaborationAuditQuery`、`AdminCollaborationAuditQuery`、`CollaborationAuditPage`。
   - 请求方法（通过 `request` / `api.get` 走 `/api/v1` 代理）：
     - `collaborationAuditApi.listForOwner(scope, resourceId, query, signal?)` → `GET /{projects|series}/{resourceId}/collaborators/audit-logs`
     - `collaborationAuditApi.listForAdmin(query, signal?)` → `GET /admin/audit-logs/collaboration`
   - 唯一标签常量：
     - `COLLAB_AUDIT_SCOPE`、`COLLAB_AUDIT_ACTION`、`COLLAB_AUDIT_RESULT`、`COLLAB_AUDIT_ERROR`、`COLLAB_AUDIT_ROLE`、`COLLAB_AUDIT_STATUS`
2. `frontend/src/utils/collaborationAudit.ts`（纯函数，可直接做 Node 单测）：
   - 复用 `auditDateRange` 与 `auditValidPage`（来自 `utils/auditLogs.ts`）构建作者与管理员查询参数：`authorCollaborationAuditQuery`、`adminCollaborationAuditQuery`。
   - 快照标签：`collaborationUserLabel(userId, username, fallback)`（避免出现 `#null`）、`collaborationResourceLabel(event)`（`resource_id === 0` 显示「未知资源（无效/非本人邀请） · #0」）。
   - 摘要格式化：`collaborationAuditSummaryRows(event)`，严格按白名单 6 键渲染角色、状态、作者模型授权的前后变化（`scope === 'series'` 不显示作者模型授权；失败记录标注「操作前」，绝不伪造未提交的 `after_*`）。

### 3.2 组件与页面集成

1. **作者侧共享面板：`frontend/src/components/collaboration/CollaborationAuditPanel.tsx`**
   - Props：`{ scope: CollaborationScope; resourceId: number; canManage: boolean }`
   - 筛选区：成员用户名快照（`member_q`）、动作（`action`）、结果（`result`）、开始日期/结束日期（本地日期 `type="date"`），含「查询」「重置」「刷新」按钮。
   - 结果展示：
     - 采用清晰紧凑的响应式记录卡片/列表（在 760px 作品抽屉、系列卡片与 375px 窄屏上体验一致且零横向滚动），每条展示时间、动作标签、结果标签、操作人、目标成员（脱敏时显示「未公开 / 不可用」）、变更摘要与失败说明；提供只读详情展开/抽屉（展示 `operation_id`、`invitation_id`、完整前后摘要与错误码）。
     - 显式声明隐私与范围说明：「展示本作品/系列的协作变更及本人/受邀人失败记录；不包含其他用户的越权尝试，邀请不可用账号时不区分账号不存在或已禁用。」
   - 并发/失效防护：绑定 `actorId`、`scope`、`resourceId`、请求序列 `AbortController` 与 `currentRequestGeneration()`，切换账号/关闭抽屉/变更筛选时立即中止并丢弃旧响应。
2. **`frontend/src/components/collaboration/MemberPanel.tsx` 与 `ProjectLayout.tsx`**
   - `MemberPanel` 外层接入 Ant Design `Tabs`（`成员管理` | `协作审计`，`destroyOnHidden`），默认 `members`；现有 `MemberRows`、`MemberChangeNotice`、`MEMBER_AI_ABORT_NOTICE` 导出与既有行为不变，确保 `SeriesPage.test.ts` 与 `collaborationWorkspace.test.ts` 零破坏。
   - 切换到「协作审计」时挂载 `CollaborationAuditPanel` 并拉取最新数据；作者在「成员管理」完成邀请/改权/撤销后切换标签即可立刻看到新记录。
   - `ProjectLayout.tsx:63` 将「作品协作者」抽屉桌面宽度由 `520` 调整为 `760`（移动端仍为 `'100%'`），`destroyOnHidden` 保持不变。
3. **管理员侧：`frontend/src/pages/AdminAuditLogsPage.tsx` + `AdminCollaborationAuditTab.tsx` + `CollaborationAuditDetailDrawer.tsx`**
   - 在 `AdminAuditLogsPage` 顶部增加 `Tabs`（`管理操作日志` | `协作审计`），默认 `admin`（如 URL 查询参数 `?tab=collaboration` 则初始进入协作审计，无 Router 上下文的 SSR 单元测试同样安全）。
   - 「按日期清理」按钮仅在「管理操作日志」标签页显示；`AuditCleanupModal` 文案补充一句「清理仅针对管理操作日志，不影响协作审计」。
   - `AdminCollaborationAuditTab`（`destroyOnHidden`）：
     - 筛选区：作用范围（作品/系列）、资源 ID（支持 `0`）、资源名称快照、操作人用户名/ID、目标成员用户名/ID、动作、结果、起止日期（本地含结束日）。
     - 桌面端使用 `Table`（`scroll={{ x: ... }}`）+ 只读「详情」按钮；移动端（`useIsMobile()`）使用可换行 `List` 卡片，无 `<table` 与大 `min-width`。
     - 只读详情抽屉 `CollaborationAuditDetailDrawer` 展示全部安全快照字段、`operation_id`、`invitation_id`、安全错误码及白名单摘要，无任何写操作或清理入口。
     - 通过 `useAuthorizationCleanup` 与 `key={user.id}` 在撤权或切换账号时清空状态并关闭详情抽屉。

---

## 4. 风险、权衡与回滚

- **权衡 1（查询层脱敏 vs 修改写入层）**：选择在作者查询层（SQL + 序列化）做防枚举脱敏，而不修改写入层。优点是不改动已验证的协作写事务/并发测试，同时直接覆盖库中已存在的历史 `invitee_unavailable` 记录，并让管理员在全站视图能排查被禁用账号的邀请失败；风险是将来若新增第二个作者查询入口必须复用 `collaboration_audit_query`（将在 spec 中明确约束）。
- **权衡 2（列表直接返回完整 `CollaborationAuditEventOut`）**：协作审计摘要最多 6 个标量键且无业务版本关联，无需单独 `GET /{id}` 二次查询，减少一次往返并避免多余端点。
- **回滚方案**：无数据库迁移、无权限目录变更，若需回滚只需 revert 本次提交代码即可安全恢复到原有状态，不影响任何已落库的 `collaboration_audit_events` 与 `admin_audit_logs`。
