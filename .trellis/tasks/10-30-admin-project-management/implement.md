# 执行计划：管理员作品管理

## 前置门禁

- [x] 用户同意创建任务并进入规划。
- [x] 用户选择“完整管理”，确认项目资料、正文、设定可跨用户读写及项目删除。
- [x] 已阅读现有规范并完成后端授权、前端复用、验证设施研究。
- [x] 已整理并收敛 `prd.md`、`design.md`；本功能作为一个任务统一验收。
- [x] `implement.jsonl` / `check.jsonl` 路径有效且包含真实规范/研究条目，`task.py validate` 已通过；超限规范由研究索引指向原文对应章节，避免截断。
- [x] 已展示最终规划摘要，用户随后回复“开始”，明确批准实施。
- [x] 已在批准后运行 `task.py start .trellis/tasks/10-30-admin-project-management`，分支 `codex/admin-project-management`。

## 1. 后端内容授权与管理列表

- [x] 先编写有效的行为失败测试：管理员跨用户列表、核心资料/章节/世界观读写、第三名用户继续拒绝；覆盖角色撤销。
- [x] 保持 `get_owned_project` / `get_owned_chapter` 严格，增加窄人工内容授权 helper；盘点修改点的调用者，不批量替换所有 strict helper。
- [x] 实现 `/admin/projects` schema/service/route，聚合章节统计、稳定排序、q 字面搜索、owner/status 过滤、分页和 count。
- [x] ProjectOut 增加可空 owner 元数据，处理详情、更新与刷新；不扩大个人书架查询。
- [x] 项目修改/删除及所有人工设定、章节/版本/选角、封面、知识库已有内容、记忆等逐项接入授权，复用现有业务 service。
- [x] 对被 AI/日志/任务共用的子资源 helper 增加明确的人工/严格边界；所有未纳入的动作继续严格归属。
- [x] 跨用户风格引用只读；memory 人工保存跳过所有者向量任务；Reader 仅开放目录与正文，不开放进度读写。
- [x] 补充参数化隔离、混用子资源、事务/字数/版本、删除级联、私有资源与 FakeLLM 零调用验收。

建议文件：`backend/app/schemas/admin.py`、`schemas/project.py`、`api/v1/admin.py`、`services/admin_service.py` 或同层新增 `admin_project_service.py`、`services/project_service.py`、各内容 service 和混合路由、`backend/tests/test_admin_projects.py`。

前后端按冻结的接口契约分别开发；后端核心测试与原普通用户跨资源隔离通过后进行真实浏览器联调，避免等待期间修改接口约定。

## 2. 管理列表与工作区接入

- [x] 添加镜像传输类型和 `adminApi.listProjects`；新增查询参数、Cookie、错误、删除契约测试。
- [x] 添加懒加载 `AdminProjectsPage` 与 RequireAdmin 路由、账户/侧栏入口、页面名称；提供搜索、owner/status 筛选、分页、统计和删除确认。
- [x] 从列表进入既有 `/projects/:pid`，在 ProjectLayout/Reader 持续展示所属用户并能返回管理列表。
- [x] 共用纯派生管理能力逻辑，按当前项目 ID 判断元数据；项目快速切换、刷新、登出不能串 owner。
- [x] 管理模式条件关闭 AI/智能体/向导/批量/分析、自动 task 查询与订阅、生成日志、风格私有库、导入导出入口；自己的项目继续原功能。
- [x] 概览/正文仅保存人工可管理字段；保留私有风格引用与原编辑器未保存保护、生成中禁写行为。
- [x] Reader 管理模式关闭所有阅读进度写入/keepalive，不从所有者进度恢复。
- [x] 知识库与记忆仅暴露设计允许的人工能力，不挂载索引任务轮询或向量搜索。
- [x] 补充有业务意义的纯函数/API/SSR 渲染测试，避免只测试实现常量或为了本次引入新测试依赖。

建议文件：`frontend/src/api/{admin,types}.ts`、`router.tsx`、`pages/AdminProjectsPage.tsx`、`components/AppLayout.tsx`、`pages/project/ProjectLayout.tsx`、`stores/project.ts`、内容页与相关 hooks、Reader、管理能力工具及相应测试。

## 3. 完整质量验证

- [x] 派发 `trellis-check` 审核全部任务差异，逐条映射 PRD AC1–AC9，直接修复实际问题，保留原普通用户隔离断言。
- [x] 后端（`backend/`）：`uv run ruff check .`、`uv run pytest`。
- [x] 前端（`frontend/`）：`npm run lint`、`npm run build`、`npm test`。
- [x] 构建无超过 1500 kB 的 chunk 警告，新增管理页面按需加载，质量门槛不降低。
- [x] 真实浏览器：管理员、项目所有者和第三名普通用户；查询/筛选/分页、编辑资料/正文/人工设定、取消与确认删除、直接链接/刷新与往返导航。
- [x] 网络验收：管理他人工作区不请求任务/日志/私人配置、不调用模型，Reader 无进度写入；自己的项目仍有正常功能。
- [x] UI 验收：桌面与 375px、亮/暗主题、空态/错误态，页面无横向溢出；表格按现有模式内部滚动。
- [x] 将真实命令、结果和浏览器证据记录在任务目录；未执行的检查明确注明，不宣称通过。

## 4. 规范同步与收尾

- [x] 使用 `trellis-update-spec`，同步管理员可管理项目内容的新契约、人工/严格拥有权边界及模型副作用注意点；同步前端工作区能力与 Reader 边界。
- [x] 更新 README 的管理员权限及作品管理使用说明，保留账户私有凭据隔离说明。
- [x] 已检查完整 diff、未知改动和验证结果，生成 `commit-plan.md`：后端、前端、规范与验收记录三组；并行书架改动明确排除。用户已回复“提交 归档”，批准按此方案提交与收尾。
- [ ] 经确认提交后按 `trellis-finish-work` 归档与记录会话，报告完成范围和真实验证结果。

## 风险与回退点

- 授权 helper、混合 AI/CRUD 的 API、memory 索引副作用、风格 PATCH、Reader 进度、project store 是重点审核文件。
- 若发现计划必须扩大到模型权限/私有资源、导入导出或所有者任务控制，返回规划，不因页面复用而自动纳入。
- 在每个可验证切片完成后保留干净证据；回退只覆盖本任务改动，不回退其他会话或用户变更。
- 不新增数据迁移；回退代码不能恢复管理员已保存/删除的内容，删除必须由界面确认。
