# 协作审计查询界面

## Goal

为作品与系列作者提供可筛选的协作审计查询界面，并在管理后台「操作日志」中明确建立管理员查看全站协作审计的只读边界与权限规则，使已持久化的协作审计快照（`TODOS.md` 第 21 项）可查、可核对且不破坏既有隐私与权限隔离。

## Confirmed Facts

- **数据层已独立持久化**：`collaboration_audit_events` 表（`backend/app/models/collaboration.py:72-98`，迁移 `0044_novel_collaborators.py:73-101`）无业务外键，删除用户、作品或系列后快照仍保留；包含 `id`、`operation_id`、`occurred_at`（UTC，`server_default=func.now()`）、`scope`（`project`/`series`）、`resource_id`（`>= 0`，`0` 仅用于无效或非本人邀请的失败响应）、`resource_label`、`invitation_id`、`actor_user_id`/`actor_username`、`target_user_id`/`target_username`、`action`（`invite`/`accept`/`decline`/`modify`/`revoke`）、`result`（`success`/`failure`）、白名单 `error_code`（12 项）及白名单 `summary`（6 项 `before_*`/`after_*` 标量键），不含正文、提示词或密钥。
- **索引与备份边界**：现有索引 `ix_collaboration_audit_scope_resource (scope, resource_id, occurred_at, id)` 与 `ix_collaboration_audit_actor_time (actor_user_id, occurred_at, id)` 已覆盖按资源与按操作人时间倒序查询；单书 JSON 备份不携带协作审计，仅整库备份包含。
- **既有写入与隐私事实**：
  - `backend/app/services/collaboration_service.py:242-254` 在邀请校验 `is_active` 和 `_lock_parent(owner_only=True)` 之前即按用户名查库并写入 `state.target_user_id`/`target_username`，因此 `invitee_unavailable`（已禁用 vs 不存在）及其他未通过目标可用性校验的邀请失败行在库中可能存有目标身份，而 `11-09-novel-collaborators/prd.md` R1 规定作者不得借此区分已禁用与不存在账号。
  - 非作者越权调用邀请/修改/撤销接口会在目标 `resource_id` 下记录失败事件（`actor_user_id != owner_id`）；陌生人尝试接受/拒绝不存在或他人邀请时 `resource_id = 0`（`backend/app/services/collaboration_service.py:358-362`）。
- **现有界面与权限体系**：
  - 作者侧成员管理共用 `frontend/src/components/collaboration/MemberPanel.tsx`，作品端挂在 `frontend/src/pages/project/ProjectLayout.tsx:63-65` 的「作品协作者」抽屉，系列端挂在 `frontend/src/pages/SeriesPage.tsx:107-109` 的底部卡片。
  - 管理员操作日志位于 `/admin/audit-logs`（`frontend/src/pages/AdminAuditLogsPage.tsx`、`backend/app/api/v1/admin_audit.py`），读取权限 `audit.read`、按日期清理权限 `audit.cleanup`（`backend/app/core/permissions.py:33-34`），底层读写 `admin_audit_logs` 表。

## Requirements

### R1 作者侧作品与系列协作审计查询
- 同步提供作品（`scope = 'project'`）与系列（`scope = 'series'`）两条作者只读查询入口，仅对应作品作者或系列作者可访问。
- 协作者（查看者/编辑者/系列设定协作者）、无关用户及仅具管理员权限的非作者账号调用作者查询接口均返回 `404`（不扩大管理员人工内容旁路，不暴露作品/系列是否存在）。
- 作品处于回收站时返回 `404`，处于删除中时返回 `409`（`code = project_deleting`），与现有 `list_members` 生命周期边界一致。
- 支持按**成员**（用户名快照字面子串匹配，或成员用户 ID）、**动作**（`invite`/`accept`/`decline`/`modify`/`revoke`）、**结果**（`success`/`failure`）及**时间范围**（成对带时区 UTC 半开区间 `[from_at, to_at)`）组合筛选，支持分页，按 `occurred_at DESC, id DESC` 稳定排序。
- 列表展示操作时间、操作人快照、动作、目标成员快照、结果、错误类别（失败时）及角色/状态/模型授权变更摘要；单条记录支持展开或只读详情查看（含 `operation_id`、`invitation_id` 等安全元数据）。

### R2 作者侧失败记录可见性与防枚举隔离
- 作者侧展示本作品/系列的全部**成功记录**以及**合法参与方的失败记录**（作者本人失败的邀请/修改/撤销，以及受邀人失败的接受/拒绝）。
- **排除非作者越权探测**：作者侧查询在数据库条件层即排除 `action IN ('invite', 'modify', 'revoke')` 且 `actor_user_id != owner_id` 的失败记录，既不返回也不计入 `total`，避免向作者泄露陌生人用户名或探测行为。
- **防账号枚举脱敏**：对于 `action = 'invite'` 且 `result = 'failure'` 且 `error_code NOT IN ('self_invite', 'already_collaborator')` 的记录（包含 `invitee_unavailable`、作品回收期 `not_found`、系统/并发/取消失败等），无论数据库历史行是否存有目标用户字段，作者侧在 **SQL 成员筛选条件** 与 **响应序列化** 中一律将 `target_user_id` 和 `target_username` 视作 `null`，确保「账号不存在」与「账号已禁用」在列表、计数与搜索下均完全不可区分。

