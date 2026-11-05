# 提交清单

用户已于 2026-11-05 明确授权「提交 收尾 合并到main并推送」。以下为确认后的三批工作提交；目标分支：`codex/mention-tracking`。

按 `.trellis/workflow.md` Phase 3.4 依次提交，不 amend。随后按 finish-work 归档本次父/子任务并记录会话，记账提交放在工作提交之后；最后按用户本次授权合并到 `main` 并推送 `origin/main`。

产品代码、规范、任务规划及验收材料合计如下；保留现有无关文件。

## 1. `feat(story): 添加作品笔记与四源提及接口`

35 个文件：

- `backend/alembic/versions/0032_project_notes.py`
- `backend/alembic/versions/0033_world_aliases.py`
- `backend/app/api/v1/__init__.py`
- `backend/app/api/v1/mentions.py`
- `backend/app/api/v1/notes.py`
- `backend/app/models/__init__.py`
- `backend/app/models/entities.py`
- `backend/app/models/notes.py`
- `backend/app/schemas/mentions.py`
- `backend/app/schemas/notes.py`
- `backend/app/schemas/story.py`
- `backend/app/services/agent/effects.py`
- `backend/app/services/mentions/__init__.py`
- `backend/app/services/mentions/context.py`
- `backend/app/services/mentions/matcher.py`
- `backend/app/services/mentions/query.py`
- `backend/app/services/mentions/sources.py`
- `backend/app/services/mentions/summary.py`
- `backend/app/services/mentions/window.py`
- `backend/app/services/note_service.py`
- `backend/app/services/project_transfer/exporter.py`
- `backend/app/services/project_transfer/importer.py`
- `backend/app/services/project_transfer/registry.py`
- `backend/tests/conftest.py`
- `backend/tests/test_agent_api.py`
- `backend/tests/test_mention_compat.py`
- `backend/tests/test_mentions.py`
- `backend/tests/test_mentions_matcher.py`
- `backend/tests/test_mentions_scale.py`
- `backend/tests/test_notes.py`
- `backend/tests/test_notes_migration.py`
- `backend/tests/test_project_cover.py`
- `backend/tests/test_project_transfer.py`
- `backend/tests/test_projects_and_story.py`
- `backend/tests/test_reader.py`

## 2. `feat(studio): 接入提及跳转与出场热力图`

38 个文件：

- `frontend/src/api/mentions.test.ts`
- `frontend/src/api/mentions.ts`
- `frontend/src/api/notes.test.ts`
- `frontend/src/api/notes.ts`
- `frontend/src/api/types.ts`
- `frontend/src/components/StudioNavigation.tsx`
- `frontend/src/components/agent/AgentConversationView.tsx`
- `frontend/src/components/agent/AgentPanel.tsx`
- `frontend/src/components/agent/AgentTimeline.tsx`
- `frontend/src/components/mentions/EntityMentions.tsx`
- `frontend/src/components/mentions/EntityPreview.tsx`
- `frontend/src/components/mentions/MentionHeatmap.tsx`
- `frontend/src/components/mentions/MentionPreview.tsx`
- `frontend/src/components/mentions/MentionText.tsx`
- `frontend/src/components/mentions/SourceMentionFocus.tsx`
- `frontend/src/components/mentions/mentionRendering.test.ts`
- `frontend/src/components/notes/NoteEditor.tsx`
- `frontend/src/components/notes/noteDraft.test.ts`
- `frontend/src/components/notes/noteDraft.ts`
- `frontend/src/components/progressions/useWorldBaselineEditor.ts`
- `frontend/src/components/progressions/worldForm.test.ts`
- `frontend/src/components/progressions/worldForm.ts`
- `frontend/src/hooks/useEntityDeepLink.ts`
- `frontend/src/hooks/useMentionQuery.ts`
- `frontend/src/pages/ReaderPage.tsx`
- `frontend/src/pages/project/ChapterEditorPage.tsx`
- `frontend/src/pages/project/CharactersPanel.tsx`
- `frontend/src/pages/project/KnowledgePanel.tsx`
- `frontend/src/pages/project/MentionsPanel.tsx`
- `frontend/src/pages/project/NotesPanel.tsx`
- `frontend/src/pages/project/WorldPanel.tsx`
- `frontend/src/pages/project/worldReadOnly.test.ts`
- `frontend/src/router.tsx`
- `frontend/src/utils/mentionStats.test.ts`
- `frontend/src/utils/mentionStats.ts`
- `frontend/src/utils/mentions.test.ts`
- `frontend/src/utils/mentions.ts`
- `frontend/src/utils/projectRefresh.ts`

