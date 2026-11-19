# 协作审计查询（跨层契约）

## 1. 范围 / 触发条件

适用于 `collaboration_audit_events` 的只读查询接口、防越权与防账号枚举隔离，以及前端作者侧与管理员全站协作审计界面。来源任务 `11-19-collaboration-audit-query-ui`；底层事件表由迁移 `0044` 建立，本契约不改变写入事务路径与表结构，也不新增 RBAC 权限项。

修改以下任意位置时必须同步检查本契约与测试：
- `backend/app/schemas/collaboration_audit.py`
- `backend/app/services/collaboration_audit.py`（与 schema 共享动作/错误码/摘要白名单）
- `backend/app/services/collaboration_audit_query.py`
- `backend/app/api/v1/collaboration.py`、`backend/app/api/v1/admin_audit.py`
- `frontend/src/api/collaborationAudit.ts`、`frontend/src/utils/collaborationAudit.ts`
- `frontend/src/components/collaboration/{MemberPanel,CollaborationAuditPanel}.tsx`
- `frontend/src/components/auditLogs/{AdminCollaborationAuditTab,CollaborationAuditDetailDrawer,AuditCleanupModal}.tsx`、`frontend/src/pages/AdminAuditLogsPage.tsx`

## 2. 签名

所有查询响应必须携带 `Cache-Control: no-store`，前缀 `/api/v1`：

| 方法 / 路径 | 权限 / 生命周期 | 请求 / 响应 |
| --- | --- | --- |
| `GET /projects/{project_id}/collaborators/audit-logs` | 仅作品作者；回收站 404，删除中 409 | `AuthorCollaborationAuditFilters` → `CollaborationAuditList` |
| `GET /series/{sid}/collaborators/audit-logs` | 仅系列作者 | `AuthorCollaborationAuditFilters` → `CollaborationAuditList` |
| `GET /admin/audit-logs/collaboration` | `audit.read`（必须注册在 `GET /{log_id}` 之前） | `AdminCollaborationAuditFilters` → `CollaborationAuditList` |

```python
# backend/app/services/collaboration_audit_query.py
async def list_for_owner(
    db: AsyncSession, actor_id: int, scope: Scope, resource_id: int, filters: AuthorCollaborationAuditFilters,
) -> CollaborationAuditList

async def list_for_admin(
    db: AsyncSession, filters: AdminCollaborationAuditFilters,
) -> CollaborationAuditList
```

`AuthorCollaborationAuditFilters`（`extra="forbid"`）：`member_user_id?`（`1..2_147_483_647`）、`member_q?`（≤64）、`action?`、`result?`、`from_at?`/`to_at?`（成对带时区 UTC 半开区间）、`page=1`（`1..10_000_000`）、`page_size=20`（`1..100`）。

`AdminCollaborationAuditFilters`（`extra="forbid"`）：`scope?`（`project`/`series`）、`resource_id?`（`0..2_147_483_647`，允许 `0`）、`resource_q?`（≤200）、`actor_user_id?`、`actor_q?`（≤64）、`target_user_id?`、`target_q?`（≤64）、`action?`、`result?`、`from_at?`/`to_at?`、`page`、`page_size`。

`CollaborationAuditEventOut`：`id`、`operation_id`、`occurred_at`（UTC aware）、`scope`、`resource_id`、`resource_label`、`invitation_id`、`actor_user_id`、`actor_username`、`target_user_id`、`target_username`、`action`、`result`、`error_code`、`summary`（严格 6 键 `before_*`/`after_*` 白名单）。

## 3. 契约

