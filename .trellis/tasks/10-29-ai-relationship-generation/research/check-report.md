# AI 人物关系生成：独立检查报告

## 范围与结论

检查任务：`10-29-ai-relationship-generation`。已读取完整 native hook 保存文件、PRD、design、implement、check.jsonl、两层规范索引与质量规范，执行 `get_context.py --mode packages`，确认检查范围为 `backend/` 和 `frontend/`。

已审查本任务全部产品差异和新增文件，沿着请求 → 后台任务 → 提示词 → 只读规范化 → unknown 收窄 → 编辑勾选 → batch → 统一关系写入/历史 → 页面刷新检查真实调用路径。未修改 Git 状态、分支、提交或用户的 `pelican-bicycle.html`。

发现并修复 1 项确定性并发缺口；最终后端 Ruff 和 1019 项完整测试通过（335.12s，1 条既有 warning）。前端未在检查阶段修改，独立 lint、类型检查和 54 文件/428 项全量测试通过。浏览器交互与视觉验收受 Work 设置限制，不能标为通过。

## Findings (fixed)

### 自定义类型首次创建竞争仍返回 500

- File：`backend/app/services/relationship_service.py`、`backend/tests/test_relationships.py`。
- Issue：两个请求均查到自定义称呼不存在，随后保存同一缺失角色对时，`resolve_type` 的首次类型 flush 先触发 `(project_id, role_a, role_b)` 唯一约束；原有角色对 guard 尚未执行，异常外露为 500。此前“朋友”预置类型的并发测试不能覆盖此路径。
- RED：[独立复现脚本](./check_concurrent_custom_type.py) 和 [原始异常日志](./check-concurrent-custom-type.log) 得到一方 201、另一方 `relationship_types` 的 IntegrityError；补入正式测试后，[RED 日志](./check-type-conflict-red.log) 为 6 失败、1 通过。
- Fix：仅将此类型唯一约束精确转换为中文 `409 关系类型已被其他请求创建，请重试保存`。SQLite 同时检查 UNIQUE 错误码和三列完整消息；PostgreSQL 同时检查 SQLSTATE `23505` 和约束名 `uq_relationship_types_project_id`，该名称已与迁移 `0007` 核对。其他 IntegrityError 原样抛出。
- 事务：`resolve_type` 不 rollback、不 commit、不新增 SAVEPOINT；HTTP create/batch 的现有 ConflictError 边界回滚整个请求。外层 SAVEPOINT/智能体 TransactionSession 保留事务所有权。未引入 upsert、自动重试或迁移。
- 语义：同一角色对竞争为 201/409，关系、类型和历史只有一份；不同角色对首次共用同一新称呼时允许一方 409，作者再次保存即可复用已建类型。
- GREEN：[最终定向日志](./check-type-conflict-green.log) 为 49 项通过，覆盖同/不同角色对竞争及重试、整批回滚先前新类型/关系/历史、无关约束不误报、外层项目编辑不被回滚、PG 精确约束名与 SQLSTATE。主会话随后以最终代码重启隔离服务，最新 TCP 结构化日志同样确认这两组自定义类型竞争及重试通过。

本轮检查新增产品修改仅上述 service 和测试两处；其余产品文件未改动。

## Findings (not fixed)

没有保留已确认但未修复的产品代码问题。

以下属于尚未完成的验证，不作通过声明：

- Work 工具明确返回 `Local browser use is disabled in Work settings`。未绕过设置，未进行多选、预览编辑/勾选、任务面板重开、主题切换或 375px 的真实浏览器验收。静态审查、TCP/SSE 与构建不能替代这些验收。
- 没有运行真实 PostgreSQL 服务器并发；其约束翻译由精确包装形态测试、迁移名称和本地 SQLAlchemy asyncpg 适配器源码核对支持。SQLite 的并发测试实际执行 SQL 和独立请求事务。
- 未调用付费或真实 LLM，创作质量未评估；任务与错误路径使用隔离 FakeLLM/Gate。

## AC1–AC8 证据映射

