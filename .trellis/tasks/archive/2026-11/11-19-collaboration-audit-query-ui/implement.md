# 实施计划：协作审计查询界面

## 1. 实施检查清单（按顺序执行）

### 阶段 A：后端契约、查询服务与路由
- [x] A1. 新增 `backend/app/schemas/collaboration_audit.py`：定义 `CollaborationAuditSummary`、`CollaborationAuditEventOut`、`CollaborationAuditList`、`AuthorCollaborationAuditFilters`、`AdminCollaborationAuditFilters`（复用 `AuditDateRange` 成对 UTC 校验与 `le=2_147_483_647` 整数边界），并与 `services/collaboration_audit.py` 共享动作/错误码/摘要键常量。
- [x] A2. 新增 `backend/app/services/collaboration_audit_query.py`：
  - 实现 `list_for_owner`（`db.no_autoflush` + `require_authenticated` + 严格 owner 校验 + `project_lifecycle.ensure_active` + 行级排除非作者越权 `invite`/`modify`/`revoke` + 防枚举 `target` SQL/序列化双脱敏 + 字面 `%`/`_`/`\` 转义 + SQLite/PG 整秒边界半开时间筛选与稳定排序）。
  - 实现 `list_for_admin`（全站组合筛选，支持 `resource_id = 0` 与 `resource_q`/`actor_q`/`target_q` 字面转义、同条件 count 与分页）。
- [x] A3. 接入路由：
  - `backend/app/api/v1/collaboration.py` 新增 `GET /projects/{project_id}/collaborators/audit-logs` 与 `GET /series/{sid}/collaborators/audit-logs`（设置 `Cache-Control: no-store`）。
  - `backend/app/api/v1/admin_audit.py` 在 `GET /{log_id}` **之前**新增 `GET /admin/audit-logs/collaboration`（依赖 `PermissionAuditRead`）。
- [x] A4. 新增后端测试 `backend/tests/test_collaboration_audit_query.py`（及 PG 专项 `test_collaboration_audit_query_postgresql.py`）：
  - 覆盖作者作品/系列查询、组合筛选（成员/动作/结果/时间）、字面 `%`/`_`/`\` 转义、分页与稳定排序。
  - 覆盖权限与生命周期矩阵：401、查看者/编辑者/系列协作者/陌生人/非作者管理员（含 `audit.read` 和超管）访问作者接口 404；回收站 404；删除中 409。
  - 覆盖安全边界：非作者越权 `invite`/`modify`/`revoke` 失败在作者侧完全不可见且不计入 `total`；`invitee_unavailable`（已禁用 vs 不存在）与回收期失败邀请在作者侧响应与按用户名/ID 筛选均完全一致不可区分。
  - 覆盖管理员 `/admin/audit-logs/collaboration`：`audit.read` 放行、无 `audit.read` 403、401、`Cache-Control: no-store`、可见已彻底删除作品/系列快照、越权失败、`resource_id = 0`；执行 `audit.cleanup` 清理管理日志后 `collaboration_audit_events` 条数与内容完全不变。
  - 覆盖 422 参数矩阵（不成对日期、无时区、起止颠倒、超 `2_147_483_647` 整数、负 `resource_id`、非法枚举）及 SQLite `server_default` 整秒边界 `[from_at, to_at)` 精确测试。

### 阶段 B：前端 API、工具、组件与页面集成
- [x] B1. 新增 `frontend/src/api/collaborationAudit.ts` 与 `frontend/src/utils/collaborationAudit.ts`：定义 TS 类型镜像、API 客户端、唯一标签映射、查询构建（复用 `auditDateRange`/`auditValidPage`）和白名单摘要格式化器。
- [x] B2. 新增 `frontend/src/components/collaboration/CollaborationAuditPanel.tsx`（作者侧作品/系列共用查询面板，含筛选表单、脱敏提示、记录卡片列表、只读详情展开/抽屉、分页、空/错态重试及会话/代次失效保护）并补充样式到 `collaboration.css`。
- [x] B3. 更新 `frontend/src/components/collaboration/MemberPanel.tsx`（增加 `成员管理` | `协作审计` 标签页，按需挂载审计面板，保持既有导出与测试兼容）与 `frontend/src/pages/project/ProjectLayout.tsx`（作品协作者抽屉桌面端宽度调整为 `760`）。
- [x] B4. 新增 `frontend/src/components/auditLogs/AdminCollaborationAuditTab.tsx` 与 `CollaborationAuditDetailDrawer.tsx`，并更新 `frontend/src/pages/AdminAuditLogsPage.tsx`（新增 `管理操作日志` | `协作审计` 标签页，清理按钮仅归属管理操作日志页签，保持无 Router SSR 渲染兼容）及 `frontend/src/components/auditLogs/AuditCleanupModal.tsx`（明确提示不影响协作审计）。
- [x] B5. 新增前端单元测试：
  - `frontend/src/api/collaborationAudit.test.ts`（路径、参数、空白裁剪、日期转换、请求取消）
  - `frontend/src/utils/collaborationAudit.test.ts`（日期区间、摘要投影、脱敏/未知资源 `#0` 展示、防 `#null`、失败不显示未提交 `after_*`）
  - `frontend/src/components/collaboration/collaborationAuditUI.test.ts`（作者标签页、筛选控件、空/错态重试、375px 适配、脱敏提示）
  - `frontend/src/components/auditLogs/auditLogsUI.test.ts` 补充管理员「协作审计」标签页、清理说明、只读详情及 375px 移动端卡片断言。