### 作者侧安全隔离与防枚举
- 授权与父资源读取在 `db.no_autoflush` 内执行；非作者（含查看者、编辑者、系列设定协作者、陌生用户及具备 `audit.read`/`projects.read` 或超管身份的非作者管理员）一律返回 `404`，不扩大管理员人工旁路。
- **行级排除非作者越权尝试**：作者查询条件必须限定 `scope`、`resource_id` 且 `(action IN ('accept', 'decline') OR actor_user_id = owner_id)`。陌生人或协作者越权调用 `invite`/`modify`/`revoke` 产生的失败行不得返回，也不计入 `total`。
- **未确认目标的失败邀请双层脱敏**：`action = 'invite'` 且 `result = 'failure'` 且 `error_code NOT IN ('self_invite', 'already_collaborator')`（含 `invitee_unavailable`、回收期 `not_found`、系统/并发/取消失败）在作者侧 **SQL 成员筛选**（`member_user_id` / `member_q`）与 **响应序列化** 中均必须将 `target_user_id`/`target_username` 视作 `null`。必须处理 SQL 三值逻辑（`error_code IS NULL` 不可误放行）。由此保证「账号不存在」与「账号已禁用」在列表、总数和搜索下完全不可区分。
- 若未来新增其他作者侧协作审计查询入口，必须复用 `collaboration_audit_query.list_for_owner` 的行级过滤与脱敏谓词，不得直接 `SELECT * FROM collaboration_audit_events`。

### 管理员全站查询与清理隔离
- 复用现有 `audit.read` 权限，不新增 RBAC 权限代码。
- 管理员视角保留完整快照，不做作者侧越权过滤或目标脱敏，可查已彻底删除作品/系列快照、已删用户快照、非作者越权尝试及 `resource_id = 0` 的无效邀请失败记录。
- 协作审计独立只追加保留，**绝不纳入 `audit.cleanup` 按日期清理范围**；后台「协作审计」标签页不提供清理按钮，`AuditCleanupModal` 明确注明「仅清理管理操作日志，不影响协作审计」。

### 时间区间、字面转义与分页
- `count` 与列表分页共用完全相同的 `conditions`。
- 所有文本子串搜索（`member_q`/`resource_q`/`actor_q`/`target_q`）trim 后空串视为未筛选，并转义 `%`、`_`、`\` 为字面量（`escape="\\"`）。
- `CollaborationAuditEvent.occurred_at` 为 `server_default=func.now()`：PostgreSQL 使用原生 `TIMESTAMPTZ` 比较与 `ORDER BY occurred_at DESC, id DESC`；SQLite 中 `CURRENT_TIMESTAMP` 写入 19 字符无微秒文本，而 SQLAlchemy 绑定 UTC `datetime` 为 26 字符带 `.000000` 文本，整秒半开边界 `[from_at, to_at)` 必须在 SQLite 上分别兼容 `cast(occurred_at, String) == ...`（起点包含、终点排除），排序归一化到 26 字符微秒精度后按 `id DESC` 打破并列。

### 前端跨层契约
- `frontend/src/api/collaborationAudit.ts` 镜像后端 schema 与全部 12 项错误码标签；`utils/collaborationAudit.ts` 复用 `utils/auditLogs.ts` 的 `auditDateRange` 与 `auditValidPage`，摘要严格只读白名单 6 键（系列隐藏 `allow_owner_ai`，失败记录仅显示操作前状态，绝不伪造未提交 `after_*`；无用户 ID 时不输出 `#null`）。
- 作者端在 `MemberPanel` 内提供「成员管理｜协作审计」标签页，`CollaborationAuditPanel` 使用 `React.lazy` 按需加载（切到审计标签时拉取最新记录），作品抽屉桌面宽 `760px`，移动端 `100%`。
- 管理员端在 `AdminAuditLogsPage` 内提供「管理操作日志｜协作审计」标签页，兼容无 Router 上下文的 SSR 单元测试；桌面用表格、375px 移动端用可换行卡片，无固定大 `min-width`。
- 切换账号、撤权、关闭抽屉或变更筛选时通过 `AbortController`、请求序列与 `watchRequestGeneration` 丢弃旧响应。

## 4. 校验与错误矩阵

