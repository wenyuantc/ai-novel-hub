# 前端开发规范

> `frontend/` 的约定（Vite 8 + React 18 + TypeScript strict + Ant Design 5 + Zustand 5 + React Router 6 + Vitest）。

---

## 概述

前端是一个单页应用：开发时由 Vite 提供服务（`/api` 代理到 FastAPI 后端），构建后产出一个静态包。所有后端调用都经过 `src/api/`（fetch 封装 + SSE 客户端）；UI 使用 Ant Design 并启用 `zh_CN` 语言包；全局状态由三个小型 Zustand store 承担；其余一律是组件本地状态。这些文档描述的是代码当前的实际行为——修改某一层之前请先阅读对应文档。

---

## 规范索引

| 规范 | 说明 | 状态 |
|-------|-------------|--------|
| [目录结构](./directory-structure.md) | `src/api`、`stores`、`hooks`、`components`、`pages/project`、`utils`、`router.tsx` 的布局与命名 | 已填写 |
| [组件规范](./component-guidelines.md) | AntD/zh_CN 配置、可复用组件、页面模式、章节编辑器行为、AI 配置页提示、管理员用户页 / `RequireAdmin`、项目智能体面板与 external 提案卡片、Skill / MCP 插件页（`SkillSelect` 复用、请求头编辑语义） | 已填写 |
| [项目封面跨层契约](../backend/project-cover.md) | 概览/书架图片、上传与预览确认弹窗、微秒版本 URL、只合并封面状态以保留未保存表单 | 已填写 |
| [AI 人物关系跨层契约](../backend/relationship-generation.md) | 多选角色生成缺失关系、结果守卫与勾选、重开防重复、任务面板保存刷新、409 重试 | 已填写 |
| [工坊跨层契约](../backend/workshop.md) | `/workshop` 浏览 / 我的分享 / 审核、匿名署名、分享与导入、模板差异及历史恢复 | 已填写 |
| [Hook 规范](./hook-guidelines.md) | `useStreamTask`、`useLoader`、`useProjectId`、`useHasAIConfig`；SSE 客户端 code-spec 场景 | 已填写 |
| [状态管理](./state-management.md) | Zustand store（auth / aiConfigs / project / tasks / styles / skills）、全局与本地状态、登出/401 时清空 | 已填写 |
| [管理员操作审计跨层契约](../backend/admin-audit.md) | 管理员筛选分页/只读详情、历史 actor、版本可变提示、日期预览确认及旧响应失效、移动卡片 | 已填写 |
| [管理员系统设置跨层契约](../backend/system-settings.md) | 六项编辑与来源、逐项恢复/版本冲突、StrictMode与确认框清理、30秒公开配置及旧响应失效 | 已填写 |
| [管理员首页与全站统计跨层契约](../backend/admin-dashboard.md) | 含回收站总量、独立任务累计、7/30/90日趋势、缺口/null、60秒刷新、可读SVG与数据表 | 已填写 |
| [管理员全站任务监控跨层契约](../backend/admin-task-monitoring.md) | 安全列表/详情、现存筛选计数、UUID深链、5秒刷新/失效、二阶段取消及受理审计语义 | 已填写 |
| [管理员作品管理跨层契约](../backend/admin-projects.md) | 管理列表、owner归属、复用人工编辑区、自动任务与私有资源隔离、Reader不写他人进度 | 已填写 |
| [作品回收站跨层契约](../backend/recycle-bin.md) | 管理员列表/恢复/名称确认彻删、原始删除代次、30天边界、删号保护与目标级状态失效 | 已填写 |
| [质量规范](./quality-guidelines.md) | lint/build/test 门禁、Vite 代理、包体积、禁止/必需模式、评审清单 | 已填写 |
| [类型安全](./type-safety.md) | `types.ts` 镜像后端 schema、枚举、PATCH 语义、UTC 时间解析；API 客户端 code-spec 场景 | 已填写 |
| [管理员用户详情跨层契约](../backend/auth-admin.md) | 最近登录、正常/回收站/删除中基础统计、固定owner作品、共享账号动作及离页/身份/请求代次失效 | 已填写 |
| [平台 API Key 跨层契约](../backend/open-api.md) | 密钥管理页、主动显隐 / 复制、异步失效、有效期与小说范围、公开客户开发指南及 Swagger 文档入口 | 已填写 |

Code-spec 场景（7 段式格式）：

- API 客户端契约（`src/api/client.ts`、`src/api/projects.ts` 导出）——见 [类型安全](./type-safety.md#场景api-客户端契约)。
- SSE 客户端解析器（`src/api/sse.ts`、`src/api/sse.test.ts`）——见 [Hook 规范](./hook-guidelines.md#场景sse-客户端解析器)。

---

**语言**：所有文档均使用**简体中文**编写；代码、标识符、路径与命令保持原样。
