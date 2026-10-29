# 本地提交计划

授权：用户在官网、首页认证弹窗、登录注册页补齐并验收后，明确要求「提交归档」。本轮按以下清单创建本地工作提交，再执行Trellis任务归档和会话日志；不推送远程。

实现、独立审查、浏览器验收和最终 lint/build/417项测试已完成。当前分支 main；只提交以下明确文件。

## 1. feat(frontend): 实现知页官网认证、布局与真实数据书库

- [package-lock.json](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/package-lock.json) — `frontend/package-lock.json`
- [package.json](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/package.json) — `frontend/package.json`
- [App.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/App.tsx) — `frontend/src/App.tsx`
- [AppLayout.css](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/components/AppLayout.css) — `frontend/src/components/AppLayout.css`
- [AppLayout.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/components/AppLayout.tsx) — `frontend/src/components/AppLayout.tsx`
- [StudioNavigation.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/components/StudioNavigation.tsx) — `frontend/src/components/StudioNavigation.tsx`
- [StudioUI.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/components/StudioUI.tsx) — `frontend/src/components/StudioUI.tsx`
- [StudioUI.css](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/components/StudioUI.css) — `frontend/src/components/StudioUI.css`
- [TaskPanel.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/components/TaskPanel.tsx) — `frontend/src/components/TaskPanel.tsx`
- [AgentDock.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/components/agent/AgentDock.tsx) — `frontend/src/components/agent/AgentDock.tsx`
- [BookCover.css](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/components/shelf/BookCover.css) — `frontend/src/components/shelf/BookCover.css`
- [BookCover.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/components/shelf/BookCover.tsx) — `frontend/src/components/shelf/BookCover.tsx`
- [ContinueWriting.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/components/shelf/ContinueWriting.tsx) — `frontend/src/components/shelf/ContinueWriting.tsx`
- [CreateProjectModal.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/components/shelf/CreateProjectModal.tsx) — `frontend/src/components/shelf/CreateProjectModal.tsx`
- [LibraryBookCard.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/components/shelf/LibraryBookCard.tsx) — `frontend/src/components/shelf/LibraryBookCard.tsx`
- [index.css](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/index.css) — `frontend/src/index.css`
- [HomePage.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/pages/HomePage.tsx) — `frontend/src/pages/HomePage.tsx`
- [HomePage.css](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/pages/HomePage.css) — `frontend/src/pages/HomePage.css`
- [LoginPage.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/pages/LoginPage.tsx) — `frontend/src/pages/LoginPage.tsx`
- [LoginPage.css](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/pages/LoginPage.css) — `frontend/src/pages/LoginPage.css`
- [HomeEntryDialog.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/components/auth/HomeEntryDialog.tsx) — `frontend/src/components/auth/HomeEntryDialog.tsx`
- [HomeEntryDialog.css](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/components/auth/HomeEntryDialog.css) — `frontend/src/components/auth/HomeEntryDialog.css`
- [auth-mountain-story.jpg](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/assets/auth-mountain-story.jpg) — `frontend/src/assets/auth-mountain-story.jpg`
- [router.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/router.tsx) — `frontend/src/router.tsx`
- [ProjectListPage.css](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/pages/ProjectListPage.css) — `frontend/src/pages/ProjectListPage.css`
- [ProjectListPage.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/pages/ProjectListPage.tsx) — `frontend/src/pages/ProjectListPage.tsx`
- [ProjectLayout.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/pages/project/ProjectLayout.tsx) — `frontend/src/pages/project/ProjectLayout.tsx`
- [ThemeSwitch.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/theme/ThemeSwitch.tsx) — `frontend/src/theme/ThemeSwitch.tsx`
- [ThemeSync.tsx](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/theme/ThemeSync.tsx) — `frontend/src/theme/ThemeSync.tsx`
- [palette.ts](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/theme/palette.ts) — `frontend/src/theme/palette.ts`
- [shelf.test.ts](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/utils/shelf.test.ts) — `frontend/src/utils/shelf.test.ts`
- [shelf.ts](/Users/wenyuantc/IdeaProjects/my/aiNovel/frontend/src/utils/shelf.ts) — `frontend/src/utils/shelf.ts`

## 2. docs(frontend): 同步知页界面契约与验收记录

- `.trellis/spec/frontend/component-guidelines.md`
- `.trellis/spec/frontend/state-management.md`
- `.trellis/tasks/10-29-zhiye-library-ui/check.jsonl`
- `.trellis/tasks/10-29-zhiye-library-ui/commit-plan.md`
- `.trellis/tasks/10-29-zhiye-library-ui/design.md`
- `.trellis/tasks/10-29-zhiye-library-ui/implement.jsonl`
- `.trellis/tasks/10-29-zhiye-library-ui/implement.md`
- `.trellis/tasks/10-29-zhiye-library-ui/prd.md`
- `.trellis/tasks/10-29-zhiye-library-ui/research/design-reference.md`
- `.trellis/tasks/10-29-zhiye-library-ui/research/auth-design-reference.md`
- `.trellis/tasks/10-29-zhiye-library-ui/task.json`
- `.trellis/tasks/10-29-zhiye-library-ui/verification.md`

## 排除

- `pelican-bicycle.html`：本轮未编辑，排除。

工作提交完成后，使用 `task.py archive` 归档当前任务，再用 `add_session.py` 记录两个工作提交的哈希。归档和日志由各自脚本生成独立流程提交，执行结果以Git历史与归档元数据为准。