### R3 管理员全站协作审计与清理边界
- 管理员在现有「操作日志」页（`/admin/audit-logs`）新增只读的「协作审计」标签页，与原有「管理操作日志」并列。
- 权限**复用现有 `audit.read`**，不新增 RBAC 权限代码，不改变现有内置/自定义角色的权限集合；无 `audit.read` 权限的管理员与普通用户返回 `403`，未登录或会话失效返回 `401`。
- 全站协作审计查询覆盖 `collaboration_audit_events` 全部记录（含 `project`/`series`、已删除或回收站作品/系列快照、非作者越权尝试，以及 `resource_id = 0` 的无效邀请失败记录），支持按作用范围（`scope`）、资源 ID（`>= 0`）、资源名称快照、操作人（ID/用户名快照）、目标成员（ID/用户名快照）、动作、结果及时间范围组合筛选与分页，提供只读详情抽屉。
- **清理隔离**：协作审计独立只追加长期保留，**不受现有 `audit.cleanup`（按日期清理管理操作日志）影响**；后台「协作审计」标签页不提供清理按钮，且管理操作日志清理弹窗与说明文案须明确注明「仅清理管理操作日志，不影响协作审计」。

### R4 前端入口、交互安全与响应式适配
- **作者侧入口**：在现有 `MemberPanel` 内增加标签页切换（「成员管理」｜「协作审计」），默认停留在「成员管理」且仅在切到「协作审计」时按需拉取审计数据；作品端「作品协作者」抽屉桌面宽度适当加宽（约 760px）以舒适承载筛选与记录，系列端复用同一标签页卡片。
- **时间与边界一致**：前端本地日期筛选包含结束日全天，按本地时区换算为 UTC 半开区间 `[from_at, to_at)` 发送给后端；展示时间按浏览器本地时区格式化。
- **会话与权限失效保护**：切换账号、登出、切换作品/系列、关闭抽屉或撤销 `audit.read` 权限后，旧的异步响应不得回写状态；加载中、空态、筛选校验错误和网络失败均有明确反馈与重试入口。
- **移动端适配**：375px 窄屏下作者侧与管理员侧均使用可换行卡片/列表布局，不得出现撑宽整页的固定宽表格或超宽 `min-width`，长用户名、长标题与 UUID 均正常换行。

### R5 文档与规范同步
- 同步更新根目录 `README.md`（更新「协作审计」章节说明当前已提供作者侧与管理员全站查询入口及边界，不再写“当前未提供查询界面”）。
- 同步更新 `TODOS.md` 第 21 项状态与验收记录，并将协作审计查询契约沉淀到 `.trellis/spec/`。

## Out of Scope

- 不新增针对 `collaboration_audit_events` 的删除、清理或自动过期机制。
- 不修改现有协作邀请/修改/撤销/响应的写入语义与并发屏障，不向 `admin_audit_logs` 复制写入协作事件。
- 不新增 RBAC 权限项或数据库结构迁移。
- 不向协作者（查看者/编辑者/系列设定协作者）开放协作审计查询，也不提供协作审计导出（CSV/JSON）或单书备份携带。

## Acceptance Criteria

- [x] **AC1（R1 作者查询与隔离）**：作品作者与系列作者可分别通过作者接口及「成员管理｜协作审计」标签页查询本资源的协作审计，支持按成员、动作、结果和时间范围组合筛选与分页；查看者、编辑者、系列设定协作者、陌生用户及非作者管理员访问作者接口返回 `404`；回收站作品返回 `404`、删除中作品返回 `409`。
- [x] **AC2（R2 越权过滤与防枚举）**：作者查询列表、分页总数与成员筛选均排除非作者越权 `invite`/`modify`/`revoke` 失败记录；作者分别邀请「已禁用账号」与「不存在账号」产生的 `invitee_unavailable` 失败记录，在作者侧响应字段与按被邀用户名/ID 筛选时表现完全一致（不暴露 `target_user_id`/`target_username`）。
- [x] **AC3（R3 管理员全站查询与清理隔离）**：具备 `audit.read` 的管理员可在 `/admin/audit-logs`「协作审计」标签页按作用范围、资源 ID（含 `0`）、资源名、操作人、目标、动作、结果和时间范围查询全站协作审计（含已彻底删除作品/系列快照与越权尝试）并查看只读详情；无 `audit.read` 返回 `403`；执行 `audit.cleanup` 清理管理操作日志后 `collaboration_audit_events` 条数和内容不变，且清理界面明确提示不影响协作审计。
- [x] **AC4（R1/R3 时间边界与字面搜索）**：SQLite 与 PostgreSQL 下成对 UTC 半开区间 `[from_at, to_at)` 在整秒边界（含 `server_default` 秒精度时间）准确包含起点、排除终点；不成对/无时区/起止颠倒及超范围整数参数返回 `422`；用户名/资源名搜索将 `%`、`_`、`\` 转义为字面量。
- [x] **AC5（R4 响应式、状态与会话失效）**：桌面与 375px 移动端下作者抽屉、系列卡片与管理员「协作审计」标签页布局完整无横向溢出；切换成员管理后再切回协作审计可看到最新操作记录；账号切换/撤权/关闭抽屉时旧请求响应不污染界面；错误态可重试。
- [x] **AC6（R5 质量门禁与真实浏览器验收）**：后端 `ruff` 与测试（含新增协作审计查询及权限/防枚举/SQLite 边界测试；若本地 PostgreSQL 可用则补跑 PG 专项）、前端 `lint`/`build`/`vitest` 全部通过；使用隔离环境与真实 Chrome 验证作者作品/系列审计、管理员全站协作审计、清理隔离及 375px 移动布局，并更新 `README.md`、`TODOS.md` 与 `.trellis/spec/`。