### 阶段 C：验收、文档与规范同步
- [x] C1. 运行后端与前端自动化门禁（前端 262 文件/1821 用例、PG16 专项、后端 SQLite 全量 6955 passed/10 skipped/19 xfailed 全部通过）。
- [x] C2. 使用隔离后端 + 本地前端 + 真实 Chrome（桌面与 375px 移动视口）端到端验收（8/8 场景通过，见 `research/verification.md`）。
- [x] C3. 同步更新 `README.md`、`TODOS.md`（第 21 项）以及 `.trellis/spec/backend/collaboration-audit-query.md`、`admin-audit.md` 与两端 spec 索引。

## 2. 验证命令

```bash
# 后端受影响测试（开发迭代）
cd backend && uv run ruff check . && uv run pytest tests/test_collaboration_audit.py tests/test_collaboration_audit_query.py tests/test_collaboration_invites.py tests/test_admin_audit_logs.py tests/test_rbac_permissions.py

# 后端全量门禁（SQLite）
cd backend && uv run ruff check . && uv run pytest -n 4 --dist loadfile --durations=20

# 前端门禁
cd frontend && npm run lint && npm run build && npm test -- --run
```

## 3. 关键风险文件与回滚点

- `backend/app/api/v1/admin_audit.py`：`/collaboration` 路由必须置于 `/{log_id}` 之前，否则会被 FastAPI 尝试解析为整型 `log_id` 导致 422。
- `backend/app/services/collaboration_audit_query.py`：SQL 三值逻辑（`error_code IS NULL`）与 SQLite `server_default` 整秒比较边界是两处最容易出错的点，必须有针对性回归用例。
- `frontend/src/pages/AdminAuditLogsPage.tsx`：既有单元测试在无 `MemoryRouter` 环境中渲染该组件，不得无保护调用 `useSearchParams`。
- `frontend/src/components/collaboration/MemberPanel.tsx`：`SeriesPage.test.ts` 与 `collaborationWorkspace.test.ts` 均 mock 该默认导出，且 `collaborationUI.test.ts` 引用其命名导出，改动须保持接口兼容。
- 回滚点：本任务无 DB 迁移与权限目录变更，任意阶段均可通过 git revert 干净回退。

## 4. `task.py start` 前核查

- [x] `prd.md` 已完成收敛检查，无未决开放问题，无跨节重复。
- [x] `design.md` 与 `implement.md` 已落盘。
- [x] `implement.jsonl` 与 `check.jsonl` 已填充真实规范与调研条目。
- [ ] 等待用户审阅最终规划摘要并明确批准进入实现。
