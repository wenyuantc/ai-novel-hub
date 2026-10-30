# AI 生成人物关系信息：执行计划

## 实施前门禁

- [x] 已获任务创建同意，已确认批量、只补缺失、预览编辑勾选后保存。
- [x] 已核对现有代码、完成 PRD 收敛与技术方案。
- [x] implement.jsonl / check.jsonl 已填入实际规范和研究文件，通过 task.py validate；大文件已按相关章节摘录，避免注入截断。
- [x] 已展示最新完整方案，用户随后回复“开始”，明确批准按方案实现。
- [x] 批准后运行 task.py start，状态已变为 in_progress；已加载 Phase 2.1 并派发 Trellis implement，check 将在实现完成后顺序派发。

同一功能的前后端顺序交付，不拆父子任务。主会话协调契约、需求、规范与收尾；implement/check 顺序执行，避免修改同一文件。所有派发提示以 `Active task: .trellis/tasks/10-29-ai-relationship-generation` 开头，优先 native context injection，子端拉取为后备。

## 1. 后端请求、任务与草稿

归属：Trellis implement，全部产品代码仅在激活后修改。

- [x] 先补新增 HTTP 正常流、无写入与边界测试，确认未实现时失败；用现有 FakeLLM/Gate，不调用真实模型。
- [x] entity_ai schema 增请求，task schema、EntityHandler、entity_ai_service 增 kind 与显式分支，关系路由增加创建任务入口。
- [x] 服务校验项目、2–20 个不同正 ID、count/要求、缺失关系对和 AI 配置；运行阶段重新核对角色与候选对。
- [x] 有界上下文保留全部身份；按 ID 规范化、无序对去重、已有关系识别、类型正反向匹配与无效原因。
- [x] 草稿不写角色、关系、类型、历史与 generation_logs；区分空结果和非法 JSON。
- [x] 加入 ai_generate 来源，继续统一写入与批量单事务语义，不改数据库结构。

阶段验证：test_entity_tasks.py / 新 test_relations_generate.py、test_relationships.py。后端完成但无 UI 时不宣称功能完成。

## 2. 提示词模板

归属：同一 implement，与步骤 1 按契约顺序完成。

- [x] formats.py、entities.py、registry.py、__init__.py 加格式、构建、变量、导出；新增 relations.generate system/user j2。
- [x] 锁定 JSON 规则/格式，明确选中范围、缺失关系、方向与数量，参考信息用既有信任规则。
- [x] 测空/完整上下文、覆盖生效、锁定字段与 ID 引用。
- [x] 模板数量 30 → 31，仅为新 key 加精确 legacy 排除，不降低已有覆盖。

阶段验证：test_prompt_templates.py、test_prompt_parity.py、生成上下文断言。

## 3. 前端入口、预览与保存

归属：同一 implement，依赖后端/提示词契约。

- [x] types.ts 加请求/结果/kind/source；relationships.ts 加生成调用与来源标签；tasks.ts 加标签和关系页跳转。
- [x] entityTasks.ts 集中 unknown 收窄和载荷转换，复用方向逻辑，旧解析默认来源不变。
- [x] 扩展 EntityTaskModal 的角色多选、条数、要求、配置、活动任务重接与取消，不复制生命周期代码。
- [x] RelationshipsPanel 加入口；新 RelationsGenerateResultView 按主体复用关系建议表，经结果注册表 lazy 加载。
- [x] 无效、重复、已有关系不能保存；成功后防重复提交，重开过滤已存关系；409 保留草稿供调整。
- [x] 结果组件自身广播 relationships 刷新，任务面板和页面保存均更新列表、类型和图谱。
- [x] 补解析/方向/去重/勾选过滤/source/接口路径测试，不引入 DOM 测试库或低价值镜像测试。

阶段验证：entityTasks.test.ts、relationships.test.ts、tasks.test.ts；前端 lint/build/test。

## 4. 独立检查与真实浏览器验证

归属：Trellis check，implement 完成后派发；必要修复限于本任务。

- [x] 对照 AC1–AC8 记录证据，审查 kind、prompt key、source 的所有读写方；最终独立报告见 research/check-report.md。
- [x] 后端覆盖草稿无写入、所选 ID/同名、自指、反向重复、已有关系、预览后冲突整批回滚、类型方向、权限、取消/错误/空结果。最终完整套件 1019 项通过。
- [x] 前端覆盖 unknown 收窄、只交已选有效项、编辑清 type_id、来源、重开防重复、任务跳转与刷新。全量 428 项通过，交互显示仍待浏览器验收。
- [ ] 浏览器验证多选 → 生成 → 关闭/重接 → 预览编辑勾选 → 保存 → 列表/类型/图谱更新，以及任务面板重开、无配置和失败反馈。
- [ ] 验证浅色、暗色、375px（Work 设置禁止本地浏览器，尚未完成）。
- [x] 构建确认 lazy 拆分和 1500 kB 上限：结果 2.73 kB、图谱 225.96 kB、最大 vendor-antd 1146.09 kB。
- [x] 隔离 SQLite/FakeLLM 的真实 TCP/SSE 验收记录于 research/runtime-verification.md，未调用付费模型；明确不作为浏览器/视觉验收。

## 验证命令

先跑针对性测试，再完成项目必需门禁；若新建 test_relations_generate.py，加入针对性命令。

```bash
cd backend
uv run pytest tests/test_entity_tasks.py tests/test_relationships.py tests/test_prompt_templates.py tests/test_prompt_parity.py
uv run ruff check .
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest
```

```bash
cd frontend
npm test -- src/api/entityTasks.test.ts src/api/relationships.test.ts src/api/tasks.test.ts
npm run lint
npm run build
npm test
```

规划阶段只验证任务资料：`python3 .trellis/scripts/task.py validate .trellis/tasks/10-29-ai-relationship-generation`，不将未运行产品测试标为通过。

## 规范、提交与回滚

- [x] 检查后更新 backend/story-entities.md、prompts-styles.md 与索引；同步前端组件、类型/任务契约中的实际行为。新增 backend/relationship-generation.md 记录跨层和并发事务契约。
- [x] 不降低阈值、不跳过测试、不扩大 legacy 豁免；独立检查已核对。
- [x] 提交前检查授权 diff；初始有未追踪 pelican-bicycle.html，不修改、不暂存、不提交。
- [x] 按批准三批清单完成工作提交：6a98227（后端）、c5c61e3（前端）、036af7a（规范与验收）；按用户“提交归档”指令进入 finish-work 归档和会话记录，保留未验证项。
- [ ] 无迁移，以功能提交回滚；先处理在途任务，保留已确认关系及新来源读取兼容性。

## 规划审核状态

用户已在最新完整方案摘要后回复“开始”，批准按本方案进入实现。

用户于 2026-10-30 回复“提交归档”，确认 research/commit-plan.md 的三批工作提交及后续归档。归档保留真实浏览器/主题/375px、真实 PostgreSQL 并发与模型内容质量未验证的边界，未完成项不改为通过。
