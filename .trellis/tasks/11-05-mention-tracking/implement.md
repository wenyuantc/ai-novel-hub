# 执行计划
## 阶段与依赖
用户已明确授权计划完成后直接实施。主会话先展示方案供审阅，再启动以下子任务；父任务负责集成验收，不直接替代子任务。
- [x] N：11-05-project-notes 提供稳定 ProjectNote、CRUD、CAS、备份和笔记页。
- [x] M-B：11-05-mention-index-navigation 后端实现词典/匹配/四来源索引/API/别名兼容。
- [x] M-F：同子任务前端实现编辑/阅读标记、详情反向提及、四来源深链、热力图。
N 与 M 的纯匹配/独立组件可并行；M 的完整笔记测试等待 N 的模型/API就绪。0033 迁移必须依赖0032。

## 文件责任
- N 工作者：backend models/notes.py、schemas/notes.py、services/note_service.py、api/v1/notes.py、0032、tests/test_notes.py；frontend api/notes.ts（类型也在本文件）、pages/project/NotesPanel.tsx、components/notes/**。先完成共享注册 models/__init__.py、api/v1/router.py（以实际路由聚合文件为准）、project_transfer registry/exporter 与必要备份测试，然后通知 M-B 接手共享注册。不编辑 frontend router/ProjectLayout/Sidebar，交 M-F 集成。
- M-B 工作者：上述 N 所属文件之外的提及后端、WorldSetting 别名、0033、后端提及/迁移/兼容测试；待 N 注册结束后接手共享模型/API/备份注册。
- M-F 工作者：除 N 所属前端文件外的全部提及前端及路由/导航集成；NotesPanel 深链接口按 design 与 N 协商，不与 N 同时改文件。
- M-S 工作者：从 M-F 拆出独立统计展示，拥有 pages/project/MentionsPanel.tsx、components/mentions/MentionHeatmap.tsx、utils/mentionStats.ts 及其测试；M-F 保留 API/共享组件与路由菜单。统计页默认导出；使用 M-F 的 EntityMentions({projectId,entity?,chapterFrom?,chapterTo?}) 显示格子命中，不另建导航协议。
- 所有工作者不得撤销他人修改；发现接口冲突即时报告主会话。精确文件落点可按现有架构调整，但职责、API与用户范围不变。

## 实现顺序与验证
1. N：先写 CRUD/权限/CAS/备份失败用例，实现后定向 pytest 与前端笔记测试。M-B 先用纯匹配测试定义重叠、歧义、Unicode；M-F 按已定 API 做独立工具/组件与测试。
2. N 模型可用后，M-B 完成四源查询、预览、SourceWindow、summary；覆盖生成/恢复/删源/改名后的重新查询以及管理员聊天过滤。
3. M-F 接通详情、Reader、编辑器、聊天、知识库、笔记与统计页；保持草稿/异步代次/主题和只读契约。
4. [x] 每个工作者完成自身 lint/typecheck 与定向测试，记录命令结果。
5. [x] trellis-check 全范围审查父/子 PRD、design、implement 和全部变更，直接修复问题；不得再次派 implement/check 套娃。
6. [x] backend：uv run ruff check .；uv run pytest。重点迁移/项目备份/设定变化/agent与权限回归必须通过。
7. [x] frontend：npm run lint；npm run build；npm test。
8. [x] 真浏览器：本地测试账号/隔离作品创建角色、地点/物品别名、四来源样例；验证草稿预览、精确定位、重名、热力图、改稿后旧链接；桌面/窄屏/暗色/键盘。检查控制台与请求。
9. [x] 有界性能样例：至少500万码点正文及多文档资料，记录实体数/来源数/冷热耗时/缓存边界；不能删除必要断言或跳过失败测试。
10. [x] 更新 .trellis/spec 对应契约与 TODOS 第2项（第9项只勾已实交付）；记录 implementation-log 和 browser-verification。
11. [x] Phase 3.4 提交清单已获确认；用户明确授权三批提交、归档父/子任务、记录 journal、合并到 main 并推送。执行结果由归档后的 task.json 和开发者 journal 记录。

## 回退与停止条件
接口或匹配口径需改变时先更新design并通知所有消费方；不得静默删去四来源之一。新功能入口可回退，原文不因追踪改写。涉及真实笔记数据的数据库降级不得自动执行。