| 验收 | 代码与自动验证证据 | 结果与边界 |
|---|---|---|
| AC1：选择、限制、无缺失对 | `RelationsGenerateRequest` 拒绝非正/布尔/重复 ID、数量/条数/要求超限；`_selected_for_generation` 在创建和运行时限定项目重查；`test_generate_invalid_selection`、`test_generate_requires_missing_pairs_and_ai`、`test_queued_task_rechecks_missing_pairs_without_model_call`；TCP 的拒绝场景 | 通过接口/逻辑验证；表单交互待浏览器 |
| AC2：ID、字段、完整上下文及列表/图谱 | 身份清单与逐名 800 字卡片独立保留，世界观 4000、关系详情 6000；全部已有无序对 ID 不裁；`test_generate_preview_then_confirm_selected`、`test_count_is_capped_and_all_20_identities_survive_long_cards`、`test_generate_uses_enabled_knowledge_and_honors_project_switch`；保存后图谱 API 边数与视角断言 | 通过；20 人长卡片与同名 ID、知识库开关有实证；页面显示待浏览器 |
| AC3：已有、自指、越界/删除、重复与保存竞争 | `generated_relation_suggestions` 同时检查原选择和当前项目，按 min/max 去重；invalid JSON 字段保持不可保存；`test_generate_marks_duplicate_self_unselected_existing_and_invalid_fields`、`test_generation_rechecks_current_data_after_llm`、`test_preview_conflict_rolls_back_new_types_and_entire_batch`；角色对并发与本轮类型并发回归 | 通过。pair 与 type 冲突分别精确转 409，混合批次完整回滚，无覆盖 |
| AC4：勾选 2/3、编辑、方向及来源 | `generatedSuggestionsToBatch` 仅取所选 ID、勾选且字段有效项，再按无序对去重；`suggestionTypeRef` 编辑称呼清 type_id 并以主体重新匹配；API 单测 + HTTP 预览确认测试；TCP 的 2/3、描述编辑、师徒反向、自定义类型、历史 `ai_generate` | 通过载荷/HTTP 验证；实际点击编辑勾选待浏览器 |
| AC5：草稿零设定写入 | 新分支仅 `find_type`，不调用 `resolve_type/create_relation`；`run_entity_job` 不 commit 设定、不写 generation_logs；正常、无效、空、错误、取消测试均比较 Character/Relationship/Type/History/GenerationLog 数量；TCP 草稿计数一致 | 通过。组件关闭无保存调用，卸载仅断订阅；真实关闭动作待浏览器 |
| AC6：防重复、重开、刷新 | `saved` 成功锁定当前提交；Editor 挂载读取已有关系并按无序对屏蔽；409 只 reload、不清 list 编辑；`filterExistingGeneratedSuggestions` 单测；保存自行 `invalidateProject(['relationships'])`，关系列表/类型、角色页与图谱 loader 均订阅此资源 | 通过静态与纯逻辑；任务面板重开/广播后的视觉更新待浏览器 |
| AC7：后台恢复、取消、错误与空结果 | 新 kind 显式注册，原请求持久化；活动查询按项目/kind 取最新，关闭时 useTaskStream 仅 detach；已结束任务通过快照+done 取得同一结果；Gate 取消/重试、格式失败、模型异常、合法空数组测试；TCP 断开仍 running、重接 done | 通过任务/HTTP/SSE 验证；组件操作与错误提示视觉待浏览器 |
| AC8：权限与兼容 | 项目、选中角色、配置、任务均按归属校验；`test_generate_project_character_config_and_task_permissions`、扩展跨用户路由矩阵；旧 entity_tasks、relationships、cards、agent_tools、writeback、wizard 模块在完整套件中回归 | 创建与读取的权限定向已通过；最终共享入口完整回归见门禁结果 |

## 关键契约核对

- `relations.generate` 已覆盖后端 TaskKind/TASK_KINDS、handler 请求映射、validate/build/result 分支，及前端 kind、标签、跳转、lazy 结果注册；多选存 `input.request.character_ids`，`TaskOut.character_id` 保持 null。
- `relations.generate` 与 `relations.parse` 模板分离，正文在 j2；json_rule/output_format 锁定。模板数 31，legacy parity 只精确排除新 key。覆盖与锁定预览有独立测试。
- 新结果在 `api/entityTasks.ts` 集中从 unknown 收窄；原始 ID、类别/状态/亲密度及称呼不被默认值恢复成可保存行；后端保存仍为最终项目/类型/角色/唯一性校验。
- `ai_generate` 同步公开可写来源、后端业务枚举、前端联合和“AI 生成”标签；历史字段为字符串，无迁移需求。旧解析默认 `ai_parse` 保留。
- 所有新关系仍经 `create_relation → resolve_type → apply_change`；角色对及类型唯一错误各自精确处理，其他完整性错误不误报。已核对卡片导入、向导、章节回写和智能体的调用/事务边界。
- 新结果只经 React.lazy 注册；图谱依赖仅在 RelationGraph 导入。已读取完整构建日志，结果 chunk 2.73 kB、图谱 JS 225.96 kB/CSS 15.41 kB、最大 vendor-antd 1146.09 kB，小于 1500 kB。实际构建目录 `react-flow__` 仅出现在 RelationGraph JS。
- 静态布局使用 AntD、局部表格横向滚动、可换行工具栏与容器 minWidth:0；新增控件有标签/aria-label，未写死颜色、未直接读取 window.innerWidth。主题/375px 仍需要浏览器确认。

