# 用户详情提交计划

状态：最终完整质量门禁及规范同步已通过；用户明确确认“按计划提交和收尾”，两批工作提交已完成（84688d7、ee64804），独立归档113ade5与会话记录均已完成。

## 1. feat(admin): 新增用户详情与最近登录统计

- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/backend/alembic/versions/0028_user_last_login.py`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/backend/app/api/v1/admin.py`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/backend/app/models/entities.py`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/backend/app/schemas/admin.py`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/backend/app/services/admin_service.py`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/backend/app/services/auth_service.py`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/backend/tests/test_admin_user_details.py`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/backend/tests/test_migrations.py`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/frontend/src/api/admin.test.ts`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/frontend/src/api/admin.ts`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/frontend/src/api/types.ts`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/frontend/src/components/adminUsers/AdminUserActionButtons.tsx`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/frontend/src/components/adminUsers/AdminUserActionDialog.tsx`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/frontend/src/components/adminUsers/adminUserActionsController.test.ts`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/frontend/src/components/adminUsers/adminUserActionsController.ts`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/frontend/src/components/adminUsers/adminUserDetailsUI.test.ts`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/frontend/src/components/adminUsers/useAdminUserActions.ts`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/frontend/src/pages/AdminProjectsPage.tsx`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/frontend/src/pages/AdminRecycleBinPage.tsx`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/frontend/src/pages/AdminUserDetailsPage.tsx`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/frontend/src/pages/AdminUsersPage.tsx`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/frontend/src/router.tsx`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/frontend/src/utils/adminUserDetails.test.ts`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/frontend/src/utils/adminUserDetails.ts`

## 2. docs(admin): 固化用户详情契约与验收记录

- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/spec/backend/auth-admin.md`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/spec/backend/database-guidelines.md`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/spec/backend/index.md`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/spec/frontend/component-guidelines.md`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/spec/frontend/index.md`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/spec/frontend/type-safety.md`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/check.jsonl`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/commit-plan.md`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/design.md`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/implement.jsonl`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/implement.md`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/prd.md`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/research/auth-contracts.md`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/research/backend-checks.md`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/research/backend-database-excerpts.md`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/research/backend-error-excerpts.md`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/research/backend-implementation.md`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/research/backend-quality-excerpts.md`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/research/browser-runtime-final.json`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/research/browser-verification.md`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/research/commit-plan.json`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/research/current-contracts.md`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/research/environment-checks.md`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/research/frontend-checks.md`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/research/frontend-component-excerpts.md`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/research/frontend-implementation.md`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/research/frontend-quality-excerpts.md`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/research/frontend-type-excerpts.md`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/research/full-scope-check.md`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/research/implementation-baseline.json`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/task.json`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.trellis/tasks/10-30-admin-user-details/verification.md`

## 原有改动：全部排除并保持原样

- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.codex/config.toml`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.gitignore`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.agents/skills/source-command-trellis-continue`
- `/Users/wenyuan/IdeaProjects/my/ai-novel-hub/.agents/skills/source-command-trellis-finish-work`

不amend；原计划不push，用户随后明确追加“合并到main并推送”，收尾提交完成后按此授权执行普通非强制推送。工作提交后单独归档与记录会话，不与工作提交交错。
