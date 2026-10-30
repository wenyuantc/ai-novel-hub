# Research: 管理员跨用户项目内容管理的后端边界

- Query: 如何复用现有项目内容 CRUD，同时仅向管理员授予明确的项目管理能力，不冒用所有者身份、不读取账户私有配置、不启动所有者的模型任务。
- Scope: internal
- Date: 2026-10-30

## Findings

### 1. 现有约束与建议结论

用户已选择完整管理，包括跨用户列表、删除，以及查看和修改项目资料、章节正文和设定。旧约定明确管理员不能读取小说：`backend/app/services/admin_service.py:3`、`.trellis/spec/backend/auth-admin.md:76`、`README.md:311`；这是本需求授权修改的契约，不是必须维持的产品行为。

**收敛建议：新增 `/api/v1/admin/projects` 跨用户列表，继续使用现有 `/projects/:pid` 编辑区和内容 API；保留严格 `get_owned_project` / `get_owned_chapter`，新增 `get_manageable_project(db, actor_user_id, project_id)`，只在明确的手工内容 CRUD 中使用。** helper 从数据库判断当前真实 actor 是否是启用中的管理员；普通用户只允许访问本人项目。他人项目的 AI、任务、日志和账户私有资源继续走严格所有权检查。

所有 API 继续传真实管理员的 `user.id`，不得在整个项目会话内隐式改成 owner ID。若管理输出需要上下文，定义明确字段 `actor_user_id`、`project_id`、`owner_user_id`，而不是返回一个伪装为所有者的 `User`。所有数据库查询仍位于 service；API 只负责 schemas、依赖和响应（`.trellis/spec/backend/directory-structure.md:107`）。专用管理内容路由可作为替代方案，但不是本次最小改动的首选。

### 2. 认证链与所有权检查

- `backend/app/core/deps.py:70`：`get_current_user` 根据 cookie/Bearer token 取真实用户，校验启用状态与 `token_version`。
- `backend/app/core/deps.py:84`：`get_admin_user` 在当前真实用户不是管理员时返回 403。角色每个请求读取数据库，因此管理员被降级后下一请求应立即失败，无需更新 cookie。
- `backend/app/services/project_service.py:22`：`get_owned_project` 只接受 `Project.id` 和 `Project.user_id == user_id`，统一 404。
- `backend/app/services/chapter_service.py:30`：**另有独立检查** `get_owned_chapter`，直接 join `Project.user_id`，用于只有章节 ID 的生成等操作。
- `backend/app/services/chapter_service.py:41`：项目内章节先查项目所有权，再查章节 ID 属于同一项目。
- `backend/app/services/story_service.py:76`：世界观/角色/大纲同样同时校验项目和 item 的 `project_id`。
- `backend/app/services/agent/tools/base.py:59`：智能体工具层还有独立 `Project.user_id` 条件。

只改 `get_owned_project` 无法统一所有路径，还会把该 helper 下游的生成、任务和检索意外纳入管理员权限；不建议全局把管理员视为所有者。

### 3. 可复用的内容接口分类

下表为建议的管理端内容能力白名单，复用现有路径。只将相应纯内容 service 的项目 gate 换为 manageable；同 router 内其余 AI/任务路径仍严格所有权，不能全局替换 helper。

