# 提交计划

- 作者已确认提交、收尾、合并 main 并推送；创建 `codex/series-shared-settings` 并同步任务分支记录。
- 工作提交：`feat(series): 支持系列作品与跨书共享设定`。
- 下列工作文件已提交为 `357071fdda5faa367e51a206432817ef197a9c11`，随后 fast-forward 合并至 `main`；任务文档、归档与日志单独提交。按用户要求合并后不重跑测试。

## 后端实现、迁移与测试（47）

- `backend/alembic/versions/0038_series_settings.py`（新增）
- `backend/app/api/v1/__init__.py`（修改）
- `backend/app/api/v1/projects.py`（修改）
- `backend/app/api/v1/series.py`（新增）
- `backend/app/api/v1/story.py`（修改）
- `backend/app/models/__init__.py`（修改）
- `backend/app/models/entities.py`（修改）
- `backend/app/models/series.py`（新增）
- `backend/app/schemas/planning.py`（修改）
- `backend/app/schemas/project.py`（修改）
- `backend/app/schemas/series.py`（新增）
- `backend/app/schemas/story.py`（修改）
- `backend/app/services/agent/effects.py`（修改）
- `backend/app/services/ai_dispatch.py`（修改）
- `backend/app/services/book_import/reverse.py`（修改）
- `backend/app/services/card_service.py`（修改）
- `backend/app/services/inspiration_service.py`（修改）
- `backend/app/services/outline_snapshot_service.py`（修改）
- `backend/app/services/outline_transfer_service.py`（修改）
- `backend/app/services/planning_service.py`（修改）
- `backend/app/services/project_service.py`（修改）
- `backend/app/services/project_transfer/exporter.py`（修改）
- `backend/app/services/project_transfer/registry.py`（修改）
- `backend/app/services/series_dependencies.py`（新增）
- `backend/app/services/series_policy.py`（新增）
- `backend/app/services/series_read.py`（新增）
- `backend/app/services/series_service.py`（新增）
- `backend/app/services/setting_ai_visibility.py`（修改）
- `backend/app/services/setting_progression_service.py`（修改）
- `backend/app/services/story_service.py`（修改）
- `backend/app/services/wizard_service.py`（修改）
- `backend/tests/test_agent_api.py`（修改）
- `backend/tests/test_category_transfer.py`（修改）
- `backend/tests/test_mention_compat.py`（修改）
- `backend/tests/test_planning.py`（修改）
- `backend/tests/test_project_cover.py`（修改）
- `backend/tests/test_project_transfer.py`（修改）
- `backend/tests/test_reader.py`（修改）
- `backend/tests/test_series.py`（新增）
- `backend/tests/test_series_migration.py`（新增）
- `backend/tests/test_series_postgresql.py`（新增）
- `backend/tests/test_series_projection_guards.py`（新增）
- `backend/tests/test_series_transfer.py`（新增）
- `backend/tests/test_setting_ai_visibility_formats.py`（修改）
- `backend/tests/test_setting_ai_visibility_migration.py`（修改）
- `backend/tests/test_setting_fields_categories_roundtrip.py`（修改）
- `backend/tests/test_setting_fields_migration.py`（修改）

## 前端实现与测试（34）

- `frontend/src/api/planning.ts`（修改）
- `frontend/src/api/series.test.ts`（新增）
- `frontend/src/api/series.ts`（新增）
- `frontend/src/api/types.ts`（修改）
- `frontend/src/components/StudioNavigation.tsx`（修改）
- `frontend/src/components/chapter/CastSelect.tsx`（修改）
- `frontend/src/components/entities/CharacterCard.tsx`（修改）
- `frontend/src/components/entities/CharacterFormFields.tsx`（修改）
- `frontend/src/components/planning/PlanningFields.test.ts`（修改）
- `frontend/src/components/planning/PlanningFields.tsx`（修改）
- `frontend/src/components/progressions/useWorldBaselineEditor.ts`（修改）
- `frontend/src/components/series/BookSeriesSettings.tsx`（新增）
- `frontend/src/components/series/SeriesBlockers.tsx`（新增）
- `frontend/src/components/series/SeriesEntryEditor.tsx`（新增）
- `frontend/src/components/series/SeriesLibrary.tsx`（新增）
- `frontend/src/components/series/SeriesTransitionDialog.test.ts`（新增）
- `frontend/src/components/series/SeriesTransitionDialog.tsx`（新增）
- `frontend/src/components/series/seriesState.test.ts`（新增）
- `frontend/src/components/series/seriesState.ts`（新增）
- `frontend/src/components/series/useSeriesAction.ts`（新增）
- `frontend/src/components/settingAi/SettingAiPanel.tsx`（修改）
- `frontend/src/components/settingAi/settingAiController.test.ts`（修改）
- `frontend/src/components/settingAi/settingAiController.ts`（修改）
- `frontend/src/components/settingAi/settingAiDraft.ts`（修改）
- `frontend/src/components/settingAi/settingAiOptions.ts`（新增）
- `frontend/src/components/shelf/LibraryBookCard.tsx`（修改）
- `frontend/src/components/transfer/BackupModal.tsx`（修改）
- `frontend/src/pages/ProjectListPage.css`（修改）
- `frontend/src/pages/ProjectListPage.tsx`（修改）
- `frontend/src/pages/SeriesPage.tsx`（新增）
- `frontend/src/pages/project/CharactersPanel.tsx`（修改）
- `frontend/src/pages/project/OverviewPanel.tsx`（修改）
- `frontend/src/pages/project/WorldPanel.tsx`（修改）
- `frontend/src/router.tsx`（修改）

## 用户文档与代码规范（6）

- `.trellis/spec/backend/index.md`（修改）
- `.trellis/spec/backend/project-transfer.md`（修改）
- `.trellis/spec/backend/series-settings.md`（新增）
- `.trellis/spec/frontend/index.md`（修改）
- `README.md`（修改）
- `TODOS.md`（修改）

## 既有未跟踪文件（不纳入）

- `.agents/skills/source-command-trellis-continue/SKILL.md`
- `.agents/skills/source-command-trellis-finish-work/SKILL.md`
