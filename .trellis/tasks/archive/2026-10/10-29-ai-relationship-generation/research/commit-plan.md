# 本任务提交计划（已获确认）

当前分支：`feature/ai-relationship-generation`。用户于 2026-10-30 回复“提交归档”，授权按以下三批清单提交并归档，保留已知验收限制。

项目 `.trellis/workflow.md` Phase 3.4 要求展示逻辑提交及文件清单后，一次性确认；回复“行”后按顺序提交，不 amend、不 push；回复“我自己来”则交由用户手动提交。

代码与自动验证已完成：后端 1019 项、前端 428 项通过，lint、TypeScript、构建通过；隔离 TCP/SSE 覆盖生成只读、确认保存、两类并发与整批回滚。真实浏览器/主题/375px 受 Work 设置限制尚未验证，真实 PostgreSQL 并发和模型内容质量未验证。详见 [独立检查报告](./check-report.md)。

## 1. `feat(relationships): 增加 AI 批量关系草稿生成`

文件数：19。

- `backend/app/ai/prompts/__init__.py`
- `backend/app/ai/prompts/entities.py`
- `backend/app/ai/prompts/formats.py`
- `backend/app/ai/prompts/registry.py`
- `backend/app/ai/prompts/templates/relations.generate.system.j2`
- `backend/app/ai/prompts/templates/relations.generate.user.j2`
- `backend/app/api/v1/relationships.py`
- `backend/app/models/entities.py`
- `backend/app/schemas/entity_ai.py`
- `backend/app/schemas/relationship.py`
- `backend/app/schemas/task.py`
- `backend/app/services/entity_ai_service.py`
- `backend/app/services/relationship_service.py`
- `backend/app/services/task_engine/handlers/entities.py`
- `backend/tests/test_projects_and_story.py`
- `backend/tests/test_prompt_parity.py`
- `backend/tests/test_prompt_templates.py`
- `backend/tests/test_relations_generate.py`
- `backend/tests/test_relationships.py`

## 2. `feat(frontend): 接入 AI 关系生成与预览保存`

文件数：12。

- `frontend/src/api/entityTasks.test.ts`
- `frontend/src/api/entityTasks.ts`
- `frontend/src/api/relationships.test.ts`
- `frontend/src/api/relationships.ts`
- `frontend/src/api/tasks.test.ts`
- `frontend/src/api/tasks.ts`
- `frontend/src/api/types.ts`
- `frontend/src/components/entities/EntityTaskModal.tsx`
- `frontend/src/components/entities/RelationSuggestionList.tsx`
- `frontend/src/components/entities/RelationsGenerateResultView.tsx`
- `frontend/src/components/entities/taskResultViewers.ts`
- `frontend/src/pages/project/RelationshipsPanel.tsx`

## 3. `docs(relationships): 记录生成契约与验收结果`

文件数：43。

- `.trellis/spec/backend/error-handling.md`
- `.trellis/spec/backend/index.md`
- `.trellis/spec/backend/prompts-styles.md`
- `.trellis/spec/backend/quality-guidelines.md`
- `.trellis/spec/backend/relationship-generation.md`
- `.trellis/spec/backend/story-entities.md`
- `.trellis/spec/frontend/component-guidelines.md`
- `.trellis/spec/frontend/index.md`
- `.trellis/spec/frontend/type-safety.md`
- `.trellis/tasks/10-29-ai-relationship-generation/check.jsonl`
- `.trellis/tasks/10-29-ai-relationship-generation/design.md`
- `.trellis/tasks/10-29-ai-relationship-generation/implement.jsonl`
- `.trellis/tasks/10-29-ai-relationship-generation/implement.md`
- `.trellis/tasks/10-29-ai-relationship-generation/prd.md`
- `.trellis/tasks/10-29-ai-relationship-generation/research/backend-knowledge-targeted.log`
- `.trellis/tasks/10-29-ai-relationship-generation/research/backend-migrations-utf8.log`
- `.trellis/tasks/10-29-ai-relationship-generation/research/backend-pytest-initial-ascii.log`
- `.trellis/tasks/10-29-ai-relationship-generation/research/backend-pytest.log`
- `.trellis/tasks/10-29-ai-relationship-generation/research/backend-ruff.log`
- `.trellis/tasks/10-29-ai-relationship-generation/research/check-backend-pytest.log`
- `.trellis/tasks/10-29-ai-relationship-generation/research/check-backend-ruff.log`
- `.trellis/tasks/10-29-ai-relationship-generation/research/check-backend-targeted.log`
- `.trellis/tasks/10-29-ai-relationship-generation/research/check-concurrent-custom-type.log`
- `.trellis/tasks/10-29-ai-relationship-generation/research/check-frontend-lint.log`
- `.trellis/tasks/10-29-ai-relationship-generation/research/check-frontend-test.log`
- `.trellis/tasks/10-29-ai-relationship-generation/research/check-frontend-typecheck.log`
- `.trellis/tasks/10-29-ai-relationship-generation/research/check-report.md`
- `.trellis/tasks/10-29-ai-relationship-generation/research/check-type-conflict-green.log`
- `.trellis/tasks/10-29-ai-relationship-generation/research/check-type-conflict-red.log`
- `.trellis/tasks/10-29-ai-relationship-generation/research/check_concurrent_custom_type.py`
- `.trellis/tasks/10-29-ai-relationship-generation/research/commit-plan.md`
- `.trellis/tasks/10-29-ai-relationship-generation/research/frontend-build.log`
- `.trellis/tasks/10-29-ai-relationship-generation/research/frontend-lint.log`
- `.trellis/tasks/10-29-ai-relationship-generation/research/frontend-test.log`
- `.trellis/tasks/10-29-ai-relationship-generation/research/implementation-report.md`
- `.trellis/tasks/10-29-ai-relationship-generation/research/integration-map.md`
- `.trellis/tasks/10-29-ai-relationship-generation/research/relationship-contracts.md`
- `.trellis/tasks/10-29-ai-relationship-generation/research/runtime-smoke.json`
- `.trellis/tasks/10-29-ai-relationship-generation/research/runtime-verification.md`
- `.trellis/tasks/10-29-ai-relationship-generation/research/task-error-contracts.md`
- `.trellis/tasks/10-29-ai-relationship-generation/research/transport-contracts.md`
- `.trellis/tasks/10-29-ai-relationship-generation/research/ui-contracts.md`
- `.trellis/tasks/10-29-ai-relationship-generation/task.json`

## 不纳入本任务提交

- `pelican-bicycle.html`：任务开始前已有的用户文件，未修改，不暂存、不提交。

任务与会话后续归档在以上工作提交完成后按 Trellis 收尾流程处理；浏览器未验证项保持未完成，不因用户要求归档而改为通过。