| 类别 | 建议包含的内容能力 | 现有实现与证据 |
|---|---|---|
| 项目 | GET/PATCH/DELETE 目标项目；列表另做分页聚合 | `api/v1/projects.py:25`、`:31`、`:45`；`services/project_service.py:211`、`:235` |
| 世界观、角色、大纲基础项 | 列表、详情、创建、修改、删除 | `api/v1/story.py:23` 工厂；`services/story_service.py:46`、`:76`、`:84`、`:98`、`:110` |
| 章节 | 列表、详情、创建、修改、删除、按大纲建章、选角读取与设置 | `api/v1/chapters.py:22` 至 `:70`；`services/chapter_service.py:137`；`services/cast_service.py:141` |
| 组织、成员 | CRUD、创建并带成员、角色所属组织 | `api/v1/organizations.py:26` 至 `:41`、`:57` 至 `:100`；`services/organization_service.py:33`、`:114`、`:221` |
| 关系、关系类型与图谱 | 类型列表/创建/删除、关系列表/创建/批量创建/修改/删除、历史、图谱、角色附带关系创建 | `api/v1/relationships.py:24` 至 `:61`、`:71` 至 `:91`；`api/v1/character_actions.py:16`；`services/relationship_service.py:161`、`:434`、`:537` |
| 职业、阶段、角色职业 | 职业 CRUD/批量保存、持有人、角色分配、历史 | `api/v1/careers.py:25` 至 `:37`、`:52` 至 `:107`；`services/career_service.py:139`、`:245`、`:525` |
| 伏笔 | CRUD、统计、埋入/部分回收/完成/放弃、历史与章节提醒 | `api/v1/foreshadows.py:23` 至 `:104` |
| 大纲进阶 | 树、追加、排序、手工细纲编辑/建章、章节细纲、快照创建/读取/恢复/删除 | `api/v1/outline_pro.py:44`、`:68`、`:85`、`:109` 至 `:161`；`services/plan_service.py:44` |
| 章节版本 | 版本列表/详情、标星/备注、删除、恢复 | `api/v1/chapter_versions.py:27` 至 `:54`；`services/chapter_version_service.py:13`、`:31`、`:62` |
| 封面 | 读取已有封面、手动上传与删除 | `api/v1/project_cover.py:14`、`:24`、`:35`；`services/cover_service.py:20` 至 `:63` |
| 知识库已有内容 | 检索参数读取/修改、文档列表、注入开关、删除、已存分块读取 | `api/v1/kb.py:27` 至 `:39`、`:57` 至 `:67`、`:77` 至 `:87`；`services/kb_service.py:68`、`:90`、`:158`、`:188`、`:211` |
| 剧情记忆 | 列表、手动记忆 CRUD、钉住/屏蔽 | `services/memory_service.py:246`、`:295`、`:318`、`:346`；**不可照用普通 API 保存后的自动向量排队** |

世界观/角色/大纲工厂一套生成三组路由；继续复用原 router 和 API，仅在手工内容 service 入口采用 manageable helper。此处复用的是校验和事务业务逻辑，不能重写 ORM 简单 CRUD 跳过章节版本、关系历史、编号平移、组织循环检查等规则。

### 4. 明确不自动授予的端点

| 类型 | 原因及证据 |
|---|---|
| 章节生成/润色/改写/分析/改纲、向导、批量生成、人物/关系/组织/职业生成、智能体执行 | 通过 `task_service.create_task(..., user.id, ...)` 或同步 provider 调用，user_id 是任务所有者与模型计费身份；例如 `api/v1/chapters.py:75`、`:87`、`api/v1/outline_pro.py:73`、`:92`、`api/v1/chapter_versions.py:72`、`api/v1/analysis.py:22` |
| 所有者 AI 配置、提示词模板、私人风格库、Skill、MCP、API Keys | 属于账户级资源；`services/ai_config_service.py:65`、`:74`、`services/style_service.py:90`、`services/skill_service.py:47`、`services/agent/service.py:191` 直接按账户身份过滤 |
| 知识库上传/重建/重索引、向量搜索/调试、章节上下文预览 | 不全是普通 CRUD，可能解析任务、读取向量配置或调用真实 provider；`services/kb_service.py:148`、`:207`、`:264`、`:265`；`api/v1/memories.py:95` |
| 智能体会话、运行中任务及其重试/继续/取消 | 同时由 project_id 和 user_id 限定；运行上下文可能含私有 Skill/MCP 参数。`services/agent/service.py:93` 至 `:147`、`services/task_service.py:220`、`:249` |
| 生成日志全文 | `schemas/chapter_version.py:74` 返回完整 `messages`，其中可能已有私有模板/风格/Skill 的展开快照；不能因版本页复用就自动开放 |
| 全项目备份、导入、卡片导入导出、大纲导入导出、阅读进度 | 不属于本次已明确的手工管理核心；应在最终方案中列为延后或明确允许项，而不能从 router 复用顺带放开；阅读进度写入会改所有者进度 |

界面可以允许查看小说正文而无需开放阅读进度接口；例如管理端章节详情已经包含正文。

### 5. user_id 的非权限语义与隐藏副作用

#### 5.1 不能更换认证用户为所有者

