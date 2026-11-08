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
| [设定随剧情演变跨层契约](../backend/setting-progressions.md) | 基础值、按章变化、草稿与只读预览 | 已填写 |
| [自定义设定字段跨层契约](../backend/setting-fields.md) | 四类控件、配置页、空槽首写、领域条目选择、独立草稿、四实体字段与卡片显式映射 | 已填写 |
| [整理标签与自动分类跨层契约](../backend/setting-categories.md) | 标签独立保存、规则草稿双版本、当前／按章结果与分页、只读预览、冲突对照和v5卡片 | 已填写 |
| [设定 AI 可见性跨层契约](../backend/setting-ai-visibility.md) | AI 与提及、独立草稿／冲突对照、字段隐藏、v6 卡片映射、Agent 旧会话封存 | 已填写 |
| [系列作品与共享设定跨层契约](../backend/series-settings.md) | 系列与本书 ID 分离、采用确认、归属预检、书内演变和备份提示 | 已填写 |
| [规划矩阵与支线跨层契约](../backend/planning.md) | 两类矩阵列、批量操作、交织双轴/明确交汇、CAS草稿保留、待关联/合并、AI预览 | 已填写 |
| [独立场景跨层契约](../backend/scenes.md) | 稳定身份草稿、场景目录／矩阵、按项继承、码点拆并、结构历史、只读AI提案与409显式核对 | 已填写 |
| [富文档跨层契约](../backend/rich-documents.md) | 编辑器码点映射、组合输入、区块与标记身份、AI 出站确认、任务核对及会话隔离 | 已填写 |
| [全文提及与作品笔记跨层契约](../backend/mention-tracking.md) | 草稿/阅读标记、设定预览、四来源深链、失效位置、热力图、笔记保存与加载重试 | 已填写 |
| [组件规范](./component-guidelines.md) | AntD/zh_CN 配置、可复用组件、页面模式、章节编辑器行为、AI 配置页提示、管理页 / `RequirePermission`、项目智能体面板与 external 提案卡片、Skill / MCP 插件页（`SkillSelect` 复用、请求头编辑语义） | 已填写 |
| [角色、授权与配额跨层契约](../backend/rbac-quotas.md) | required能力快照、角色/分配/配额三态、逐动作路由、同actor撤权与旧响应隔离、完整人工只读面 | 已填写 |
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
| [管理员整库备份跨层契约](../backend/admin-backups.md) | 分项权限、独立配置、稳定受理意图、秘密草稿失效、HEAD预检及原生大文件下载 | 已填写 |
| [质量规范](./quality-guidelines.md) | lint/build/test 门禁、功能增改删必须同步根目录 README、Vite 代理、包体积、禁止/必需模式、评审清单 | 已填写 |
| [类型安全](./type-safety.md) | `types.ts` 镜像后端 schema、枚举、PATCH 语义、UTC 时间解析；API 客户端 code-spec 场景 | 已填写 |
| [管理员用户详情跨层契约](../backend/auth-admin.md) | 最近登录、正常/回收站/删除中基础统计、固定owner作品、共享账号动作及离页/身份/请求代次失效 | 已填写 |
| [平台 API Key 跨层契约](../backend/open-api.md) | 密钥管理页、主动显隐 / 复制、异步失效、有效期与小说范围、公开客户开发指南及 Swagger 文档入口 | 已填写 |

Code-spec 场景（7 段式格式）：

- API 客户端契约（`src/api/client.ts`、`src/api/projects.ts` 导出）——见 [类型安全](./type-safety.md#场景api-客户端契约)。
- SSE 客户端解析器（`src/api/sse.ts`、`src/api/sse.test.ts`）——见 [Hook 规范](./hook-guidelines.md#场景sse-客户端解析器)。

---

**语言**：所有文档均使用**简体中文**编写；代码、标识符、路径与命令保持原样。
