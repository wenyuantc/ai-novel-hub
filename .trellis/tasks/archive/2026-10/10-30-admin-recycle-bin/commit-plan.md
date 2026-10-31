# 作品回收站提交与收尾计划

状态：用户已在提交计划后明确要求“合并到main 推送”。前两笔工作提交已完成，当前任务已移入归档；后续按归档提交 → journal 提交 → main 快进合并 → origin/main 正常推送执行，结果由验证文档、会话日志与最终 Git 状态核实。下列工作文件清单记录的是提交时路径，归档后路径参见第 3 项。

分支：`codex/admin-recycle-bin`。仅处理当前作品回收站任务；保留其他规划任务。

## 1. feat(admin): 新增作品回收站与安全恢复

共 102 个代码、迁移及测试文件：

- `backend/alembic/versions/0024_project_recycle_bin.py`
- `backend/app/api/v1/__init__.py`
- `backend/app/api/v1/admin_recycle_bin.py`
- `backend/app/api/v1/projects.py`
- `backend/app/main.py`
- `backend/app/models/audit.py`
- `backend/app/models/entities.py`
- `backend/app/schemas/audit.py`
- `backend/app/schemas/recycle_bin.py`
- `backend/app/services/admin_project_service.py`
- `backend/app/services/admin_service.py`
- `backend/app/services/agent/effects.py`
- `backend/app/services/agent/service.py`
- `backend/app/services/agent/tools/base.py`
- `backend/app/services/analysis_service.py`
- `backend/app/services/api_key_service.py`
- `backend/app/services/audit_service.py`
- `backend/app/services/audit_writer.py`
- `backend/app/services/batch_service.py`
- `backend/app/services/book_import/reverse.py`
- `backend/app/services/card_service.py`
- `backend/app/services/career_service.py`
- `backend/app/services/cast_service.py`
- `backend/app/services/chapter_service.py`
- `backend/app/services/chapter_version_service.py`
- `backend/app/services/cover_service.py`
- `backend/app/services/entity_ai_service.py`
- `backend/app/services/foreshadow_service.py`
- `backend/app/services/generation_log_service.py`
- `backend/app/services/generation_service.py`
- `backend/app/services/inspiration_service.py`
- `backend/app/services/kb_index_service.py`
- `backend/app/services/kb_service.py`
- `backend/app/services/memory_service.py`
- `backend/app/services/open_api/catalog.py`
- `backend/app/services/organization_service.py`
- `backend/app/services/outline_ai_service.py`
- `backend/app/services/outline_service.py`
- `backend/app/services/outline_snapshot_service.py`
- `backend/app/services/outline_transfer_service.py`
- `backend/app/services/plan_service.py`
- `backend/app/services/project_lifecycle.py`
- `backend/app/services/project_service.py`
- `backend/app/services/project_transfer/exporter.py`
- `backend/app/services/project_transfer/registry.py`
- `backend/app/services/reader_service.py`
- `backend/app/services/recycle_bin_service.py`
- `backend/app/services/relationship_service.py`
- `backend/app/services/rewrite_service.py`
- `backend/app/services/story_service.py`
- `backend/app/services/task_engine/engine.py`
- `backend/app/services/task_engine/handlers/batch.py`
- `backend/app/services/task_engine/handlers/chapter.py`
- `backend/app/services/task_engine/registry.py`
- `backend/app/services/task_service.py`
- `backend/app/services/wizard_service.py`
- `backend/app/services/writeback_service.py`
- `backend/tests/conftest.py`
- `backend/tests/helpers.py`
- `backend/tests/test_admin.py`
- `backend/tests/test_admin_audit_logs.py`
- `backend/tests/test_admin_projects.py`
- `backend/tests/test_agent_api.py`
- `backend/tests/test_api_keys.py`
- `backend/tests/test_careers.py`
- `backend/tests/test_project_cover.py`
- `backend/tests/test_project_lifecycle.py`
- `backend/tests/test_reader.py`
- `backend/tests/test_recycle_bin.py`
- `backend/tests/test_recycle_bin_migrations.py`
- `backend/tests/test_recycle_bin_tasks.py`
- `backend/tests/test_relationships.py`
- `backend/tests/test_tasks.py`
- `frontend/src/api/auditLogs.ts`
- `frontend/src/api/recycleBin.test.ts`
- `frontend/src/api/recycleBin.ts`
- `frontend/src/api/types.ts`
- `frontend/src/components/AppLayout.tsx`
- `frontend/src/components/apiKeys/ApiKeyFormModal.tsx`
- `frontend/src/components/auditLogs/AuditLogDetailDrawer.tsx`
- `frontend/src/components/auditLogs/auditLogsUI.test.ts`
- `frontend/src/components/project/DeleteProjectModal.tsx`
- `frontend/src/components/project/RecycleProjectModal.tsx`
- `frontend/src/components/project/recycleBinUI.test.ts`
- `frontend/src/pages/AdminAuditLogsPage.tsx`
- `frontend/src/pages/AdminProjectsPage.tsx`
- `frontend/src/pages/AdminRecycleBinPage.tsx`
- `frontend/src/pages/AdminUsersPage.tsx`
- `frontend/src/pages/ProjectListPage.tsx`
- `frontend/src/router.tsx`
- `frontend/src/stores/project.test.ts`
- `frontend/src/stores/project.ts`
- `frontend/src/stores/tasks.test.ts`
- `frontend/src/stores/tasks.ts`
- `frontend/src/utils/adminUsers.test.ts`
- `frontend/src/utils/adminUsers.ts`
- `frontend/src/utils/apiKeys.test.ts`
- `frontend/src/utils/apiKeys.ts`
- `frontend/src/utils/auditLogs.test.ts`
- `frontend/src/utils/auditLogs.ts`
- `frontend/src/utils/recycleBin.test.ts`
- `frontend/src/utils/recycleBin.ts`