普通 API 中 `user.id` 同时承载四种含义：真实请求身份、项目所有权、私有资源命名空间、后台任务归属与模型配置选择。禁止通过全局 `app.dependency_overrides[get_current_user]`、临时令牌或 clone `User.id` 等方式“切换用户”。这会越过管理员原本没有的账户权限，并且全局 override 还存在并发污染。

本次推荐方案始终传真实 `actor_user_id`；`get_manageable_project` 在 service 内判断项目归属或管理员权限，不更改当前请求人的 ID。owner 字段只用于归属显示、判断是否跨用户管理及阻止模型副作用。不得提供任意函数名/任意路径代理，不能允许前端通过 header/body 自定 owner ID。

#### 5.2 项目和章节的风格引用

- `services/project_service.py:217`：项目修改 `default_style_id` 按传入 user_id 调用 `ensure_assignable`。
- `services/chapter_service.py:155`：章节 `style_id` 也按传入 user_id 校验。
- `services/style_service.py:101`：只允许预设或该用户所有的风格。
- `services/project_service.py:228` → `style_service.apply_legacy_style`：废弃的 `style` 文本字段会创建/修改所有者账户的私人风格；管理员 API 应显式拒绝/排除该兼容字段。

**最小方案建议（尚不是已确认的用户需求）：跨用户管理时仅只读显示当前风格引用，后端 PATCH 明确拒绝修改 `style`、`default_style_id`、章节 `style_id`。** 这样不需要访问所有者或管理员的私人风格库，也不会误把管理员私人风格绑定到他人项目；未包含这些字段的项目资料/正文保存维持现有引用。前端进入跨用户管理模式时，不加载 `StyleSelect` 的私人风格列表，不把当前只读值原样带回 PATCH；后端不能只依赖隐藏控件。管理员编辑自己项目的既有风格操作保持原状。

如未来要求管理员调整风格引用，应作为单独明确范围设计，按目标项目所有者命名空间验证，并定义可见的引用候选，不能复用管理员私人风格列表。

#### 5.3 剧情记忆保存会自动消耗所有者向量模型

- `api/v1/memories.py:59` 定义 `_enqueue_vectors`；`:68`/`:84` 在创建/修改 service 成功后调用它。
- `services/kb_index_service.py:98` 的 `enqueue_memory` 按 `project.user_id` 读取所有者 embedding 模型（`:108`），并创建 `Task(user_id=project.user_id, kind="kb.index")`（`:125`）。
- 所以“只修改手工记忆”若原样复用普通路由，仍会自动排队消费他人模型。在真实 actor 不是 owner 时，普通 memories API 保存必须跳过 `_enqueue_vectors`；不能让前端承担这个安全边界。service 本身会更新关键词检索块并清除/失效旧向量，见 `memory_service.py:312`、`:340`。
- 原有自动记忆限制继续生效：`memory_service.py:324` 不允许修改自动记忆内容，`:348` 不允许删除自动记忆；管理员可按原有方式钉住或屏蔽，不能直接绕过规则修改全部 ORM 字段。

#### 5.4 普通章节保存必须复用业务 service

`chapter_service.py:142` 检查生成中状态；`:155` 校验风格；`:161` 同步剧情记忆编号；`:169` 计算非空白字数；`:174` 写手工版本；`:175` 将版本唯一约束竞争转换为 409。删除章节先删自动记忆和检索单元（`:180`）。这些都不能被管理端直接 UPDATE/DELETE 绕过。

**人工保存本身不会自动分析。** 自动分析发生在 `services/task_engine/handlers/chapter.py:150` 的 `on_success`，其 `analysis_service.enqueue_auto` 在 `analysis_service.py:583` 至 `:615` 中直接创建 `user_id=project.user_id` 的 task。因此只开放手工 CRUD 并严格阻止跨用户生成，不需要从人工章节保存中删除任何自动分析逻辑；要用测试防止未来引入这类副作用。

#### 5.5 生成日志和备份不是纯小说正文

生成日志 `messages` 为完整提示词快照（`generation_log_service.py:18` 至 `:55`；`schemas/chapter_version.py:74`）。项目备份注册表也原样保留日志 params/messages（`services/project_transfer/registry.py:241`）。若声明账户私有模板不开放，则需要同时阻止这些间接读取路径，或明确将其纳入后续授权范围。

