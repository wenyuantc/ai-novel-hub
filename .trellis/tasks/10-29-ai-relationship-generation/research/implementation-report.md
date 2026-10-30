# AI 关系生成实施报告

## 交付范围

已打通 `relations.generate`：关系页选 2–20 名已有角色，填写条数、要求与配置，后台生成只读草稿，按主体预览、编辑、勾选后经既有 batch 保存；来源为 `ai_generate`。

- 请求拒绝重复或非正整数 ID，count 为 1–50 的整数、默认 10，要求不超过 2000 字。创建和运行阶段都检查项目、选择、配置及缺失对；没有缺失对时不调用模型。
- 新 kind 在请求、任务注册、job、result、前端类型、标签、跳转和 lazy 结果注册中均显式处理；多选保存在 `input.request.character_ids`，不借用单角色字段。
- 身份按 ID 校验，同名角色分别处理；原选择和当前项目双重校验，生成期间删除、已有关系、无序重复、自指、未选 ID、非法字段均不可保存并有原因/警告。
- 上下文独立保留所有所选身份及已有无序对 ID；逐角色资料预算 800 字，世界观 4000 字，关系详情 6000 字，复用角色卡、组织成员和知识库检索。合法建议数不超过有效目标，预览最多 50 行。
- 生成只读，不创建角色、关系、类型、历史或 generation_logs。确认时继续走 `create_relation → resolve_type → apply_change`，反向预置类型与编辑称呼后重新匹配均复用原逻辑。
- 新结果组件复用建议表并展示双方 ID、类别和状态；重新打开查询现有关系，屏蔽已保存条目；成功后锁定按钮并自行广播 `relationships` 刷新，任务面板无需传 `onSaved`。
- 没有新增依赖或数据库迁移，没有 Git 写操作，未操作 `pelican-bicycle.html`。

## 修改文件

后端接口与任务：

- `backend/app/api/v1/relationships.py`
- `backend/app/schemas/entity_ai.py`
- `backend/app/schemas/relationship.py`
- `backend/app/schemas/task.py`
- `backend/app/models/entities.py`
- `backend/app/services/entity_ai_service.py`
- `backend/app/services/relationship_service.py`
- `backend/app/services/task_engine/handlers/entities.py`

提示词：

- `backend/app/ai/prompts/entities.py`
- `backend/app/ai/prompts/formats.py`
- `backend/app/ai/prompts/registry.py`
- `backend/app/ai/prompts/__init__.py`
- `backend/app/ai/prompts/templates/relations.generate.system.j2`（新增）
- `backend/app/ai/prompts/templates/relations.generate.user.j2`（新增）

后端测试：

- `backend/tests/test_relations_generate.py`（新增）
- `backend/tests/test_relationships.py`
- `backend/tests/test_projects_and_story.py`
- `backend/tests/test_prompt_templates.py`
- `backend/tests/test_prompt_parity.py`

前端：

- `frontend/src/api/types.ts`
- `frontend/src/api/relationships.ts`
- `frontend/src/api/entityTasks.ts`
- `frontend/src/api/tasks.ts`
- `frontend/src/api/entityTasks.test.ts`
- `frontend/src/api/relationships.test.ts`
- `frontend/src/api/tasks.test.ts`
- `frontend/src/components/entities/EntityTaskModal.tsx`
- `frontend/src/components/entities/RelationSuggestionList.tsx`
- `frontend/src/components/entities/RelationsGenerateResultView.tsx`（新增）
- `frontend/src/components/entities/taskResultViewers.ts`
- `frontend/src/pages/project/RelationshipsPanel.tsx`

## 先失败再修复的证据

1. 第一轮新增 HTTP 测试 9 项失败：`relationships/generate` 尚未实现，返回 405。实现后 9 项通过。
2. 前端先补生成结果守卫、保存换算、重开过滤和任务跳转，5 项失败（函数缺失及错误跳转）；实现后通过。再补非法原始字段，3 项失败（默认值错误恢复有效性），收紧新 kind 守卫后通过，旧解析容错默认不变。
3. 真正并发的角色对写入先用双请求 barrier 保证双方都通过查重，复现一方 201、另一方在 flush 抛唯一约束异常；另一个失败测试确认旧 `_commit` 会把无关外键错误误报为角色对冲突。
4. 新 SAVEPOINT 失败回归证明：统一入口直接 rollback 会丢失外层标题编辑。修复事务所有权后，双请求、无关完整性错误与 SAVEPOINT 三项核心回归全部通过；关系模块最终 10 项通过。
5. 模型把称呼/类别/状态输出为数字或数组时，4 项失败测试证明原始类型会被文本容错错误接受；新生成分支收紧后 4 项通过，旧解析分支不变。

## 并发与事务处理边界

`apply_change` 的 flush 只精确转换角色对唯一约束，不提交、不主动 rollback。原生 SAVEPOINT、分析、导入和智能体调用方继续拥有事务；智能体的 `TransactionSession` 仍由外层 SAVEPOINT 收口。

