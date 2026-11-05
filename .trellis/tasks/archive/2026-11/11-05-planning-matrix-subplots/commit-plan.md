# 已确认的提交方案

状态：用户已回复“明白了，按清单提交并收尾。”，授权依次执行以下三笔提交。后端1900项全过（含PG），前端858项全过，lint/build、真实PG与浏览器验收通过。

不推送远程，不改写已有提交；分支 `codex/planning-matrix-subplots`，基线 `4c80eca`。

## 1. 功能工作提交

`feat(planning): 新增规划矩阵与支线交织并接入AI写作`

共 112 个文件，包含实现、回归测试、项目规范、TODOS状态及本任务设计/验收产物：

- `.trellis/spec/backend/index.md`
- `.trellis/spec/backend/outline.md`
- `.trellis/spec/backend/planning.md`
- `.trellis/spec/backend/project-transfer.md`
- `.trellis/spec/backend/prompts-styles.md`
- `.trellis/spec/frontend/index.md`
- `.trellis/tasks/11-05-planning-matrix-subplots/check.jsonl`
- `.trellis/tasks/11-05-planning-matrix-subplots/commit-plan.md`
- `.trellis/tasks/11-05-planning-matrix-subplots/design.md`
- `.trellis/tasks/11-05-planning-matrix-subplots/implement.jsonl`
- `.trellis/tasks/11-05-planning-matrix-subplots/implement.md`
- `.trellis/tasks/11-05-planning-matrix-subplots/prd.md`
- `.trellis/tasks/11-05-planning-matrix-subplots/progress.md`
- `.trellis/tasks/11-05-planning-matrix-subplots/research/ai-context-boundary.md`
- `.trellis/tasks/11-05-planning-matrix-subplots/research/ai-integration-design.md`
- `.trellis/tasks/11-05-planning-matrix-subplots/research/browser-verification.md`
- `.trellis/tasks/11-05-planning-matrix-subplots/research/codebase-foundations.md`
- `.trellis/tasks/11-05-planning-matrix-subplots/research/planning-anchor-design.md`
- `.trellis/tasks/11-05-planning-matrix-subplots/research/planning-consistency-review.md`
- `.trellis/tasks/11-05-planning-matrix-subplots/research/postgres-verification.md`
- `.trellis/tasks/11-05-planning-matrix-subplots/research/reference-products.md`
- `.trellis/tasks/11-05-planning-matrix-subplots/research/relationship-and-crossing-contract.md`
- `.trellis/tasks/11-05-planning-matrix-subplots/research/ui-and-api-contract.md`
- `.trellis/tasks/11-05-planning-matrix-subplots/task.json`
- `.trellis/tasks/11-05-planning-matrix-subplots/verification.md`
- `TODOS.md`
- `backend/alembic/versions/0034_planning_matrix.py`
- `backend/app/ai/prompts/chapter.py`
- `backend/app/ai/prompts/planning.py`
- `backend/app/ai/prompts/registry.py`
- `backend/app/ai/prompts/rewrite.py`
- `backend/app/ai/prompts/templates/chapter.generate.user.j2`
- `backend/app/ai/prompts/templates/chapter.revise.user.j2`
- `backend/app/ai/prompts/templates/chapter.rewrite.user.j2`
- `backend/app/ai/prompts/templates/planning.context.j2`
- `backend/app/api/v1/__init__.py`
- `backend/app/api/v1/planning.py`
- `backend/app/models/__init__.py`
- `backend/app/models/entities.py`
- `backend/app/models/planning.py`
- `backend/app/schemas/audit.py`
- `backend/app/schemas/planning.py`
- `backend/app/services/agent/effects.py`
- `backend/app/services/career_service.py`
- `backend/app/services/cast_service.py`
- `backend/app/services/chapter_service.py`
- `backend/app/services/context_builder.py`
- `backend/app/services/generation_service.py`
- `backend/app/services/organization_service.py`
- `backend/app/services/outline_service.py`
- `backend/app/services/outline_snapshot_service.py`
- `backend/app/services/outline_transfer_service.py`
- `backend/app/services/plan_service.py`
- `backend/app/services/planning_context_service.py`
- `backend/app/services/planning_lifecycle.py`
- `backend/app/services/planning_queries.py`
- `backend/app/services/planning_service.py`
- `backend/app/services/project_transfer/exporter.py`
- `backend/app/services/project_transfer/importer.py`
- `backend/app/services/project_transfer/planning.py`
- `backend/app/services/project_transfer/registry.py`
- `backend/app/services/project_transfer/remap.py`
- `backend/app/services/relationship_service.py`
- `backend/app/services/rewrite_service.py`
- `backend/app/services/story_service.py`
- `backend/app/services/wizard_service.py`
- `backend/tests/test_agent_api.py`
- `backend/tests/test_mention_compat.py`
- `backend/tests/test_open_api.py`
- `backend/tests/test_planning.py`
- `backend/tests/test_planning_access.py`
- `backend/tests/test_planning_context.py`
- `backend/tests/test_planning_lifecycle.py`
- `backend/tests/test_planning_migration.py`
- `backend/tests/test_planning_readonly.py`
- `backend/tests/test_project_cover.py`
- `backend/tests/test_project_transfer.py`
- `backend/tests/test_reader.py`
- `frontend/src/api/auditLogs.ts`
- `frontend/src/api/planning.test.ts`
- `frontend/src/api/planning.ts`
- `frontend/src/api/types.ts`
- `frontend/src/components/StudioNavigation.tsx`
- `frontend/src/components/outlines/ExpandDraftResult.tsx`
- `frontend/src/components/outlines/PlanList.tsx`
- `frontend/src/components/planning/PlanningBatchModal.tsx`
- `frontend/src/components/planning/PlanningCatalog.tsx`
- `frontend/src/components/planning/PlanningCellDrawer.tsx`
- `frontend/src/components/planning/PlanningColumnPicker.tsx`
- `frontend/src/components/planning/PlanningCrossingList.tsx`
- `frontend/src/components/planning/PlanningCrossingModal.tsx`
- `frontend/src/components/planning/PlanningDetachedDrawer.tsx`
- `frontend/src/components/planning/PlanningFields.tsx`
- `frontend/src/components/planning/PlanningMatrixTable.tsx`
- `frontend/src/components/planning/PlanningMergeModal.tsx`
- `frontend/src/components/planning/PlanningNodeModal.tsx`
- `frontend/src/components/planning/PlanningPreviewDrawer.tsx`
- `frontend/src/components/planning/PlanningRevisionReview.tsx`
- `frontend/src/components/planning/PlanningWeaveDiagram.tsx`
- `frontend/src/components/planning/PlanningWeaveTable.tsx`
- `frontend/src/components/planning/usePlanningMutation.ts`
- `frontend/src/components/relations/RelationTimelineDeepLink.tsx`
- `frontend/src/pages/project/CareersPanel.tsx`
- `frontend/src/pages/project/OrganizationsPanel.tsx`
- `frontend/src/pages/project/OutlinesPanel.tsx`
- `frontend/src/pages/project/PlanningPanel.css`
- `frontend/src/pages/project/PlanningPanel.tsx`
- `frontend/src/pages/project/RelationshipsPanel.tsx`
- `frontend/src/router.tsx`
- `frontend/src/utils/planning.test.ts`
- `frontend/src/utils/planning.ts`
- `frontend/src/utils/projectRefresh.ts`

## 2. 任务归档提交

`chore(task): archive 11-05-planning-matrix-subplots`

- 功能工作提交成功后，使用Trellis脚本将本任务目录移至`.trellis/tasks/archive/2026-11/11-05-planning-matrix-subplots/`并标记完成。

## 3. 会话日志提交

`chore: record journal`

- 使用Trellis脚本更新`.trellis/workspace/aiNovel/index.md`及当前`journal-*.md`，只记录本次工作提交与验证证据。
- 日志标题：`规划矩阵与支线交织接入AI写作`。
- 日志正文：章/未建章规划矩阵、全设定关联、POV/标签批量、支线节点/明确交汇/断档、AI生成与重写、稳定身份/备份v3/撤销、事务边界修复；记录最终后端/前端测试统计、真实PG和浏览器验收，以及独立场景/静态档案边界。工作提交hash在提交成功后由脚本参数关联。

## 不纳入提交的无关内容

- `.openidea/`（本会话未创建或修改，不识别的外部内容）
- `pelican-cycling.html`（实施前已有）

按用户确认的顺序执行，范围不变。
