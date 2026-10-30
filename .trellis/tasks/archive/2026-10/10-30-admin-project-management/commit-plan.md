# 管理员作品管理提交方案

状态：代码与验收完成；用户已回复“提交 归档”，以下三组工作提交已全部完成，按授权继续归档与会话记录。不推送、不合并主分支。

## 质量依据

- 后端：有效 UTF-8 locale 下完整 1069 测试通过，Ruff 通过。
- 前端：ESLint、TypeScript/Vite 构建和 61 文件 459 测试通过。
- 三身份、编辑/阅读/删除、375px 与明暗主题浏览器验收通过；详情见 final-check.md 和 browser-checks.md。
- 当前分支：codex/admin-project-management。

## 1. feat(admin): 支持管理员跨用户维护项目内容

范围：27 个文件。

- `backend/app/api/v1/admin.py`
- `backend/app/api/v1/chapters.py`
- `backend/app/api/v1/kb.py`
- `backend/app/api/v1/memories.py`
- `backend/app/api/v1/projects.py`
- `backend/app/api/v1/story.py`
- `backend/app/schemas/admin.py`
- `backend/app/schemas/project.py`
- `backend/app/services/admin_project_service.py`
- `backend/app/services/admin_service.py`
- `backend/app/services/career_service.py`
- `backend/app/services/cast_service.py`
- `backend/app/services/chapter_service.py`
- `backend/app/services/chapter_version_service.py`
- `backend/app/services/cover_service.py`
- `backend/app/services/foreshadow_service.py`
- `backend/app/services/kb_service.py`
- `backend/app/services/memory_service.py`
- `backend/app/services/organization_service.py`
- `backend/app/services/outline_service.py`
- `backend/app/services/outline_snapshot_service.py`
- `backend/app/services/plan_service.py`
- `backend/app/services/project_service.py`
- `backend/app/services/reader_service.py`
- `backend/app/services/relationship_service.py`
- `backend/app/services/story_service.py`
- `backend/tests/test_admin_projects.py`

## 2. feat(frontend): 增加管理员作品管理与人工编辑入口

范围：35 个文件。

- `frontend/src/api/admin.test.ts`
- `frontend/src/api/admin.ts`
- `frontend/src/api/types.ts`
- `frontend/src/components/AppLayout.tsx`
- `frontend/src/components/StudioNavigation.tsx`
- `frontend/src/components/chapter/ChapterStyleSetting.tsx`
- `frontend/src/components/chapter/VersionDrawer.tsx`
- `frontend/src/components/entities/CharacterRelationsTab.tsx`
- `frontend/src/components/project/CoverPanel.tsx`
- `frontend/src/components/project/DeleteProjectModal.tsx`
- `frontend/src/components/project/ProjectManagementNotice.tsx`
- `frontend/src/components/project/adminProjectsUI.test.ts`
- `frontend/src/hooks/useChapterAnalysis.ts`
- `frontend/src/hooks/useIndexStatus.ts`
- `frontend/src/hooks/useProjectAccess.ts`
- `frontend/src/pages/AdminProjectsPage.tsx`
- `frontend/src/pages/AdminUsersPage.tsx`
- `frontend/src/pages/ReaderPage.tsx`
- `frontend/src/pages/project/CareersPanel.tsx`
- `frontend/src/pages/project/ChapterEditorPage.tsx`
- `frontend/src/pages/project/ChaptersPanel.tsx`
- `frontend/src/pages/project/CharactersPanel.tsx`
- `frontend/src/pages/project/KnowledgePanel.tsx`
- `frontend/src/pages/project/MemoriesPanel.tsx`
- `frontend/src/pages/project/OrganizationsPanel.tsx`
- `frontend/src/pages/project/OutlinesPanel.tsx`
- `frontend/src/pages/project/OverviewPanel.tsx`
- `frontend/src/pages/project/ProjectLayout.tsx`
- `frontend/src/pages/project/RelationshipsPanel.tsx`
- `frontend/src/pages/project/WorldPanel.tsx`
- `frontend/src/router.tsx`
- `frontend/src/stores/project.test.ts`
- `frontend/src/stores/project.ts`
- `frontend/src/utils/projectAccess.test.ts`
- `frontend/src/utils/projectAccess.ts`

## 3. docs(admin): 记录作品管理权限边界与验收结果

范围：25 个文件。

- `.trellis/spec/backend/admin-projects.md`
- `.trellis/spec/backend/auth-admin.md`
- `.trellis/spec/backend/database-guidelines.md`
- `.trellis/spec/backend/index.md`
- `.trellis/spec/backend/quality-guidelines.md`
- `.trellis/spec/frontend/component-guidelines.md`
- `.trellis/spec/frontend/index.md`
- `.trellis/spec/frontend/state-management.md`
- `.trellis/tasks/10-30-admin-project-management/backend-checks.md`
- `.trellis/tasks/10-30-admin-project-management/browser-checks.md`
- `.trellis/tasks/10-30-admin-project-management/check.jsonl`
- `.trellis/tasks/10-30-admin-project-management/commit-plan.md`
- `.trellis/tasks/10-30-admin-project-management/design.md`
- `.trellis/tasks/10-30-admin-project-management/final-check.md`
- `.trellis/tasks/10-30-admin-project-management/frontend-checks.md`
- `.trellis/tasks/10-30-admin-project-management/implement.jsonl`
- `.trellis/tasks/10-30-admin-project-management/implement.md`
- `.trellis/tasks/10-30-admin-project-management/prd.md`
- `.trellis/tasks/10-30-admin-project-management/research/backend-admin-access.md`
- `.trellis/tasks/10-30-admin-project-management/research/frontend-admin-workspace.md`
- `.trellis/tasks/10-30-admin-project-management/research/spec-read-map.md`
- `.trellis/tasks/10-30-admin-project-management/research/verification.md`
- `.trellis/tasks/10-30-admin-project-management/task.json`
- `README.md`
- `.trellis/tasks/10-30-admin-project-management/naming-check.md`

## 工作提交记录

- `7706563`：后端，27 个文件。
- `e4e46ed`：前端，35 个文件。
- `22050b8`：规范与验收文档，25 个文件。

## 未识别的改动（不纳入提交）

- `frontend/.codex-works-preview-20261030.html`
- `frontend/.codex-works-preview-20261030.tsx`
- `frontend/src/components/shelf/BookCover.tsx`
- `frontend/src/components/shelf/LibraryBookCard.tsx`
- `frontend/src/pages/ProjectListPage.css`
- `frontend/src/pages/ProjectListPage.tsx`

## 后续收尾

三组工作提交已完成，收尾按 `trellis-finish-work` 归档当前任务并记录会话，归档和日志使用各自的独立提交。具体完成状态见归档后的 `task.json` 与开发者日志。不执行推送，不合并主分支。