关系 HTTP service 的 `create_relationship` / `create_relationships` 捕获角色对业务冲突并回滚整个请求事务，`_commit` 负责 commit 阶段的回滚。SQLite 精确识别 `relationships.pair_low, relationships.pair_high` 的 UNIQUE；PostgreSQL/asyncpg 精确识别 `23505 + uq_relationships_pair_low`。其他约束错误保留原异常，不统一翻译成 409。

主会话另用真实 TCP 的两个登录客户端复测，修复后得到 `[201, 409]`，混合批次新类型/关系/历史全部回滚；见 [runtime-verification.md](./runtime-verification.md) 和 [runtime-smoke.json](./runtime-smoke.json)。

## 验证命令与结果

| 工作目录 | 实际命令 | 退出码 | 结果 | 完整日志 |
|---|---|---:|---|---|
| `backend/` | `uv run ruff check .` | 0 | `All checks passed!` | [backend-ruff.log](./backend-ruff.log) |
| `backend/` | `env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest` | 0 | 1011 项通过，1 warning，332.62s | [backend-pytest.log](./backend-pytest.log) |
| `backend/` | `env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest tests/test_migrations.py` | 0 | 20 项通过，5.33s | [backend-migrations-utf8.log](./backend-migrations-utf8.log) |
| `frontend/` | `npm run lint` | 0 | 无 lint 错误 | [frontend-lint.log](./frontend-lint.log) |
| `frontend/` | `npm run build` | 0 | TypeScript + Vite 成功 | [frontend-build.log](./frontend-build.log) |
| `frontend/` | `npm test` | 0 | 54 文件，428 项通过，4.16s | [frontend-test.log](./frontend-test.log) |

针对性验证另包括：

- 后端：`uv run pytest tests/test_relations_generate.py tests/test_entity_tasks.py tests/test_relationships.py tests/test_prompt_templates.py tests/test_prompt_parity.py tests/test_projects_and_story.py -q --tb=short`，当时 106 项通过，33.65s；后续边界与并发回归纳入上述最终全量门禁。
- 前端：`npm test -- src/api/entityTasks.test.ts src/api/relationships.test.ts src/api/tasks.test.ts`，46 项通过。
- 知识库：真实上传与索引后，关系生成能注入参考资料，关闭项目总开关后不注入；`env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest tests/test_relations_generate.py -k enabled_knowledge -q --tb=short`，1 项通过、30 项未选择，0.68s，日志为 [backend-knowledge-targeted.log](./backend-knowledge-targeted.log)。
- 模板数量已更新为 31；legacy parity 仅精确新增排除 `relations.generate`，没有放宽旧 key 的覆盖。
- 构建：`RelationsGenerateResultView` 独立 chunk 2.73 kB；`RelationGraph` 独立 JS 225.96 kB / CSS 15.41 kB；最大 `vendor-antd` 1146.09 kB，小于 1500 kB。
- Vitest 有 Node 的 `localStorage` ExperimentalWarning，测试全部通过；未调整无关运行环境或压低质量门禁。
- 后端唯一 warning 是既有 Gemini 图片 Provider 的 `generate_images` 弃用提示；本任务未修改图片 Provider，也未过滤该 warning。

首轮未指定 locale 的 `uv run pytest` 退出 1：990 项通过、20 项迁移测试失败，344.34s。全部迁移失败发生在 Alembic 用 `encoding="locale"` 读取含中文注释的 `alembic.ini` 时，当前壳层的 `LC_ALL=C` 导致 ASCII `UnicodeDecodeError`，尚未进入迁移逻辑。命令级改为 `en_US.UTF-8` 后确认 `locale.getencoding() == "UTF-8"`，20 项迁移全部通过。未修改产品、配置或迁移文件来绕过错误；原始日志保存在 [backend-pytest-initial-ascii.log](./backend-pytest-initial-ascii.log)。

## 后续核验与限制

- 主会话随后派独立 Trellis check，复核最终改动与 AC1–AC8。检查进一步修复首次自定义类型的并发唯一冲突，最终后端为 1019 项通过；上表 1011 项属于实施阶段版本，最终代码及证据以 [check-report.md](./check-report.md) 为准。
- 未进行真实浏览器、浅/暗主题和 375px 视觉交互验收：Work 设置禁用了本地浏览器使用，主会话已单独记录并询问用户；TCP/SSE 验证不算页面验证。
- 未调用付费或真实 LLM；模型内容质量依赖实际配置。生产 PostgreSQL 的真实并发未实跑，已覆盖原生 SQLite 双请求竞争与 asyncpg 约束名包装识别。

## 提交格式处理

2026-10-30 提交前的暂存检查发现部分日志和规范摘录有行尾空白或多余空行；仅清理这些空白以通过 diff 检查，保留命令、异常、断言、退出结果和测试数量，不重写或筛除失败证据。