## 2. docs(admin): 固化回收站契约与验收记录

共 29 个规范、任务及验证文档（包含本计划）：

- `.trellis/spec/backend/admin-audit.md`
- `.trellis/spec/backend/admin-projects.md`
- `.trellis/spec/backend/auth-admin.md`
- `.trellis/spec/backend/database-guidelines.md`
- `.trellis/spec/backend/index.md`
- `.trellis/spec/backend/open-api.md`
- `.trellis/spec/backend/project-transfer.md`
- `.trellis/spec/backend/recycle-bin.md`
- `.trellis/spec/frontend/component-guidelines.md`
- `.trellis/spec/frontend/index.md`
- `.trellis/spec/frontend/quality-guidelines.md`
- `.trellis/spec/frontend/state-management.md`
- `.trellis/tasks/10-30-admin-recycle-bin/check.jsonl`
- `.trellis/tasks/10-30-admin-recycle-bin/commit-plan.md`
- `.trellis/tasks/10-30-admin-recycle-bin/design.md`
- `.trellis/tasks/10-30-admin-recycle-bin/implement.jsonl`
- `.trellis/tasks/10-30-admin-recycle-bin/implement.md`
- `.trellis/tasks/10-30-admin-recycle-bin/prd.md`
- `.trellis/tasks/10-30-admin-recycle-bin/research/backend-report.md`
- `.trellis/tasks/10-30-admin-recycle-bin/research/current-contracts.md`
- `.trellis/tasks/10-30-admin-recycle-bin/research/environment-checks.md`
- `.trellis/tasks/10-30-admin-recycle-bin/research/final-check-report.md`
- `.trellis/tasks/10-30-admin-recycle-bin/research/final-scope.json`
- `.trellis/tasks/10-30-admin-recycle-bin/research/frontend-check-report.md`
- `.trellis/tasks/10-30-admin-recycle-bin/research/frontend-report.md`
- `.trellis/tasks/10-30-admin-recycle-bin/research/implementation-baseline.json`
- `.trellis/tasks/10-30-admin-recycle-bin/research/runtime-verification.md`
- `.trellis/tasks/10-30-admin-recycle-bin/task.json`
- `.trellis/tasks/10-30-admin-recycle-bin/verification.md`

## 3. chore(task): 归档作品回收站任务

仅在前两笔工作提交成功后执行：

- 将 `.trellis/tasks/10-30-admin-recycle-bin/` 归档到 `.trellis/tasks/archive/2026-10/10-30-admin-recycle-bin/`。
- 更新任务完成状态及收尾记录，修正归档目录内 JSONL 的自身引用。
- 不归档其他任务。

## 4. chore(workspace): 记录回收站开发会话

仅在归档提交成功后执行：

- 更新 `.trellis/workspace/aiNovel/journal-1.md` 与 `.trellis/workspace/aiNovel/index.md`（以及日志脚本实际更新的工作区索引）。
- 记录前两笔工作提交的实际哈希、验证结果、未验证项与后续操作。

## 排除并保留原有改动

- `.codex/config.toml`
- `.gitignore`
- `.agents/skills/source-command-trellis-continue/SKILL.md`
- `.agents/skills/source-command-trellis-finish-work/SKILL.md`

上述文件与实施前基线内容哈希一致，均不暂存。

## 验证与边界

- 后端最终完整测试：1143 passed；前端：67 个文件、520 tests passed。
- 最终测试后上述代码文件未再修改；lint、等价 TypeScript/Vite 构建、隔离 HTTP 和浏览器验收已完成。
- 真实 PostgreSQL 迁移、行锁与并发未实测，发布前补验。
- 标准前端 build 仍受既有 root-owned 生成目录权限阻止，等价构建已通过；本次不修复无关目录权限。
- `research/final-scope.json` 是生成本计划之前的预提交快照，不代表后续归档或提交状态。
- 严格按上述顺序提交，不 amend；工作与收尾提交完成后快进合并到 main，仅按用户授权正常推送 origin/main，不强制推送。
