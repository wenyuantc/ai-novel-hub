# 关系 AI 生成接入调研

规划时仓库事实，供 implement/check 注入。需求以 PRD 为准，接口与顺序见 design.md / implement.md。

## 入口与后端

| 文件 | 复用点与约束 |
|---|---|
| frontend/src/pages/project/RelationshipsPanel.tsx | 没有 AI 入口；已有关系/角色/类型 loader 订阅 relationships，图谱 lazy。 |
| backend/app/api/v1/relationships.py | CRUD/batch/history/graph；新路由只调用 task_service，不查 ORM。 |
| backend/app/services/relationship_service.py:183 | find_pair 用 min/max 无序对，反向必须去重。 |
| backend/app/services/relationship_service.py:188 | apply_change 是唯一写入，校验角色/类型/数值/状态/重复并记历史。 |
| backend/app/services/relationship_service.py:74 | find_type 预置优先且返回 flipped；resolve_type 会创建类型，生成阶段不可调用。 |
| backend/app/services/relationship_service.py:427 | 批量单事务，任一失败全部回滚。 |
| backend/app/services/entity_ai_service.py:286 | 旧建议按名字/别名解析；新功能两端按所选 ID，不能套名字匹配而让同名角色歧义。 |
| backend/app/services/entity_ai_service.py:390 | 创建前查项目/配置；新 kind 需查整个选择集合与缺失关系对。 |
| backend/app/services/entity_ai_service.py:467 | job 默认 else 是 organization，新增 kind 必须显式分支；已有作品/世界观/知识库路径。 |
| backend/app/services/entity_ai_service.py:568 | result 最后默认旧解析，新增 kind 必须显式分支，只读产草稿。 |
| backend/app/services/task_engine/handlers/entities.py | _REQUESTS/kinds/validate_and_claim 要同步，多选请求持久化到 input.request。 |
| backend/app/schemas/task.py:9 | TaskKind Literal 与前端联合/标签/跳转/结果注册同步。 |

## 前端草稿

- EntityTaskModal 已有创建、track/follow、进度、输出、取消与活动任务重接，扩展而不复制。
- RelationSuggestionList 以 subjectName 显示目标，编辑称呼/亲密度/描述，支持无效原因和勾选；新结果按主体分组复用，类别已有显示，状态应在新预览可见。
- api/entityTasks.ts 的 suggestionTypeRef 处理编辑清 type_id 与方向；suggestionsToBatch 目前写死 ai_parse，加来源参数时保留旧默认。
- TASK_RESULT_VIEWERS 使用 React.lazy。TaskResultModal 不传 onSaved，新结果必须自行 invalidateProject。
- utils/projectRefresh.ts 提供 relationships 广播；client.ts 不会在所有写请求后自动广播。
- saved 只是组件本地状态，重开同一结果时必须查现有关系过滤，不当作持久化幂等标记。
- 所有传输类型放 api/types.ts，unknown result 在 api/entityTasks.ts 集中收窄。

## 上下文、模板与数据

- context_builder.format_card 可格式化结构化卡片和组织成员；organization_service.memberships_of、relationship_service.relations_among 可批量查。
- story_service.format_roster 只有名字/定位/别名，不足以据性格背景构思；需详细卡片和单独稳定 ID 清单。
- ai/prompts/entities.py 的纯 build 函数交给 registry.render；格式在 formats.py，正文在 templates/*.j2。
- registry 现有 30 个 key，test_prompt_templates.py:47 固定计数，新增后同步 31。
- test_prompt_parity.py:325 比较旧 key 覆盖集合；新 key 无 legacy，只增精确 key 排除及独立测试，不能扩大前缀豁免。
- 新 key relations.generate 与 relations.parse 分开，锁定 json_rule/output_format，沿用模板覆盖/预览。
- models/entities.py:88 的来源是业务枚举，RelationshipHistory.source 存 String(16)，无来源数据库枚举约束。新增 ai_generate 同步 API 枚举/前端标签，无迁移。
- schemas/relationship.py 当前公开来源仅 manual/ai_parse，不能只改前端。
- 草稿不写类型/关系/历史/generation_logs；确认才调用 batch 并由统一入口处理方向/历史。
- 最多 20 角色对应 190 个角色对，小于现有 batch 最大 200；期望条数 1–50 另约束模型输出和预览规模。
- 初始用户未追踪文件 pelican-bicycle.html 不在本任务范围。

## 验证依据

- 后端 test_entity_tasks.py 已有 FakeLLM、草稿无写入、方向、取消、越权样例；test_relationships.py 已有无序唯一对、批量事务和历史来源。
- 前端 entityTasks.test.ts 测守卫/保存载荷，relationships.test.ts 测方向，tasks.test.ts 测跳转等。
- 后端门禁 uv run ruff check . / uv run pytest；前端 npm run lint / npm run build / npm test。
- Vitest 为 node 环境，无组件 DOM 测试库；纯逻辑用 stub fetch，交互另做真实浏览器验证。
