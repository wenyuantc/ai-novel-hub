# 项目封面提交计划

功能分支：`codex/project-cover`。用户已授权本地提交、归档及合并到 `main`；不推送。
现有 project-cover 规划文档随本任务纳入；原有 `pelican-bicycle.html` 排除。

最终质量门禁已通过（后端975项、前端414项及lint/build），用户已一次性确认以下批次。

## feat(backend): 支持项目封面上传、生成及备份导出

- `backend/alembic/versions/0021_project_covers.py`
- `backend/app/ai/images.py`
- `backend/app/ai/prompts/cover.py`
- `backend/app/ai/providers/base.py`
- `backend/app/ai/providers/gemini.py`
- `backend/app/ai/providers/openai_compat.py`
- `backend/app/api/v1/__init__.py`
- `backend/app/api/v1/project_cover.py`
- `backend/app/main.py`
- `backend/app/models/__init__.py`
- `backend/app/models/entities.py`
- `backend/app/schemas/project.py`
- `backend/app/schemas/project_cover.py`
- `backend/app/services/cover_service.py`
- `backend/app/services/epub_service.py`
- `backend/app/services/export_service.py`
- `backend/app/services/project_service.py`
- `backend/app/services/project_transfer/exporter.py`
- `backend/app/services/project_transfer/importer.py`
- `backend/app/services/project_transfer/registry.py`
- `backend/tests/cover_fixtures.py`
- `backend/tests/helpers.py`
- `backend/tests/test_agent_api.py`
- `backend/tests/test_epub.py`
- `backend/tests/test_image_providers.py`
- `backend/tests/test_migrations.py`
- `backend/tests/test_project_cover.py`
- `backend/tests/test_projects_and_story.py`
- `backend/tests/test_reader.py`

## feat(frontend): 接入项目封面管理与书架展示

- `frontend/src/api/projectCover.test.ts`
- `frontend/src/api/projects.ts`
- `frontend/src/api/types.ts`
- `frontend/src/components/AIConfigSelect.tsx`
- `frontend/src/components/project/CoverGenerateModal.tsx`
- `frontend/src/components/project/CoverPanel.tsx`
- `frontend/src/pages/ProjectListPage.tsx`
- `frontend/src/pages/project/OverviewPanel.tsx`
- `frontend/src/utils/projectCover.test.ts`
- `frontend/src/utils/projectCover.ts`
- `frontend/src/utils/shelf.test.ts`

## docs: 记录项目封面契约与验收结果

- `.trellis/spec/backend/index.md`
- `.trellis/spec/backend/project-cover.md`
- `.trellis/spec/backend/project-transfer.md`
- `.trellis/spec/frontend/index.md`
- `.trellis/tasks/10-23-project-cover/check.jsonl`
- `.trellis/tasks/10-23-project-cover/commit-plan.md`
- `.trellis/tasks/10-23-project-cover/design.md`
- `.trellis/tasks/10-23-project-cover/implement.jsonl`
- `.trellis/tasks/10-23-project-cover/implement.md`
- `.trellis/tasks/10-23-project-cover/prd.md`
- `.trellis/tasks/10-23-project-cover/task.json`
- `.trellis/tasks/10-23-project-cover/verification.md`

## 排除

- `pelican-bicycle.html`：本次启动前已有，与封面任务无关。

提交后按项目流程归档本任务并记录日志，不归档其他任务。