## 3. `docs(story): 记录提及追踪规范与验收结果`

35 个文件：

- `.trellis/spec/backend/index.md`
- `.trellis/spec/backend/mention-tracking.md`
- `.trellis/spec/backend/project-transfer.md`
- `.trellis/spec/frontend/index.md`
- `.trellis/tasks/11-05-mention-index-navigation/check.jsonl`
- `.trellis/tasks/11-05-mention-index-navigation/design.md`
- `.trellis/tasks/11-05-mention-index-navigation/implement.jsonl`
- `.trellis/tasks/11-05-mention-index-navigation/implement.md`
- `.trellis/tasks/11-05-mention-index-navigation/implementation-backend.md`
- `.trellis/tasks/11-05-mention-index-navigation/implementation-frontend.md`
- `.trellis/tasks/11-05-mention-index-navigation/implementation-stats.md`
- `.trellis/tasks/11-05-mention-index-navigation/prd.md`
- `.trellis/tasks/11-05-mention-index-navigation/task.json`
- `.trellis/tasks/11-05-mention-tracking/browser-verification.md`
- `.trellis/tasks/11-05-mention-tracking/check-report.md`
- `.trellis/tasks/11-05-mention-tracking/check.jsonl`
- `.trellis/tasks/11-05-mention-tracking/commit-plan.md`
- `.trellis/tasks/11-05-mention-tracking/design.md`
- `.trellis/tasks/11-05-mention-tracking/implement.jsonl`
- `.trellis/tasks/11-05-mention-tracking/implement.md`
- `.trellis/tasks/11-05-mention-tracking/implementation-log.md`
- `.trellis/tasks/11-05-mention-tracking/prd.md`
- `.trellis/tasks/11-05-mention-tracking/research/backend-sources.md`
- `.trellis/tasks/11-05-mention-tracking/research/frontend-navigation.md`
- `.trellis/tasks/11-05-mention-tracking/research/official-reference.md`
- `.trellis/tasks/11-05-mention-tracking/research/plan-contract-review.md`
- `.trellis/tasks/11-05-mention-tracking/task.json`
- `.trellis/tasks/11-05-project-notes/check.jsonl`
- `.trellis/tasks/11-05-project-notes/design.md`
- `.trellis/tasks/11-05-project-notes/implement.jsonl`
- `.trellis/tasks/11-05-project-notes/implement.md`
- `.trellis/tasks/11-05-project-notes/implementation-log.md`
- `.trellis/tasks/11-05-project-notes/prd.md`
- `.trellis/tasks/11-05-project-notes/task.json`
- `TODOS.md`

## 排除文件

- `pelican-cycling.html`：会话开始前已存在的无关未跟踪文件，保持原样，不纳入提交。

## 验证依据

- `check-report.md`：后端 Ruff、前端 lint/type-check/build 通过；前端 838 项通过；后端原始全量及失败文件复验合计 1825 个不同测试通过，1 项既有 PostgreSQL 条件跳过。原跑与复验已明确区分。
- `browser-verification.md`：四来源、草稿保护、旧位置失效、统计、375px/桌面、主题和键盘验收。
- `implementation-log.md`：实施、规格同步与尚待提交收尾的状态。