## Verification

独立执行的命令及退出码：

| 工作目录 | 命令 | 退出码 / 结果 | 日志 |
|---|---|---|---|
| 根目录 | `python3 .trellis/scripts/get_context.py --mode packages` | 0；backend/frontend | 工具原始输出 |
| backend | `env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run ruff check .` | 0；All checks passed | [Ruff](./check-backend-ruff.log) |
| frontend | `npm run lint` | 0 | [ESLint](./check-frontend-lint.log) |
| frontend | `npx tsc -b --pretty false` | 0 | [TypeScript](./check-frontend-typecheck.log)（成功为空） |
| frontend | `npm test` | 0；54 文件/428 项，3.06s | [Vitest](./check-frontend-test.log) |
| backend | `env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest tests/test_relations_generate.py tests/test_relationships.py -q --tb=short`（修复前） | 0；41 项，17.83s | [初次定向](./check-backend-targeted.log) |
| backend | `env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest tests/test_relationships.py -k 'concurrent_custom_type or type_flush_conflict or resolve_type_preserves or postgres_type_conflict' -q --tb=short`（RED） | 1；6 失败/1 通过 | [RED](./check-type-conflict-red.log) |
| backend | `env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest tests/test_relationships.py tests/test_relations_generate.py -q --tb=short`（最终修复） | 0；49 项，20.14s | [GREEN](./check-type-conflict-green.log) |
| backend | `env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest`（最终修复） | 0；1019 项，335.12s，1 条既有 warning | [完整后端](./check-backend-pytest.log) |
| 根目录 | `git diff --check` | 0 | 工具原始输出 |

另已读取实施版本的完整门禁原始日志：[backend-pytest.log](./backend-pytest.log) 1011 项通过、[frontend-build.log](./frontend-build.log) 构建成功、[frontend-test.log](./frontend-test.log) 428 项通过。这些是修复前代码的实证；新修复以本报告独立运行的最终后端日志为准，未变化的前端仍由原构建与独立前端门禁覆盖。

后端未配置独立静态类型检查器，不能把 Ruff 说成 Python TypeCheck。前端 TypeCheck 已单独运行。没有调高包体积限制、扩大 parity 豁免、过滤 warning、删除断言或跳过测试。

最终后端唯一 warning 来自既有 Gemini 图片 Provider 的 `generate_images` 弃用提示，与本任务无关且未屏蔽；独立前端测试保留 Node 的 localStorage ExperimentalWarning。共享调用方的卡片导入、向导、智能体工具/提案、章节分析/回写、项目导入导出和全部迁移均被这次最终后端完整套件覆盖。

已核对更新后的 [runtime-verification.md](./runtime-verification.md) 与 [runtime-smoke.json](./runtime-smoke.json)，时间为 `2026-10-29T13:41:32.978791+00:00`：主会话在最终产品代码下重启隔离 SQLite/FakeLLM 服务，通过真实 TCP/SSE 复测拒绝输入、权限、断开/重接、草稿零写入、选择 2/3、编辑描述、视角、来源、图谱、重复保存、已有类型并发 201/409 和混合回滚。新增自定义类型同一角色对竞争为 201/409 且仅一关系/类型/历史；不同角色对共用新称呼为 201/409，409 方重试成功后共用一类型、各有一关系。最终计数为 6 角色、8 关系、23 类型、8 历史、0 generation_logs。此证据覆盖最终修复的运行行为，仍不作为浏览器验收。

## 规范同步交接

主会话负责同步 `.trellis/spec/`：生成任务/只读结果、角色选择/上下文预算、31 个模板与精确 parity 排除、ai_generate 来源、结果重开/自行广播，以及本轮“首次类型竞争返回独立中文 409，低层不回滚、请求/外层事务负责整批回滚”的明确语义。检查代理未编辑主会话拥有的规范或需求文件。

### 最终规范文字核对：通过

主会话已完成上述规范同步。检查代理对新增 `backend/relationship-generation.md` 七段跨层契约、前后端索引及 story-entities、prompts-styles、error-handling、quality、component-guidelines、type-safety 的相关新增段做过一次文字一致性核对，并确认新规范已加入 implement/check.jsonl。两处文字偏差随后由主会话修正，本次仅复核这两行：上下文限制已明确为各段裁剪预算、超限另附省略号；前端 TaskKind 表已包含 agent.turn/book.import，与后端完整联合一致。

最终规范与已审产品代码、此报告一致，没有未处理的规范文字问题。本轮仅追加此报告，未修改产品或主会话规范，未重复运行测试；最终门禁仍为后端 1019 项、前端 428 项通过。浏览器交互/浅暗主题/375px、真实 PostgreSQL 并发及真实模型创作质量仍未验证，以上边界保持不变。