### 6. 三种实现方式比较

| 方案 | 真实成本与收益 | 风险/评价 |
|---|---|---|
| 普通 router 整体复制到 `/admin` 并换 user | 表面代码少 | 同一 router 经常同时包含 CRUD 和 AI；记忆 CRUD 甚至暗中排队；可能读所有者配置。**不建议** |
| 新 manageable helper + 内容服务显式使用，复用原页面/原路径 | 无需复制几十个路由和前端 API/导航；真实 actor ID 始终不变 | **本次首选**；必须逐个审计混合调用方，并保护 memory 的自动索引 |
| 独立管理 route + 纯内容 service 适配 | 需要约数十个内容端点和有选择的工厂复用；认证和接口清单容易审计；普通 API 无行为变更 | 可行替代；只能在窄适配内部使用已验证的 owner scope，不能认证换人 |
| 全部 service 改为统一 `ProjectAccess`/`Actor` 参数 | 从根本区分 actor/owner，对未来协作编辑有价值 | 本次会广泛触及所有权 helper、独立 chapter 检查、任务验证、工具层、私有资源引用；修改范围远大于新增 manageable helper |

`story._make_router` 是真实既有复用点（`api/v1/story.py:23`）；本次继续使用原三组 CRUD 即可。实现不需要动态复制 APIRoute、替换认证依赖或构建通过字符串调用服务函数的运行时网关。

#### 最小 helper 落点与需保留的严格 gate

- `project_service` 新增 manageable 项目读取；只让项目内容 GET/PATCH/DELETE 使用。`list_projects` 仍只列本人项目，跨用户列表走 `/admin/projects`；`create_project` 不代他人创建。
- `chapter_service` 新增 `get_manageable_chapter(db, actor_id, project_id, chapter_id)`，其内部验证 manageable 项目以及 Chapter.project_id 匹配。人工 list/create/update/delete、cast、版本服务使用它。**保留原 `get_owned_chapter` 与 `get_project_chapter` 严格**：后者还被生成日志读取和 rewrite apply 使用（`generation_log_service.py:87`、`:97`；`rewrite_service.py:214`）。
- `story_service` 可新增 manageable item helper 给内容 CRUD，保留 `get_owned` 用于 AI；`entity_ai_service.py:511` 原本先严格检查 project，再在 `:521` 使用 `story_service.get_owned`，应继续保持两层边界。
- organizations/relationships/careers/foreshadows/outline/plan/snapshot 的手工入口按表显式选择 manageable；AI 服务的创建校验和 build_job 继续严格检查。
- `kb_service.get_project_document` 同时给 CRUD 与重索引用（`:159`、`:189`、`:200`）。不要在不审计调用方的情况下改宽它；可新增 manageable document helper 专供读取/注入开关/删除，重索引保留严格 helper。
- `cover_service.get_cover/put_cover/delete_cover` 可 manageable；`prepare_generate`（`:81`）继续严格归属并使用真实请求人配置。封面 lock helper 若共享，需要将人工路径和 AI 路径的认证分开。
- memories 的内容 service 可 manageable，但搜索仍 `get_owned_project`；路由只为 `actor_id == project.user_id` 的成功保存 enqueue 向量任务。
- 若普通内容 `ProjectOut` 新增 owner 元数据，只有管理员访问时输出；前端据此识别“正在管理他人项目”，不能把“管理员身份”本身当成管理模式，因为管理员编辑本人项目时应保留自己的全部 AI 功能。

### 7. 列表和删除的实现基础

- 列表 service：Project join User、章节聚合子查询，一次分页主查询加一次 count；现有 `_stats_subquery` 已展示正确的章节数与字数聚合方式（`project_service.py:46`）。不要对每一行分别执行 `project_out`，后者有数次查询且包含阅读状态/私人默认风格摘要。
- 查询建议：q 按 title/username 子串匹配；按 owner ID、项目状态过滤；`page>=1`、`page_size<=100`；稳定排序 `updated_at DESC,id DESC`；返回 `{items,total}`，每项带 `owner_user_id`、`owner_username`、项目状态、章节数、总字数和时间。
- `%`、`_` 和反斜杠必须转义为字面量，复用 `admin_service.py:45` 的 `_escape_like` 约定，避免搜索通配符放大结果。
- 删除可复用 `project_service.delete_project`（`:235`），但应在管理授权后调用；数据库项目关联表普遍 ON DELETE CASCADE，包括 Task（`models/entities.py:591`）。不应删除账户资源（私有风格、AI 配置等）。
- 正在运行的 task 可能已取出输入/发起模型请求，删除数据本身不等价于立即撤销外部请求；不要承诺删除即立即停止一切模型费用。现有任务契约已经处理任务行被级联删除后的订阅断开，本次可保持原语义。