| 条件 | 结果 |
| --- | --- |
| 未登录、禁用账号、失效 token | `401` |
| 非作者（含协作者、陌生人、非作者管理员/超管）访问作者审计接口 | `404` |
| 作品在回收站 / 删除中访问作者审计接口 | `404` / `409`（`code = project_deleting`） |
| 普通用户或无 `audit.read` 管理员访问 `/admin/audit-logs/collaboration` | `403`（`code = permission_denied`, `required_permission = "audit.read"`） |
| 未知 query 参数、非法枚举、不成对/无时区/起止颠倒日期、超 `2_147_483_647` 整数、负 `resource_id`、`page < 1`、`page_size > 100` | `422` |

## 5. 正例 / 基准 / 反例

- 正例：作者邀请已禁用账号与不存在账号均产生 `invitee_unavailable`，作者侧两行均返回 `target_user_id=null, target_username=null`，按被邀用户名搜索均返回 `total=0`；管理员在 `/admin/audit-logs`「协作审计」可核查完整目标快照与陌生人越权尝试。
- 基准：管理员执行 `audit.cleanup` 清空日期范围内的 `admin_audit_logs` 后，`collaboration_audit_events` 条数与全部记录保持不变。
- 反例：仅在作者响应序列化阶段将 `invitee_unavailable` 的 `target_username` 置 `null`，却忘记在 SQL `WHERE` 中屏蔽——作者用 `member_q=目标用户名` 仍可借 `total=1` 枚举已禁用账号。

## 6. 必需测试

- 后端 `backend/tests/test_collaboration_audit_query.py`：
  - 作者作品/系列组合筛选、分页、摘要、`Cache-Control: no-store`、跨作用域隔离。
  - 401/404/409（协作者、陌生人、非作者管理员、回收站、删除中、停用账号）。
  - 陌生人越权尝试排除（含 `total` 与搜索）；已禁用 vs 不存在 `invitee_unavailable` 及回收期失败邀请的 SQL + 响应双脱敏；`self_invite`/`already_collaborator` 保留目标。
  - 管理员 `audit.read` 401/403/200、已彻删作品/已删系列/已删用户快照、`resource_id=0` 筛选、`/admin/audit-logs/{log_id}` 不被路由遮蔽、执行 `audit.cleanup` 后协作审计零丢失。
  - `%`/`_`/`\` 字面转义、SQLite 19 字符整秒 `[from_at, to_at)` 半开边界与排序、全部 422 边界参数。
- 前端：
  - `frontend/src/api/collaborationAudit.test.ts`：路径、参数、空白裁剪、`AbortSignal`、12 项错误码标签、401/403/404 行为。
  - `frontend/src/utils/collaborationAudit.test.ts`：本地日期转 UTC 半开区间、脱敏中性文案、资源 `#0`、作品/系列及失败摘要投影。
  - `frontend/src/components/collaboration/collaborationAuditUI.test.ts` & `frontend/src/components/auditLogs/auditLogsUI.test.ts`：标签页显隐、清理隔离提示、375px 无宽表无大 `min-width`、只读详情。

## 7. 错误写法 vs 正确写法

错误（仅在序列化脱敏，且把 `/collaboration` 放在 `/{log_id}` 之后）：

```python
# 1. SQL 仍用 target_username 匹配，导致作者可通过 member_q 枚举已禁用账号。
conditions.append(CollaborationAuditEvent.target_username.ilike(pattern, escape="\\"))
# 2. FastAPI 中 /collaboration 定义在 /{log_id: int} 之后会被解析成 422。
```

正确（SQL 与序列化共用同一可见性谓词，并处理 SQLite 整秒边界与路由顺序）：

```python
# admin_audit.py 中先于 GET /{log_id} 注册 GET /collaboration
# collaboration_audit_query.py 中 SQL 筛选与投影共享同一规则：
visible = or_(
    CollaborationAuditEvent.action != "invite",
    CollaborationAuditEvent.result != "failure",
    CollaborationAuditEvent.error_code.in_(("self_invite", "already_collaborator")),
)
conditions.append(
    or_(
        CollaborationAuditEvent.actor_username.ilike(pattern, escape="\\"),
        and_(visible, CollaborationAuditEvent.target_username.ilike(pattern, escape="\\")),
    )
)
```
