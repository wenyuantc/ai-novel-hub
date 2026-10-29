# 工坊批量提交计划

日期：2026-10-29；分支：`codex/prompt-workshop`。

实现、独立检查与浏览器验收已完成，门禁见 [验收记录](./verification.md)。用户于 2026-10-29 明确授权归档、推送和合并到 main，按以下顺序提交后执行收尾；推送目标等待用户补充远端 URL。

本任务开始前已有的 PRD、design.md、implement.md、上下文清单也明确纳入第 4 个提交，供用户一并确认。
其他任务和临时 HTML 不纳入提交。

## 1. `refactor(backend): 公开风格与模板保存辅助函数`

- [backend/app/services/style_service.py](/Users/wenyuantc/IdeaProjects/my/aiNovel/backend/app/services/style_service.py)
- [backend/app/services/prompt_service.py](/Users/wenyuantc/IdeaProjects/my/aiNovel/backend/app/services/prompt_service.py)
- [backend/app/services/project_transfer/importer.py](/Users/wenyuantc/IdeaProjects/my/aiNovel/backend/app/services/project_transfer/importer.py)

## 2. `feat(backend): 新增实例内提示词工坊`

- [backend/alembic/versions/0020_prompt_workshop.py](/Users/wenyuantc/IdeaProjects/my/aiNovel/backend/alembic/versions/0020_prompt_workshop.py)
- [backend/app/api/v1/__init__.py](/Users/wenyuantc/IdeaProjects/my/aiNovel/backend/app/api/v1/__init__.py)
- [backend/app/api/v1/workshop.py](/Users/wenyuantc/IdeaProjects/my/aiNovel/backend/app/api/v1/workshop.py)
- [backend/app/main.py](/Users/wenyuantc/IdeaProjects/my/aiNovel/backend/app/main.py)
- [backend/app/models/__init__.py](/Users/wenyuantc/IdeaProjects/my/aiNovel/backend/app/models/__init__.py)
- [backend/app/models/workshop.py](/Users/wenyuantc/IdeaProjects/my/aiNovel/backend/app/models/workshop.py)
- [backend/app/schemas/workshop.py](/Users/wenyuantc/IdeaProjects/my/aiNovel/backend/app/schemas/workshop.py)
- [backend/app/services/workshop_service.py](/Users/wenyuantc/IdeaProjects/my/aiNovel/backend/app/services/workshop_service.py)
- [backend/tests/test_migrations.py](/Users/wenyuantc/IdeaProjects/my/aiNovel/backend/tests/test_migrations.py)
- [backend/tests/test_workshop.py](/Users/wenyuantc/IdeaProjects/my/aiNovel/backend/tests/test_workshop.py)

## 3. `feat(frontend): 新增提示词工坊分享审核页面`

- [frontend/src/api/types.ts](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/api/types.ts)
- [frontend/src/api/workshop.test.ts](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/api/workshop.test.ts)
- [frontend/src/api/workshop.ts](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/api/workshop.ts)
- [frontend/src/components/AppLayout.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/components/AppLayout.tsx)
- [frontend/src/components/prompts/PromptDiffModal.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/components/prompts/PromptDiffModal.tsx)
- [frontend/src/components/workshop/ReviewPanel.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/components/workshop/ReviewPanel.tsx)
- [frontend/src/components/workshop/ShareToWorkshopModal.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/components/workshop/ShareToWorkshopModal.tsx)
- [frontend/src/components/workshop/WorkshopContent.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/components/workshop/WorkshopContent.tsx)
- [frontend/src/components/workshop/WorkshopItemCard.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/components/workshop/WorkshopItemCard.tsx)
- [frontend/src/components/workshop/WorkshopItemControls.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/components/workshop/WorkshopItemControls.tsx)
- [frontend/src/components/workshop/WorkshopItemDrawer.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/components/workshop/WorkshopItemDrawer.tsx)
- [frontend/src/components/workshop/WorkshopMetadataFields.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/components/workshop/WorkshopMetadataFields.tsx)
- [frontend/src/components/workshop/workshopUI.test.ts](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/components/workshop/workshopUI.test.ts)
- [frontend/src/pages/PromptTemplatesPage.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/pages/PromptTemplatesPage.tsx)
- [frontend/src/pages/StylesPage.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/pages/StylesPage.tsx)
- [frontend/src/pages/WorkshopPage.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/pages/WorkshopPage.tsx)
- [frontend/src/router.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/router.tsx)
- [frontend/src/utils/workshop.test.ts](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/utils/workshop.test.ts)
- [frontend/src/utils/workshop.ts](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/utils/workshop.ts)

## 4. `docs: 记录工坊契约与验收结果`

- [.trellis/spec/backend/database-guidelines.md](/Users/wenyuantc/IdeaProjects/my/aiNovel/.trellis/spec/backend/database-guidelines.md)
- [.trellis/spec/backend/index.md](/Users/wenyuantc/IdeaProjects/my/aiNovel/.trellis/spec/backend/index.md)
- [.trellis/spec/backend/workshop.md](/Users/wenyuantc/IdeaProjects/my/aiNovel/.trellis/spec/backend/workshop.md)
- [.trellis/spec/frontend/component-guidelines.md](/Users/wenyuantc/IdeaProjects/my/aiNovel/.trellis/spec/frontend/component-guidelines.md)
- [.trellis/spec/frontend/index.md](/Users/wenyuantc/IdeaProjects/my/aiNovel/.trellis/spec/frontend/index.md)
- [.trellis/tasks/10-23-mumu-parity/prd.md](/Users/wenyuantc/IdeaProjects/my/aiNovel/.trellis/tasks/10-23-mumu-parity/prd.md)
- [.trellis/tasks/10-26-prompt-workshop/check.jsonl](/Users/wenyuantc/IdeaProjects/my/aiNovel/.trellis/tasks/10-26-prompt-workshop/check.jsonl)
- [.trellis/tasks/10-26-prompt-workshop/commit-plan.md](/Users/wenyuantc/IdeaProjects/my/aiNovel/.trellis/tasks/10-26-prompt-workshop/commit-plan.md)
- [.trellis/tasks/10-26-prompt-workshop/design.md](/Users/wenyuantc/IdeaProjects/my/aiNovel/.trellis/tasks/10-26-prompt-workshop/design.md)
- [.trellis/tasks/10-26-prompt-workshop/implement.jsonl](/Users/wenyuantc/IdeaProjects/my/aiNovel/.trellis/tasks/10-26-prompt-workshop/implement.jsonl)
- [.trellis/tasks/10-26-prompt-workshop/implement.md](/Users/wenyuantc/IdeaProjects/my/aiNovel/.trellis/tasks/10-26-prompt-workshop/implement.md)
- [.trellis/tasks/10-26-prompt-workshop/prd.md](/Users/wenyuantc/IdeaProjects/my/aiNovel/.trellis/tasks/10-26-prompt-workshop/prd.md)
- [.trellis/tasks/10-26-prompt-workshop/task.json](/Users/wenyuantc/IdeaProjects/my/aiNovel/.trellis/tasks/10-26-prompt-workshop/task.json)
- [.trellis/tasks/10-26-prompt-workshop/verification.md](/Users/wenyuantc/IdeaProjects/my/aiNovel/.trellis/tasks/10-26-prompt-workshop/verification.md)

## 明确排除

- `.trellis/tasks/10-23-project-cover/`：会话开始前已有的其他任务。
- `pelican-bicycle.html`：会话开始前已有的无关文件。

产品提交后再按 Trellis 收尾流程处理归档与会话日志，归档和日志使用独立提交。