### 8. 测试验收建议

1. 新管理列表覆盖未登录 401、普通用户 403、管理员可访问；内容 API 对普通用户他人资源仍 404，对管理员开放白名单内容。降级/禁用/令牌失效后立即拒绝，AI/私有资源 API 始终严格所有权。
2. 管理员跨两个普通用户项目列出正确的 owner 和章节统计；空项目统计 0；q 中 `%`、`_`、反斜杠按字面量；状态/所属用户组合过滤、分页与稳定排序。
3. 管理员修改其他用户项目 title/status、章节正文/章节号、世界观、人物、组织成员、关系、职业、伏笔和细纲等；所有者通过原有 API 立即看到结果。
4. 项目 A 的路径内不能读取/更新/删除项目 B 的 item_id；关系/成员/选角/细纲中混入其他项目 ID 继续得到既有 400/404。
5. 章节修改后字数正确、版本记录存在；生成中章节仍受 409 保护；删除章节执行自动记忆/检索清理，删除项目只级联目标项目。
6. 手工记忆管理测试设置所有者 embedding 模型并监测 Fake provider，断言保存后关键词块更新、没有新增所有者 kb.index 任务，也没有 provider 调用。
7. 管理员跨用户无法触发 AI/任务/日志/私有配置端点；不能通过 owner ID/body/header 参数绕过真实认证。跨用户平台 API Key、AI Config、Skill、MCP 原有隔离回归通过。
8. 如采纳只读风格引用建议：跨用户 PATCH 携带 `style/default_style_id/style_id` 返回明确 400；未带这些字段的资料/正文保存维持原引用；管理员编辑自己项目的风格操作维持原行为。

建议新建 `backend/tests/test_admin_projects.py`，复用 `test_admin.py` 管理员 fixture 和 `tests/conftest.py` 的独立 SQLite、FakeLLM、fake_dns。实施后执行 `cd backend && uv run ruff check . && uv run pytest`；本轮仅研究，未执行测试。

## Related Specs

- `.trellis/spec/backend/auth-admin.md`：真实管理员身份、角色变更即时生效；需补充新的跨用户项目管理契约。
- `.trellis/spec/backend/directory-structure.md`：路由不得 ORM、内容规则在 service。
- `.trellis/spec/backend/database-guidelines.md`：所有权/子资源校验、级联、章节版本事务及平移登记。
- `.trellis/spec/backend/quality-guidelines.md`：项目级检查、隔离测试、不可真实请求模型。
- `.trellis/spec/backend/story-entities.md`、`outline.md`、`relationship-generation.md`：复杂设定写入的唯一业务入口。
- `.trellis/spec/backend/retrieval.md`：记忆写入会在普通路由 enqueue 向量任务，管理端需要明确的例外。
- `.trellis/spec/backend/project-cover.md`：封面上传、校验与版本输出。
- `.trellis/spec/backend/prompts-styles.md`：引用作用域及废弃 style 字段的兼容副作用。

## External References

本轮不引入新依赖或框架用法，全部结论以当前仓库源码和规范为依据，无外部检索。

## Caveats / Not Found

- 用户“完整管理”已覆盖项目资料、正文、设定的读写，不等于自动授予模型计费、全账户私有配置或运行时任务控制。最终规划应向用户明确展示这些边界。
- 上表覆盖现有可见模块，但不是新 API 的承诺清单；最终设计应逐项选定复用页面所需端点并写入实现计划，不能让未选端点在路由复制中意外出现。
- 项目管理本身不需要新增数据表/迁移；若后续另提审计日志、转移所有权或乐观锁，则需独立设计，不应悄悄混入。
