# 实施计划

以 `prd.md` 与 `design.md` 为准。任务正在实施；未勾选的步骤和阶段门禁尚未完成。严格作者校验、管理员旁路和现有测试不得为了让协作通过而被整体放宽。

迁移、后端能力、日志隐私和客户端必须作为兼容单元部署；中间阶段仅在隔离环境验证，不能只关闭前端入口后对外运行不完整的授权实现。

## 阶段 A：清点能力和建立回归基线

- [ ] A1 从实际 OpenAPI、递归路由和 service 调用链生成项目相关入口清单；逐项标注人工读/写、作品导出、AI、个人任务、作者专属、系列和管理员旁路。记录只有章节、场景、设定、提案或任务 ID 而没有项目 ID 的入口。（未勾：最终代码树上的 479 个公开操作尚无逐项闭合记录，只有 `test_collaboration_route_inventory.py` 在 Gate E 全量中通过。）
- [x] A2 为 owner、非成员、管理员读/写/回收及私人资源建立现有回归基线；保留 `test_cross_user_isolation`、管理员私有资源、回收站、作品备份及系列原有断言。
- [x] A3 确定协作 HTTP 能力矩阵的代表端点和后台任务类型，覆盖同步改写、章节/场景写、分析回写、Agent、任务流、知识库、向量与系列。路由检查必须断言扫描数量和已知具体路径，不能依赖平铺的 `app.routes`。

阶段 A 的原始清单和代表性基线见 `research/phase-a-route-inventory.md`。2026-11-10 的 `research/phase-a-reconciliation.md` 已逐键对账当前 479 个公开操作和 2 个隐藏文档路由；其中 270 个操作来自人工／规划专项复核，其余为静态分类，仍需核对实际能力与测试。A1 在最终树重新扫描及逐项闭合前保持未完成。

**Gate A**：产出可追溯的入口/能力清单；基线测试通过并记录尚未运行的环境。

## 阶段 B：迁移、协作状态与审计

- [x] B1 新增作品/系列协作 ORM 与 `0044` 迁移：唯一关系、角色/状态/正 revision 约束、每轮 UUID、查询索引和外键；项目相关表写入备份 `NOT_EXPORTED`。
- [x] B2 `generation_logs.actor_user_id` 可空且用户删除置空；按迁移前严格作者语义回填旧记录。改造单书备份规则：只导出作者自己的日志，过滤后的版本日志引用置空；导入时归导入用户。
- [x] B3 新增独立协作审计事件和 writer；成功与状态同事务，失败通过独立安全路径记录并用 operation UUID 处理未知提交结果；删除作品/用户后快照保留。
- [x] B4 升级与降级测试：空库/旧库、两个协作表分别非空、审计非空、非作者或未知 actor 的日志、有无用户删除、旧备份及版本引用。任何一类不能安全降级的数据都拒绝，不清数据。

阶段 B 独立复核在 SQLite 与隔离 PostgreSQL 16.15 上完成迁移（含空业务库）、审计、日志隐私/失败/裁剪、路由清单、备份注册表及 Agent 回归，共 30 项通过；后端 Ruff 和 `compileall` 通过。PostgreSQL 证据仅覆盖迁移等数据层约束，不代表协作写入或模型发送并发已验证；40001 错误分类使用模拟异常验证，删除 actor 的用例验证删除先于日志写入，尚未覆盖同时竞争。日志现按作品和真实 actor 分别裁剪，规范见 `.trellis/spec/backend/database-guidelines.md`。

**Gate B**：SQLite 迁移、备份注册表/往返和历史数据测试通过；PostgreSQL 升降级测试具备并在隔离库执行。

## 阶段 C：显式权限与邀请闭环

- [x] C1 新建协作访问解析及按动作 `require`，明确 owner、viewer、editor、管理员和重叠身份；保留原严格作者 helper 与原管理员授权路径，不跨事务缓存权限。
- [x] C2 实现作品/系列邀请、响应、修改、撤销及审计：每次重邀生成新 invitation UUID；全部状态写校验锁内 `expected_revision`；不存在/禁用用户同响应；并发唯一约束转安全错误。
- [x] C3 项目列表一次性查询自有和 accepted 共享，过滤生命周期；所有真实 HTTP 项目响应填充 actor 对应的 `access`，含创建、更新和封面响应。普通项目列表不包含管理员可管理但未获邀请的作品。
- [x] C4 提取并统一 gate/User → 发送写门禁（适用时）→ 项目父行 → 锁后权限重读的协议；系列邀请使用系列父行；普通写、角色修改与撤销遵守同一行锁。SQLite 旧快照/锁冲突须整体回滚。
- [x] C5 测试身份/能力矩阵、旧邀请迟到、版本竞争、重复邀请、自邀、账号状态、审计与软删/恢复；PostgreSQL 双连接验证撤权先提交和写入先提交。

阶段 C 独立复核最终组合回归 39 项通过，含 PostgreSQL 10 项并发、唯一约束与取消用例；其中直接等待作品父行的 3 项不依赖全局门禁。账号停用/令牌撤销竞争补充 2 项通过，Ruff 与 `compileall` 通过。阶段 C 只接入项目详情与资料更新等记录所述入口，其他入口仍按 D/E/F 迁移，不能单独部署。

阶段 D 开始后补强权限查询的 `no_autoflush`：新增 10 项回归通过，包含真实 PostgreSQL 撤权先提交及锁后重读；认证与生命周期组合 35 项通过。此前 39 项组合开始于认证备用查询的最后修复之前，需在最终全量回归时复核。

2026-11-10 在隔离 PostgreSQL 16.15 测试库顺序复跑 `test_collaboration_postgresql.py`、`test_collaboration_rowlock_postgresql.py`、`test_collaboration_access_postgresql.py`：11 项通过。此结果覆盖相应授权与行锁用例，不代表后续 AI、任务或系列门禁完成。

**Gate C**：邀请与作品访问矩阵通过；既有作者/管理员语义不变，撤权竞争在真实 PostgreSQL 验证。

## 阶段 D：按具体动作改造服务

- [ ] D1 按阶段 A 清单逐个迁移人工读/写、TXT/Markdown/EPUB、章节/场景/版本、story、规划、设定、知识库、记忆、笔记、提及和分析；子资源先找到父作品再做动作授权，不能只看路由方法。（未勾：各切片已迁移，并已随 Gate E 全量回归通过；但依赖 A1 清单逐项闭合，目前缺少逐项对照记录。）
- [x] D2 明确保持 `get_owned_project/get_owned_chapter/get_project_chapter` 严格，作者项目备份、回收、协作管理和阅读进度仍走专用能力；管理员手工写/回收继续原目标保护。协作者不绑定或修改作者私人风格、模板、模型或账号配置；项目/章节投影及模型提示词脱敏作者私人风格，保留全局预设，提交时不能将脱敏 null 覆盖原绑定。
- [x] D3 局部改写、场景 AI、封面、向导、分析、Agent、任务和无 `project_id` 路由逐一接 `ai.use` 或相应能力；viewer 不能触发不落库的 AI 预览。
- [x] D4 提及仅允许协作者看到自己的聊天来源；书内系列投影对未获系列权限的成员隐藏系列身份。加入读写反例和跨项目 ID 伪造用例。
- [x] D5 真实 HTTP 回归查看者无权备份/覆盖阅读进度、编辑者无权回收且不会触发删除意图或取消作者任务；管理员人工权限行为不变。

**Gate D**：完整能力清单均有归属与代表测试；不能用路径守卫存在性替代具体授权测试。

实施顺序调整：人工入口独立复核期间可先构建阶段 E 的内部模型解析与授权轮次原语，但不得接通协作者 AI、发送、任务或索引入口。D3 及依赖模型／任务的人工入口须与 E 一起完成，再核验完整 Gate D；中间状态不可部署。

## 阶段 E：任务、模型、日志和撤权

- [x] E1 统一项目模型解析：区分客户端显式配置与服务端回退；保存真实配置来源/归属和邀请轮次；覆盖生成、改写、富文档、分析、向导、实体、大纲、批量、封面及 Agent 的受理、运行、重试和续跑。
- [x] E2 `GenerationLog` 的所有同步/后台写入传真实 actor；列表、详情、Agent 日志、章节版本投影按个人可见性过滤，配置 ID 不泄露作者私有配置；可得 token 用量如实保存，缺失为 null。Agent effects 的 SSE、详情及撤销响应也需按 actor 脱敏他人的日志、任务和版本日志引用，内部撤销快照仍须完整保留。协作者改写采纳的 `ChapterOut` 须复用人工章节投影，过滤不可见的私人风格 ID。
- [x] E3 `Task` 与自动分析、批量、富文档采纳、Agent、知识库/记忆索引等衍生任务透传真实触发者和授权轮次，不把协作者工作改为作者任务；任务配额按实际 actor。
- [x] E4 在任务准备、受控模型/向量发送、Agent 工具、每批索引写、提案采纳和最终保存前重新鉴权；撤权或降级使旧工作失败关闭。设计受限 CAS 清理，按任务 UUID/轮次回退占用状态，不写旧正文。
- [x] E5 修改任务列表、详情、输出、取消/重试/审核及 SSE：本人 + 当前作者/accepted 协作身份，管理员人工读取权不能旁路；活动 AI 需要原授权轮次。撤权通知本地执行体，同时保证重启/跨会话的数据库复查；已经发送的数据不承诺撤回。
- [x] E5a 开放协作者 Agent 前，核对 `TransactionSession` 的 SAVEPOINT 和管理审计边界：代理内失败审计不能依赖随后回滚的 SAVEPOINT 持久化；需要在外层回滚后安全记账，并覆盖管理员兼协作者、权限失效及异常取消。既有严格作者 Agent 不受影响。
- [x] E6 知识库统一共享向量策略：无作者模型授权只维护/使用关键词、不调用作者向量模型、不覆盖已有不同向量；授权开启后可使用作者项目向量模型并明确费用。`IndexJob` 记录 actor，修正原有 `project.user_id == task.user_id` 假设。
- [x] E7 验证本人模型优先、失效不回退、授权回退、Agent 作者配置 ID、取消授权、实际请求次数和费用来源；覆盖日志、自动任务、模型发送、索引分批、订阅和失败清理。

索引并发补充：排队任务取消与新任务受理之间可能发生旧清理，因此正在设计 `0045` 为知识库文档持久绑定任务 UUID 和入队代次；新迁移、备份排除、旧作者任务兼容及有损降级保护须作为同一单元验证。章节占用的轮次隔离另行核对，不能仅凭 `generating` 状态清理。

**Gate E**：AI 配置与隐私测试、任务撤权测试通过；PostgreSQL 实际发送门禁与写入竞争通过。

## 阶段 F：系列权限与投影

- [x] F1 系列读取、共享条目写与成员管理分别鉴权；系列锁内重验角色；沿现有顺序锁系列、发送门禁及全部受影响项目。
- [x] F2 仅允许系列编辑者删除无任何绑定的条目；已绑定返回 `series_entry_bound`。作者删除保留现有依赖预检，不能将不校验书籍权限的内部传播锁用于删除投影。
- [x] F3 提升、采用、取消采用、移动与影响预览同时检查系列能力和全部受影响书的编辑能力；有回收站/无权书的归属变动按设计只允许作者执行。
- [x] F4 共享基础与 AI 策略传播保留各书本地演变、依赖检查和版本；对系列非作者过滤不可访问书的 title、ID、refs 和 blockers，不改变作者完整响应；批量提供包含隐藏/回收站绑定的 `has_uses` 供前端安全判断。
- [x] F5 覆盖系列/作品权限分离、仅系列协作者、双授权协作者、隐藏书、未绑定/已绑定删除、跨书传播、回收站及真实 PostgreSQL 竞争。

**Gate F**：系列新旧测试和隔离 PostgreSQL 系列/协作测试通过。

## 阶段 G：前端和浏览器验证

- [x] G1 `ProjectOut.access`、API 类型及 `projectAccess.ts`、`useProjectAccess.ts` 同步改造；即时能力、管理员+协作者重叠、异步响应失效与后端矩阵一致。
- [x] G2 书架支持共享角色和待处理邀请；项目/系列成员面板支持邀请、修改、撤销、模型授权和 revision 冲突；请求结果未知时刷新事实，不自动重发旧邀请操作。
- [x] G3 viewer 不挂载活动 AI/任务相关 effect、快捷键或订阅；viewer 兼管理员的人工编辑仍按管理员能力判断；协作者不写作者阅读进度；编辑者只访问自己的聊天/任务/日志，作者专属操作不可见。
- [x] G4 系列已绑定条目显示不能由设定协作者删除；知识库显示关键词降级及作者模型费用。网络失败、撤权、退出、切换项目与旧响应均安全清理。
- [ ] G5 前端测试能力矩阵、邀请/成员交互、旧响应、订阅和降级；真实浏览器分别验证作者、viewer、editor、系列协作者及管理员交集。（未勾：前端单测已覆盖；作者、viewer 和 editor 已在 2026-11-13、2026-11-15 两轮浏览器验证，但其后 E4i-h 又改了智能体前端，未在浏览器复验；系列协作者与管理员兼协作者尚未验证，只有 2026-11-10 早期快照上的抽查，以及单元测试和 PG 测试覆盖。）

**Gate G**：`npm run lint`、`npm run test`、`npm run build` 和关键浏览器验证通过。

## 阶段 H：完整回归与交付

- [x] H1 更新根目录 `README.md` 的协作入口、角色边界、模型费用、向量索引和迁移说明；同步项目规范及本任务验收记录，实际完成后才更新 `TODOS.md`。
- [x] H2 后端执行 `cd backend && uv run ruff check . && uv run pytest`；前端执行 `cd frontend && npm run lint && npm run test && npm run build`，记录结果。
- [x] H3 在独立测试库设置 `AINOVEL_COLLAB_POSTGRES_TEST_URL`，运行协作 PostgreSQL 测试及系列并发相关测试，记录数据库版本和结果；缺少环境时明确标注未验证，不以 SQLite 或 skip 代替。
- [ ] H4 按设计验证旧任务排空、数据库迁移、服务部署和回滚。旧服务未具备多用户日志隔离时不可直接回退；不以删除协作数据绕过降级保护。（未勾：部署、旧任务排空、迁移与回滚均未演练。）

**最终 Gate**：全部必需测试及真实 PostgreSQL 并发证据通过；任何未验证项单独记录，不报告为已完成。

## 进度记录：2026-11-10 基线恢复

前序并行会话中断后，后端测试套件已恢复为全绿基线（未放宽安全断言，未开放协作者 AI/任务入口）：

- `cd backend && uv run ruff check .`：All checks passed。
- 按文件逐个运行（每文件 600s 看门狗）：5175 passed、35 skipped、54 xfailed，0 failed / 0 error，无挂起。
- 81 个 `*_postgresql.py` 在未设置 PG URL 时整体跳过，**真实 PostgreSQL 并发未验证**（仅 `test_analysis_memory_enqueue_postgresql.py` 曾在 PG 上跑通：1 passed、1 xfailed）。

**strict xfail 清单**（均为尚未实现功能的预写测试，reason 以「待实现：」开头；实现后必须移除标记，strict 会在意外通过时报错）：
`test_api_key_actor_fencing.py`（11）、`test_shared_http_knowledge_e6.py`（E6）、`test_user_token_identity_reuse.py`、`test_agent_mcp_skills.py`（6）、`test_collaboration_editor_proposal.py`（2）、`test_collaboration_notifications.py`（3）、`test_collaboration_chapter_execution.py`、`test_collaboration_task_privacy.py`、`test_agent_authenticated_actor.py`（自动 MCP 外部调用、协作者流式与执行入口；Agent 派生任务与批量派生索引已于 E3 转正）。

**本轮裁决（偏离旧测试预期，已在测试注释中引用 design.md）**：
- book_import 失败关闭执行一次「无内容写入的系统清理」（status=failed、execution_key 清空），依据 design §5「不得留下永久锁」。
- legacy 分析任务 abort 允许按原快照重试，删除可完成且不触及其他章节。
- 富文本场景引用错误码统一为 `scene_reference_invalid`（遵循 spec/backend/scenes.md）。
- 未绑定 claim 的 legacy 知识库文档任务目前仍保守 fail-closed：保持排队并以 `task_cleanup_unproven` 阻塞项目删除。**与 design §5「不留永久锁」存在张力，待后续设计决定清理路径。**

## 进度记录：2026-11-11 迁移 0052（E2 子项：日志/模型实例键）

- 新增 `0052_generation_log_instance_identity.py`：`generation_logs.log_instance_key`（服务端 UUIDv4、唯一、插入必填、不可变）与 `model_config_instance_key`（不可变）；旧行保持 NULL；有键数据时拒绝降级。create_all 与迁移触发器逐字一致。
- Agent effects 用实例键识别日志；只带 created_at 的旧证明仅能匹配 `log_instance_key IS NULL` 的旧行；generation_logs/tasks 效果缺 before 或 after 一侧时记冲突（撤销不能创建/删除日志）。
- 单书备份不导出两键，导入重新生成；`APP_SCHEMA` 升为 "0052"。规范 database-guidelines.md / project-transfer.md 已同步。
- 原 strict xfail 的 0052 三个测试文件（14 项）已转正，另增 2 个回归用例。
- 验证：ruff 通过；逐文件全量 5191 passed、35 skipped、41 xfailed、0 failed；check 修复后受影响 10 文件复跑 134 passed、8 xfailed。
- 未验证：0052 的 PostgreSQL 升级/降级/触发器未在真实 PG 执行。E2 其余部分（日志写入 actor、个人可见性过滤等）沿用既有实现，本轮未重新审计。

## 进度记录：2026-11-11 E3（作者链路：派生任务受控受理、批量派生索引）

范围仅作者链路，协作者入口保持关闭。

- E3-A：Agent 提案时签发 HMAC 来源证明（域前缀 `ainovel/agent-task-origin/v1`），绑定 actor 与令牌版本、原轮次 Task 的 UUID/queued_at、change_key、项目、会话、tool_call_id、工具名、参数，章节类任务另绑章节 id + created_at。确认时 `prepare_confirmed_admission` 在取锁前完成证明核对、模型预检与 DNS，锁内重验后由 `execute_admitted` 经 `create_task(caller_transaction=True)` 在调用方事务/保存点内建任务；通用 `execute` 对 8 类任务仍 fail-closed。
- E3-B：`enqueue_memory_for_analysis` 接受 `chapter.batch` 收据受理与去重（existing 不要求同一父任务；actor/令牌/嵌入变化则 fail-closed）；批量 `_analyze_chapter` 传入 cipher 与 allow_private，此前批量从未派生索引；自动派生任务继承真实 actor；审阅采纳的来源不可信时跳过自动分析并给出 `REVIEW_AUTO_ORIGIN_UNTRUSTED`，采纳本身仍成功。
- 转正 strict xfail：`test_agent_tools.py`（8）、`test_analysis_memory_enqueue_fencing.py`（4）、`test_analysis_memory_enqueue_postgresql.py`（1）；新增 `test_agent_derived_task_admission.py`（11）与 `test_analysis_auto_actor_origin.py`。check 修正 existing 无子任务时状态为 current。规范 agent.md / retrieval.md / rbac-quotas.md 已同步。
- 顺带修复测试隔离：`agent_service._locks` 为模块级会话锁注册表，测试间会话 id 复用导致偶发 "bound to a different event loop"（test_recycle_bin_tasks 约 1/3 失败）；conftest 增加自动夹具每测试换新 WeakValueDictionary，生产代码未改。
- PostgreSQL 16.15（本机隔离库）：E3 相关 13 个文件中 11 个全过；`test_agent_project_style_privacy_postgresql::test_style_changed_before_real_provider_send_stops_without_http` 与 `test_collaboration_task_postgresql::test_postgres_http_stream_rechecks_after_cross_instance_revoke` 各 1 项失败，不在 E3 改动路径上，初判为测试数据未跟上执行身份收紧，待 PG 全量排查时核实。
- 设计取舍：章节身份为项目 + 主键 + created_at，SQLite 时间戳秒级精度，同秒内 id 复用只能尽力识别；批量去重不绑定父任务；审阅来源不可信时降级为跳过自动分析而非拒绝采纳。
- 验证：ruff 通过；逐文件全量 5218 passed、35 skipped、29 xfailed、2 failed——两项失败均已定位：派生受理文件为 check 改动途中被跑到的旧版本（单独复跑 11 passed），回收站为上述锁隔离偶发（修复后连跑 8 次全过）；修复后受影响 6 文件复跑 187 passed。

## 进度记录：2026-11-11 PostgreSQL 全量首跑与修复

- 首次在本机隔离 PG 16.15 上全量跑 82 个 PG 文件：383 passed、23 failed、2 errors。
- 测试基础设施：新增 `tests/pg_support.py`，conftest 提供 `pg_shared`（每进程一个随机 schema，create_all 一次）/`pg_schema_lease`（每测试 DELETE+序列重置+补预置数据，异常回退 TRUNCATE；DDL 指纹守卫，被改即判失败并重建）/`postgres_apps`；15 个夹具改为薄包装；迁移类测试保留独立 schema，新增 marker `pg_isolated_schema`。单进程全量 ~28 分钟 → 12~13 分钟。advisory lock 按数据库生效：同一库勿并发跑多个 pytest 进程。
- 产品缺陷（仅 PG 暴露）：
  - `transactions.session` 的 RR 代理引擎每次都是新对象，`_summary_guard` 按对象身份比较 sessionmaker，PG 上所有 Agent 真实发送被误判 SummaryExecutionChanged；改为记录 `visibility_bind` 原引擎（校验同连接池），发送校验会话用默认隔离级别。
  - send_message 提交并读取后在旧 RR 快照上取门禁 → 40001/409；取门禁前 rollback。复核补同类：`maybe_resume`（`holds_gate` 判定后回滚重取）与 `recover_external` 提交后先重取门禁。
  - 跨实例门禁等待后 RR 序列化失败：`authz.gate` 在 PG RR/SERIALIZABLE 且事务未开始时先 `LOCK TABLE authorization_state IN SHARE ROW EXCLUSIVE MODE`。
  - 规范 agent.md「并发视图」已补规则。
- 测试过时修正：MCP 远端补 tools/list、创建会话 201、证据测试删除改到另一应用实例、authenticated_actor PG 用例走真实受理、跨实例章节执行改轮询终态、撤权竞争固定时序、contype::text、通知流补 task_key。
- 新 strict xfail（协作者入口未开放，与同名 SQLite xfail 一致）：chapter_execution_postgresql 1、collaboration_task_postgresql 1、collaboration_notifications_postgresql 2。
- 验证：ruff 通过；PG 全量（含借用 PG 夹具的 4 个文件）472 passed、96 skipped、5 xfailed、1 failed（test_0049 备份测试缺本机 pg_dump/pg_restore，环境问题）；SQLite 逐文件全量 5220 passed、35 skipped、29 xfailed、0 failed。
- 遗留：`maybe_resume` 回滚路径无专门 PG 并发回归；`tests/test_mcp_settings_gate_debug_postgresql.py` 为来历不明未跟踪文件（未动）；96 个 PG skip 依赖其他专用库环境变量（scenes/series/fields 等），未验证。
- 设计取舍：撤权与写并发时，写已提交但响应可能为 404（最终投影失败关闭），保持现状。

## 进度记录：2026-11-11 E4（a~d）

范围：只做“不开放协作者入口”的四片；协作者 AI/任务/Agent 受理仍 fail-closed，未声明任何 `collaboration_execution_ready`，章节 strict xfail 未转正。

- E4-a 领取期授权复核与受限系统清理（`task_engine/engine.py`）：领取事务在 gate → actor/作者 User 锁 →（单章）发送写门禁 → 作品父行锁 → Task FOR UPDATE 之后，对带 `_collaboration_grant` 的任务调用 `_collaboration_claim_failure` 按最新数据库复核：账号启用、原令牌版本、accepted editor、邀请 UUID/revision 与 grant 逐字一致、作者模型来源（`_project_model.source=owner` 或向量 `_shared_embedding`）时 `allow_owner_ai` 仍为真。失效则 `_fail_collaboration_queued` 在同一事务按原 UUID/queued_at/`execution_key IS NULL`/无管理员意图 CAS 置 `failed`，并用 `release_chapter_claim` / `release_document_claim` 回退本轮占用；章节已被人工复位或换成其他轮次时只摘除任务内 `_chapter_claim` 指针、不碰当前章节；提交确认丢失时只按原 UUID 持久事实确认。错误码→固定文案：`collaboration_revoked`「协作授权已失效，任务已停止」、`collaboration_model_revoked`「作者模型授权已关闭，任务已停止」、`collaboration_account_unavailable`「账号状态已变化，任务已停止」（`COLLABORATION_FAILURE_MESSAGES`；Task 无独立 code 字段，SSE 帧保持 `{task, detail}` 形状以免破坏帧证据）。
  - 仍保持原屏障（不释放、不失败）的情形：无法捕获原章节 claim 的旧记录、无 claim 的旧文档任务（E4-e，已决：2026-11-12 改为锁内证明后受限系统清理，见下方 E4-e 记录）、有管理员取消意图（drain 负责）、input/checkpoint 存在待核对收据（分析 pending/completion、向导、外部调用、批量 current/待审阅等）、`agent.turn` / `book.import`（不在受限清理白名单，记日志）。
  - 授权仍有效但 handler 未就绪的协作任务行为不变（仍不执行、不失败）。
- E4-b `_fail_reset_queued_chapter`：去掉 `project.user_id != actor` 早退与 `require_authenticated` 前置；改为 gate + actor/作者 User 锁 → 门禁 → 作品锁，只校验“作者任务无 grant / 协作任务有 grant”的一致性，按原 Task/claim 逐字 CAS 失败；账号停用不再阻止收尾。
- E4-c `_run`：handler 未就绪 / 缺令牌版本的分支在已由 `matches_running` 证明是本轮执行时改走 `_finalize`（on_abort 按原 claim 回退 + CAS 置 failed「任务执行身份尚未确认」）；缺令牌版本时不以执行身份补签认证，改用不带身份的原 UUID/轮次/execution_key CAS。任务行已变、管理员意图在先、job 快照与领取身份不一致这三类无法证明本轮的早退保持原状（保留 running 屏障，交由 recover/管理员 drain），已加注释。
- E4-d `notify_collaboration_change`：除唤醒订阅者外，唤醒调度器（排队任务立即按 E4-a 复核）；对本进程中同作品同 actor 的运行体起后台复查：仅当数据库（`task_visible_to_actor(active_grant_required=True)` + 作者模型开关）确认原轮次失效，才登记 `_collaboration_revoked` 并按原 UUID/执行键 `cancel_running`；`_run` 将该取消映射为 failed + 上述文案。授权仍有效的通知不会停止执行体；作者任务不受影响；未收到通知（跨实例/重启）仍靠运行体自身的受控检查点。`stop()` 取消并等待复查协程。
- `require_write_execution` 直接检查 `allow_owner_ai`（新 `collaboration_task_access.task_requires_owner_ai` / `owner_ai_allowed`）：评估为低风险加入，不再只依赖“任何成员变更都递增 revision”。负对照（屏蔽该检查）确认对应用例会失败。

测试（新增）：
- `tests/test_collaboration_task_revocation_e4.py`（SQLite，20 项）：受理后领取前 × {撤权、降为 viewer、重邀新 UUID、经 PATCH 关闭模型授权、直接关闭模型授权不递增 revision、账号停用、令牌失效}：任务 failed + 对应文案、章节回到原状态、claim 与指针清除、零模型调用；有效 grant 仍排队且占用不动，同作品作者任务照常 succeeded；重邀只失败旧轮次、新轮次 claim 不被误清、旧轮次迟到复位收尾无副作用；文档 claim 释放；管理员 drain 在先时由 drain 收尾为 cancelled；E4-b 复位（含账号停用）不碰章节；E4-c 两种令牌情形；运行中 × {撤权、viewer、关闭模型授权} 经通知 + 数据库复查失败关闭，有效时通知不停止；无通知时下一写检查点直接拒绝关闭的模型授权，及正对照。运行中用例仅在测试内临时给 `outline.continue` 打 `collaboration_execution_ready` 补丁并替换 runner。
- `tests/test_collaboration_task_revocation_e4_postgresql.py`（PG，2 项）：撤权先提交时另一实例的领取被阻塞并在之后失败关闭；领取先在锁内读到有效授权时撤权等待，随后下一次领取失败关闭；均验证章节 claim 释放。

验证：
- `cd backend && uv run ruff check .`：All checks passed。
- SQLite 逐文件：新文件 20 passed；受影响 119 个任务/协作/claim/回收站/管理员任务等文件 1922 passed、2 skipped、7 xfailed、0 failed；作者路径另补充 34 个文件（生成日志、向导、知识库、分析、大纲、Agent、拆书、章节、管理员作品；少数与上组重叠）452 passed、0 failed。
- PostgreSQL 16.15（隔离 collab_test，逐文件单进程顺序）：22 个任务/claim/协作相关 PG 文件全部通过（含新文件 2 passed；既有 3 个 strict xfail 不变）。注意：部分既有 PG 文件导入 SQLite 测试模块，把多个文件放进同一 pytest 进程会额外收集数千个 SQLite 用例、看似“挂起”，应逐文件运行。

遗留 / 待裁决：
- ~~E4-e（无 claim 的旧知识库文档任务）未做：仍保持排队并以 `task_cleanup_unproven` 阻塞删除，待设计裁决清理路径。~~ **已决**（design.md §6.3「旧无占用知识库文档任务（E4-e 裁决，2026-11-12）」），已实现，见「进度记录：2026-11-12 E4-e 旧无占用文档任务清理」。
- 授权有效但 handler 未就绪的协作排队任务仍会被反复领取后返回（`_on_done` 触发 notify，形成调度热循环）；入口开放（E4-f/g）后自然消失，若需提前处理应另行裁决（例如固定失败或退避）。
- `agent.turn` / `book.import` 的协作排队任务未纳入受限清理（两者 build_job 已拒绝 grant，且无法经 HTTP 受理），失效时仅记日志、保留原屏障。
- 作者任务的令牌失效/账号停用仍按原行为保持排队（未改变作者任务语义）。
- 运行中失效的唤醒在本进程内是“数据库复查后取消”，比“下一检查点自行失败”更早停止；已发出的请求不可撤回。跨实例仍只依赖运行体检查点（无跨进程通知）。

设计取舍：
- 受限清理与领取共用同一事务和锁序，复用 `release_*` 原语（不要求成员身份、按原 UUID/轮次/claim CAS）；不写正文、设定或模型产出。
- 无法证明本轮或资源归属时一律保持原屏障并记日志，宁可停在排队/运行也不猜测释放。
- 失败原因以固定文案区分，不新增 Task 字段或 SSE 帧字段，避免触碰帧证据契约。

### E4（a~d）check 结论（2026-11-11）

- 复核：领取复核/受限 CAS 与领取同事务，锁序 gate → User 升序 →（单章）发送写门禁 → 作品 → Task，与撤权 `_lock_parent`（require_authenticated(write, targets) → 门禁 → 作品）同序且共用 gate，无反向取锁；引擎会话为默认隔离级别，不适用 Agent RR 首语句规则；CAS 带原 UUID/queued_at/`execution_key IS NULL`/无管理员意图，失败整体 rollback；章节 claim 不匹配时只摘指针，持有原 claim_key 则回滚不动；未声明 `collaboration_execution_ready`，strict xfail 未转正。
- 修复：`_check_collaboration_runs` 在 DB 复查返回后先确认原执行体仍在本进程且 UUID/执行键未变才登记 `_collaboration_revoked`，避免复查期间已收尾时残留标记。
- 补测：通知后复查直接核对不递增 revision 的模型授权关闭（以「作者模型授权已关闭」失败，执行体被取消）；针对作者本人的通知不取消作者运行任务且照常 succeeded；检查点用例补充失败文案只为固定安全文案。
- 存疑：测试替身绕过 `verify_event` 时，最终写身份核验拒绝落为通用「任务执行失败：服务器内部错误」（安全但不精确，真实 handler 在 verify_event 处映射为「任务输出授权已变化」）；未改。
- 验证：ruff 通过；E4 SQLite 22 passed；受影响 12 个 SQLite 文件全部通过；PG 逐文件 4 个（含 E4 2 项）通过，xfail 不变。

E4（a~d）最终回归（2026-11-12，主会话）：ruff 通过；SQLite 逐文件全量 5242 passed、35 skipped、29 xfailed、0 failed；PostgreSQL 16.15 顺序全量（`tests/test_*postgres*.py` + 借用 PG 夹具的 4 个文件）474 passed、5 xfailed、1 failed——唯一失败仍为 `test_0049_postgresql_full_backup_restores_instance_keys` 缺本机 pg_dump/pg_restore（环境问题）。

## 进度记录：2026-11-12 E4-f 章节协作入口

范围：只开放单章类任务（`chapter.generate` / `chapter.polish` / `chapter.revise`，HTTP 入口 `POST /chapters/{id}/generate|polish`、`POST /chapters/{id}/revise`（分析建议重写）、场景 AI 的 `chapter.revise` 入口）。批量、分析、大纲、实体、向导、知识库索引、Agent 仍失败关闭；任务列表/SSE 的协作者开放属于 E5，未改。

做了什么：
- `ChapterHandler.collaboration_execution_ready = True`：走既有带 grant 的受理（`task_service.create_task` → `require_project(ai.use, grant)`）、领取复核（E4-a）、`build_job`（`require_write_execution` + `require_running_chapter`）、`SendGuard.for_task` 逐次发送重验、最终保存 `require_task_write` / `verify_event`。重试路由（`api/v1/tasks.py`）因 handler 就绪随之对本人章节任务开放，`validate_retry` 要求原模型绑定 grant 与当前邀请轮次一致。
- `generation_service.build_chapter_job`：原「协作检索尚未就绪」总闸收窄为只拒绝无执行身份或缺冻结共享向量选择的协作 claim（否则会落到协作者本人默认向量配置）。协作者 generate/revise 检索只用受理时冻结的 `ResolvedSharedEmbedding`：无授权 → keyword、零向量发送；`allow_owner_ai` → 作者向量、查询嵌入经 `for_task` 守卫；命中仍逐条 `verify_retrieval_hits`，检索只读。
- `ChapterHandler.on_success`：带 grant 的执行不派生自动分析（分析未开放协作执行，`enqueue_auto` 本就拒绝；改为显式跳过，避免每次成功都写「自动分析状态未确认」误导警告）。
- 引擎：新增 `_finish_before_cancel`，`build_job`、delta 等非终止事件核验与输出刷新在取消（撤权复查 / 用户取消）时先等在途持锁事务结束再传递取消（结果丢弃，不用刚组装的 job、不发布未完成核验）。真实缺陷：取消打断 aiosqlite 游标会遗留写事务，`_restore_collaborator_claim` / `on_abort` 永远拿不到锁，任务卡在 running（修复前保存前撤权用例多次运行中约 1/3 卡死，修复后连跑 72 次通过；两处各有确定性回归，负对照还原后均失败）。E4-d 让撤权自动取消运行中的协作执行体，章节入口开放后该窗口真实可达；作者的用户取消同样受益。
- 引擎固定文案：`permission_denied`（CollaborationSendBlocked）→「权限或模型授权已变化，已停止发送」；事件发布/输出刷新核验失败改带受控 code `task_output_changed`→「任务输出授权已变化，已停止」（此前落为通用「任务执行失败：服务器内部错误」，即 E4 check 的存疑项）。

测试：
- 转正 strict xfail：`test_collaboration_chapter_execution.py::test_editor_chapter_task_is_attributed_to_real_actor`（SSE 属 E5，改经本人任务详情观察终态，并加强断言：日志 success、`model_source=actor`、章节 done 且 claim 清除）；`test_collaboration_chapter_execution_postgresql.py::test_editor_reinvite_after_send_cannot_save_old_result`（原断言不变，真实通过）。分析相关 `test_collaboration_editor_proposal.py` 的 xfail、四个 SSE xfail（E5）未动。
- 新增 `tests/test_collaboration_chapter_entry_e4f.py`（SQLite，32 项）：editor generate/polish 用本人模型保存、Task/日志归真实 actor、本人优先（作者授权开启也不用作者密钥）、无本人配置时按授权回退作者模型并记 `model_source=owner`/作者为配置所有者、无授权 400、本人配置损坏 400 不回退、不能选作者配置/私人风格且私人风格不进入提示词与日志、viewer/admin+viewer 403 与待接受/非成员/已撤权/仅管理员 404（零任务零调用）、受理后领取前 × {撤权、降 viewer、关作者模型授权、停用}、领取后 build 前/发送前 × {撤权、viewer} 零发送、发送后保存前 × {撤权、viewer、关模型授权} × {本进程通知、无通知} 不写正文、已提交结果撤权后保留、重试同轮次成功与重邀后旧任务重试 400、作者链路不变、共享向量未授权零嵌入/授权用作者向量且不建协作索引任务、取消不打断在途核验与在途 build_job 的确定性回归。build 前/发送前用例的撤权在独立任务中发起（httpx ASGITransport 在调用方任务内运行应用，若在执行体任务里直接请求，撤权通知取消执行体会打断撤权请求自身的收尾，属测试假象）。
- 新增 `tests/test_collaboration_chapter_entry_e4f_postgresql.py`（PG，3 项，执行引擎在另一实例、无本进程通知）：保存事务先持锁 → 撤权被阻塞至保存提交，正文/本人成功日志保留；撤权先完整提交 → 保存锁内重验失败，正文与成功日志均不写、章节占用回退（撤权 / 降 viewer）。
- 既有测试调整（不放宽）：`test_collaboration_chapter_receipt_identity.py::test_editor_generation_never_calls_legacy_owner_retrieval` 原断言旧总闸（编辑者 generate 的 build_job 必抛错），按收窄后的规则改写：无执行身份的直接调用仍在组装上下文前拒绝；经原执行时只用冻结的 keyword 选择（embedder 为空）、上下文按真实 actor 组装，作者身份旧检索 / 本人默认向量入口一次都不调用、零模型请求。`test_collaboration_task_revocation_e4.py` 三个「handler 未就绪」用例（含重邀新轮次保持排队）与 `_postgresql.py` 两个领取复核用例改为测试内临时撤销章节就绪声明（新增 `chapter_unready` 夹具），原断言不变；`test_running_checkpoint_rejects_owner_ai_closed_without_revision_bump` 允许的固定文案由通用服务器错误换为新的「任务输出授权已变化，已停止」。

验证：
- `cd backend && uv run ruff check .`：All checks passed；`compileall app` 通过。
- SQLite 逐文件（每文件独立进程，4 路并行）全量 333 个非 PG 文件：5272 passed、34 skipped、28 xfailed、1 failed——唯一失败为高并发负载下 `test_agent_runtime.py` 的一次偶发（未取到用例名），随后在负载下单独复跑 4 次全部 20 passed、其中取消相关用例另跑 4 次通过，判定为负载计时问题。最终代码另对受影响 175 个文件完整跑一轮：2684 passed、6 skipped、6 xfailed、0 failed。E4-f 新文件在负载下多次复跑 32 passed；保存前撤权用例修复后连跑 72 次通过。
- PostgreSQL 16.15（隔离 collab_test，逐文件单进程顺序）：32 个章节/任务/claim/协作/收据/日志/审阅/改写/分析相关 PG 文件全部通过，含新文件 3 passed、转正的 `test_collaboration_chapter_execution_postgresql.py` 11 passed、E4 PG 2 passed；既有 3 个 SSE strict xfail（E5）不变。未跑全部 PG 文件。

遗留 / 偏差：
- 保存已提交、撤权恰好落在提交与 `done` 核验之间：`done` 核验与 `verify_finalization` 要求当前授权，任务停在带完整完成收据（含 grant）的待核对 running，正文/版本/日志保留；但 Task 仍占 `chapter:N` 活动资源键（作者也无法再对该章发起 AI）且项目记入 `_abort_failures`，重启 `recover()` 也只记待核对。作者在提交后瞬间改密/改模型同样触发，属终态证据模型的既有限制；系统级按收据收尾为 succeeded（不向失权订阅者发布）需与 E5 帧发布身份一起设计。PG 用例按当前可保证性质断言（`succeeded` 或带 completed 收据的 `running`）。
- 协作者执行不派生自动分析，待 E4-g 开放分析后改为派生给真实 actor。
- 旧轮次重试在原模型预检处被拒为 400「模型绑定无效或已失效」（失败关闭、无副作用），不是 409。
- 仅管理员身份（非成员）请求章节 AI 为 404（无协作读取来源），与「管理员读取权不能旁路协作 AI」一致。
- `test_collaboration_editor_proposal.py` 分析 xfail 的 reason 文案仍写「目前无 handler 声明」，属分析切片，留待 E4-g 一并更新。

### E4-f check 结论（2026-11-12）

- 复核（读代码）：仅 `ChapterHandler` 声明 `collaboration_execution_ready`；`create_task` 只对就绪 handler 走 `require_project(ai.use, grant)`，其余仍 `get_owned_project` 严格归属（协作者 404）；重试路由对非就绪 handler 409、就绪时要求原 token 版本与原模型绑定（含邀请轮次）逐字一致，旧轮次在预检处失败、零副作用；任务列表/详情/SSE 未改（E5）。仅管理员身份无协作读取来源 → 404，不能旁路协作 AI。`build_chapter_job` 收窄后：协作 claim 的 generate/revise 无执行身份或无冻结选择仍拒绝；有执行身份时只走「冻结 `embedding_selection`」分支（keyword 不建 embedder、零向量请求；vector 为作者向量并经 `for_task` 守卫逐次重验），不会进入 `get_embedder`/`default_embedding_config`（本人默认向量）分支；风格/模板按 actor 解析（`_visible(actor)`、`get_override(actor)`），作者私人风格不可见。
- `_finish_before_cancel`：只包含 build_job、非终止事件核验与输出刷新这些持锁步骤；取消只被延迟到该步结束，随后原样重抛 `CancelledError`（步骤异常被丢弃），`_run` 的取消/撤权映射不变。取消只发一次（`_cancel_requested` 守卫，`stop()` 不重复取消），无二次取消打断 `await step` 的情形；撤权在提交后才通知，执行体等待的步骤不会反向等待撤权事务，未见死锁。时延上限 = 该步耗时：章节 build_job 内查询向量各有 20 秒超时（知识库/记忆两次），SQLite 锁等待受 busy_timeout 约束；`cancel_running` 仍最多等 10 秒后返回、任务稍后收尾，作者取消可接受。
- **修复（重点遗留窗口）**：新增 `TaskEngine._complete_from_chapter_receipt` 受限系统收尾。凡单章任务在保存已提交后因取消、`done` 事件核验失败或 `verify_finalization`/终态写入失败而未能成功收尾（撤权、降 viewer、关作者模型授权、作者改密、可见性变化等），按原 UUID/`queued_at`/执行键 + `running` + 无管理员意图 CAS，并经 `completed_chapter_receipt`（锁作品→Task，逐字核对收据、输入摘要、版本摘要及协作模型/日志实例来源）证明后，Task 记为 `succeeded`、`error` 固定为 `RECEIPT_FINALIZED_NOTE`、清 `execution_key`、释放 `chapter:N`；`result` 仅当与收据 `result_hash` 一致才保存（恢复路径为 null）。不要求当前授权，但不写正文/版本/日志、不派生自动分析、不向外发送；Hub 以不含任务快照的 error（`RECEIPT_FINALIZED_DETAIL`）关闭，订阅侧按数据库终态与当前授权回放。`recover()` 对带完成收据的 running 单章任务同样按此证明收尾（原先永久「待核对」）；收据损坏/伪造、有管理员意图或证明不可用时保持原屏障。PG 旧用例原「`succeeded` 或 `running` 二选一」改为必须 `succeeded` 且执行键清空。规范 error-handling.md 原「已知限制」条改写为该契约。
- 补测：SQLite `test_collaboration_chapter_entry_e4f.py` +9（{撤权、viewer、关模型授权}×{本进程通知、无通知} 提交后 done 前变化 → 收据收尾、作者可立即再发起、失权协作者 404/403/400；作者改密同窗；篡改收据不收尾且 recover 亦拒绝；进程内收尾不可用时 recover 收尾）。PG `_postgresql.py` +4（另一实例在 done 核验取锁前提交撤权/降 viewer/作者改密，及 recover 变体），确定性命中该分支。负对照：让收尾直接返回 None，SQLite 8 项、PG 4 项全部失败。
- 测试质量：被调整的旧测试未放宽安全断言（检索测试改为「直接调用仍拒绝 + 经执行身份零旧检索/零请求」；E4 用例改为临时撤销就绪声明，断言不变；固定文案由通用 500 收紧为受控文案）。
- 验证：`uv run ruff check .` 通过；SQLite 148 个章节/任务/claim/收据/协作/批量/富文档/统计/回收站相关文件逐文件：2349 passed、2 skipped、6 xfailed、0 failed（E4-f 文件 41 passed）；PostgreSQL 14 个相关文件逐文件顺序：全部通过（E4-f PG 7 passed，既有 1 xfailed 不变）。
- 仍存疑：收据收尾的终态帧固定为 error 形状（不发布 done 结果），已授权订阅者需刷新/重订阅才看到成功结果，帧发布身份留 E5；`ChapterCommitUncertain`（提交确认丢失且即时证明不可用）仍只在重启 `recover()` 时按收据收尾，进程内不会自动重试证明；收尾成功后进程内 `_abort_failures` 等项目级标记若此前已登记不会被清除（仅影响该进程的删除前置检查）；作者任务按收据收尾时不派生自动分析（以 note 告知）。

### E4-f 偶发卡死修复（2026-11-12）

- 现象：`test_revoke_after_claim_before_send_never_sends[*-send]` 约 1/5 卡满 10 秒，任务停在 running；日志依次为协作占用回退失败、约 5 秒后 on_abort 回退失败、取消收尾失败、终态写入失败（间隔即 SQLite 默认 5 秒 busy_timeout）。
- 根因（产品缺陷，非测试构造）：撤权复查取消执行体时，取消正落在真实发送租约 `_GuardedProvider._lease` → `authorize_send_actor` → `require_authenticated_for_send` 的语句上（该事务已由 `lock_users` 的 no-op UPDATE 取得写锁）。SQLAlchemy 把 CancelledError 视为退出异常，不关游标、不回滚，直接作废并 terminate 连接；被打断的语句在 aiosqlite 线程里照常执行完，游标被异常回溯引用而未 finalize，sqlite3 关闭带未 finalize 语句的连接只会变成僵尸连接，库锁保留到游标被循环 GC 回收（时机不定，因此偶发）。临时调试确认：被作废连接的 pool `invalidate(CancelledError)`/`close` 事件已触发而锁仍在；纯 sqlite3 复现：关闭带未完成 SELECT 的写事务连接后其他连接 `BEGIN IMMEDIATE` 失败，GC 后恢复；只回滚仍留读锁，其他连接无法提交（中途尝试的「仅回滚」版本使测试的读查询也被锁住，已否定）。`_finish_before_cancel` 只覆盖 build_job/事件核验/刷新，发送租约这一持锁步骤是遗漏点，且同类风险存在于任何被取消打断的 SQLite 写事务。
- 修复：`app/core/database.py` 对 SQLite 引擎注册 `handle_error` 监听：原始异常为 CancelledError 时，在 greenlet 内把「关闭执行上下文上的被打断游标（finalize）」和「回滚」按序排入 aiosqlite 线程队列（在被打断语句之后、SQLAlchemy 随后的 terminate/close 之前）；只撤销本就要随连接作废丢弃的未提交事务，取消照常向上传递，PG 不受影响。注意 SQLAlchemy 2.1 的 `ExceptionContextImpl.cursor` 声明了但从不赋值，须从 `execution_context.cursor` 取。
- 回归：新增 `tests/test_sqlite_cancel_rollback.py`（写事务内取消一条读表的慢查询，保留异常引用，另一连接须能在 2 秒内提交写入）；负对照：去掉监听→失败，只回滚不 finalize→失败。
- 验证（负载平均 40–70）：目标用例连跑 45 次 0 失败（180 个参数化用例）；SQLite 逐文件 e4f 41、E4 撤权 22、协作章节执行 35、tasks 32、chapter_claim core/wiring/author_wiring/migration 24/20/12/2、batch claim 55、batch generate 27、回收站任务 12、SSE/流批 1/2、agent_api 11、agent_runtime 20、可见性发送 19+1 skipped、协作故事发送 9、模型预检引擎身份 15，全部通过；PG 逐文件单进程 e4f 7、E4 撤权 2、协作章节执行 11、chapter_claim 3、author 2，全部通过。`uv run ruff check .` 通过。
- 另：`test_agent_api.py::test_undo_turn_is_atomic_when_earlier_change_conflicts` 不是卡死。两次工具调用约 8500 条 SQLite 语句（2.4 万次线程往返，无 sleep），完成时间随负载线性增长：串行约 1.4–2.5 秒、3 路并行约 2 秒、8 路并行 3.9–5.2 秒，分布单峰、无 5 秒 busy 卡顿，60 秒上限下全部成功。仅把该用例的 `wait_task` 上限从默认 5 秒放宽到 30 秒，断言不变。

E4-f 收口回归（2026-11-12，主会话，按精简策略）：全量 SQLite 在完成 197 个文件时按用户要求停止（仅 3 个超时类失败，两项已修复/放宽、一项负载超时）；改为受影响文件 + 取消相关哨兵：修复代理已跑 19 个 SQLite 与 5 个 PG 相关文件全过；主会话另跑 121 个含取消路径的 SQLite 文件（2 路并行，逐文件）2503 passed、20 skipped、24 xfailed、0 failed、无挂起。`database-guidelines.md` 补「SQLite 取消释放」规则。SQLite/PG 全量留到 Gate E 收口。

## 进度记录：2026-11-12 E4-g1 分析协作入口

范围：只开放章节分析（task type `chapter.analyze`）。HTTP 入口 `POST /chapters/{id}/analyze`（手动）、`POST /projects/{pid}/analysis/analyze-pending`（一键分析）；提案采纳派生（既有）与单章 generate/polish/revise 成功后的自动派生。分析建议的确认/拒绝/撤销（`/analysis-changes/{id}/confirm|reject|revert`）是人工写入，阶段 D 已按 `content.edit` 开放，本轮未改。批量、大纲、实体、向导、知识库索引、Agent 仍失败关闭；任务列表/SSE 属 E5 未动。

做了什么：
- `AnalysisHandler` 声明 `admission_preflight_aware` 与 `collaboration_execution_ready`。`validate_and_claim` 改为消费中央已接受的预检模型（`accepted_model`），新 `analysis_service.validate_analysis_task` 在受理事务内按原 grant 锁后重验 `ai.use`、章节归属与正文，并在同一屏障冻结共享向量选择（协作者未获授权 → keyword）；不再持锁解析 DNS。旧 `validate_analyze` 删除。
- `request_analyze` / `analyze_pending`：先短读 `require_project(ai.use)`（viewer 403、无协作读取来源/仅管理员 404）+ `preflight_project_model`（本人优先，仅无配置且 `allow_owner_ai` 时回退作者，本人配置无效不回退）+ `prepare_task_admission`，再按 认证门禁 → 发送写门禁 → 作品（按预检 grant）加锁，按本人 + 同轮次去重后交中央 `create_task`。一键分析逐章准备证明，锁内重算后只为已有证明的章节入队。
- `_analysis_proposal_source` / `_valid_execution`：协作分析不带提案来源也合法（手动与单章派生），带来源时仍完整校验，作者不得携带。
- 自动派生：`ChapterHandler.on_success` 去掉 grant 跳过；`enqueue_auto` 接受协作执行，继承原 actor、令牌、模型绑定（含费用来源）与 grant，在 认证（含作者 User）→ 发送写门禁 → 作品（原 grant）锁内核验原单章完成收据与模型/向量授权后入队；撤权/降级/重邀/关模型授权时抛错不入队，引擎记固定提示，章节成功不受影响。
- 写回：`run_analysis_job` 原有写事务（`_analysis_write_barrier` 按原 grant/令牌 + `require_task_write` + 模型/向量重验）即覆盖分析行、设定/伏笔回写与剧情记忆，本轮未改，只用测试证明。记忆向量：`enqueue_memory_for_execution` 对 grant 执行只返回 keyword/unavailable，不建 `kb.index`、不调用作者向量模型（E6 未开放）。
- 引擎：`_POLICY_MESSAGES` 增加 `analysis_execution_changed` →「分析任务执行身份或授权已变化，已停止」（此前落为通用服务器错误）。新增 `_complete_from_analysis_receipt`：写回已提交但 done/终态核验因撤权或改密失败时，按原 UUID/轮次/执行键 + running + 无管理员意图 CAS 并经 `completed_analysis_receipt` 证明，Task 记为 succeeded（`ANALYSIS_RECEIPT_FINALIZED_NOTE`，result 由收据逐字重建），释放 `analysis:{project}` 串行键，不派生后续任务；`recover()` 对带完成收据的 running 分析同样收尾。修复前该窗口使任务永久停在 running，阻塞整部作品的后续分析（含作者），撤权使其真实可达。
- Agent：分析改为受控受理后，作者 Agent 的 `start_task(chapter.analyze)` 只能经提案确认的 `execute_admitted`（签名来源），通用 `execute` 失败关闭。协作者 Agent 仍关闭。

测试：
- 转正 strict xfail：`test_collaboration_editor_proposal.py::test_editor_proposal_analysis_runs_as_original_actor_with_frozen_model`（2 项，原断言不变，xfail 及过时 reason 删除）。
- 新增 `tests/test_collaboration_analysis_entry_e4g.py`（SQLite，55 项）：editor 手动分析用本人模型、Task 归真实 actor、费用来源（`_project_model.source/config_owner_id` 与实际密钥）、回退作者模型且作者向量零调用/零索引任务；无模型无授权 400、本人配置损坏 400 不回退、不能选作者配置；viewer/admin+viewer 403 与 pending/非成员/已撤权/仅管理员 404（手动与一键均零任务零调用）；一键分析与按本人同轮次去重；作者模板/私人风格不进入协作者请求（作者正对照）；受理后领取前 ×{撤权、viewer、关模型授权、停用}；build 前/发送前 ×{撤权、viewer}；流中撤权与写回屏障前撤权 ×{本进程通知、无通知}（后者含停用）不写分析/回写/伏笔/记忆；重试同轮次成功、重邀后旧任务重试被拒；作者链路不变；自动派生（本人/作者模型）、项目关自动分析不派生、派生前撤权/降级/关授权与重邀不派生且章节仍成功；done 前授权/作者改密 → 收据收尾并释放串行键、篡改收据不收尾、recover 收尾。
- 新增 `tests/test_collaboration_analysis_entry_e4g_postgresql.py`（PG，5 项，引擎在另一实例、无本进程通知）：写回事务先持锁 → 撤权阻塞至提交、结果保留；撤权先提交（撤权/降 viewer/关作者模型授权）→ 写回锁内重验失败、零写入；写回提交后 done 前撤权 → 收据收尾。
- 负对照：屏蔽写回事务的 grant/写重验 → 写回前撤权（无通知）4 项失败；收据收尾直接返回 None → 9 项失败。最初的「流中撤权」用例被逐帧发布核验先拦下、未覆盖写回屏障，因此另补写回屏障用例。
- 既有测试调整（不放宽）：`test_collaboration_analysis_execution.py` 原「编辑者分析 404」改为断言受控受理入队（grant、actor 模型来源、无提案来源），缺绑定行仍不能被捕获；另一用例中的 404 前置断言删除。`test_collaboration_planning_access.py` 原 viewer/editor/非成员一律 404 改为 403/400/404 且零任务。`test_agent_tools.py` 两项：分析的通用执行改为失败关闭断言，外层事务边界改用仍走通用执行的 `chapter.batch` 验证。`test_collaboration_chapter_entry_e4f.py` 默认关闭部署级自动分析（文件按章节调用次数断言，派生会多一次模型调用），4 个收据用例显式开启，继续断言收据收尾不派生。

验证：
- `cd backend && uv run ruff check .`：All checks passed。
- SQLite 逐文件（3 路并行）99 个受影响文件：1747+204 passed、1 skipped、1 xfailed；唯一失败为 `test_collaboration_planning_access.py` 的旧 404 断言，按上文调整后单独复跑 14 passed。e4g 文件 55 passed。
- PostgreSQL 16.15（隔离 collab_test，逐文件单进程顺序）：23 个分析/章节 claim/收据/协作任务/审阅/规划相关 PG 文件 155 passed、1 xfailed（既有 E5 SSE strict xfail），含新文件 5 passed。未跑全部 PG 文件。

遗留 / 偏差：
- 一键分析为每个待分析章节各做一次锁外 DNS/provider 预检（受理证明不可复用），章节很多时有额外耗时；未做批量证明。
- 写回已提交、done 前授权变化的收尾 Task 记 succeeded 并保存收据重建的 result，但 Hub 以不含任务快照的 error 帧关闭（同 E4-f，帧发布身份留 E5）；作者任务按收据收尾时不派生记忆向量索引（以 note 告知）。
- 自动派生在撤权后失败时，章节 Task 的 error 写「正文已保存，自动分析状态未确认…」（沿用引擎既有提示），失权协作者已看不到该任务。
- 协作者分析的记忆只维护关键词单元；即使作者授权共享向量，分析结果 warning 为「向量索引暂不可用」，开放属 E6。
- 协作者 SSE 仍 404（E5）；前端分析按钮按 `can_use_ai` 显示的核对未做。

设计取舍：
- 沿 E4-f 模式要求开放的 handler 同时 admission-aware，因而作者 Agent 的分析也改走签名来源受控受理，不再允许通用执行；比保留双路径少一个未受控入口。
- 写回已提交但无法发布时以收据证明收尾，而不是保留 running 屏障：保留屏障会让整部作品的分析串行键永久占用，且已提交结果本就不回滚。

### E4-g1 check 结论（2026-11-12）

- 作者回归：Agent 的 `task` 类提案不在 `_can_auto_execute`（只放行 small/text）中，`auto_execute` 从未直接执行 `start_task`，所有任务提案都需确认。确认时 `service.execute_change` 对受控受理类型改走 `execute_admitted`，runner 写提案时已为 `chapter.analyze` 签发来源证明，作者 Agent 分析仍可确认执行（`test_agent_api::test_start_task_is_confirmed_and_can_be_cancelled` 通过）。被关闭的只是无提案来源的通用 `execute`，生产代码中没有调用它执行任务提案的路径。升级前已存在、未带来源证明的分析提案在确认时失败关闭，与 E4-f 章节类一致，属于可接受的一次性影响。作者 HTTP 手动分析和一键分析、单章成功后自动分析（`test_analysis`）、富文档采纳派生（`test_rich_document_analysis`）、批量分析（`test_batch_generate`）、拆书 `enqueue_pending`（直接插入带 `_project_model` 的作者行）均未变化。旧的作者分析任务如果缺少 `_project_model`，重试会被拒并提示“请重新创建任务”，与 E4-f 相同，没有补签。
- 开放范围：只有 `ChapterHandler` 和 `AnalysisHandler` 声明了 `collaboration_execution_ready`，其他 handler 仍失败关闭。任务列表、SSE 和 `get_owned_task` 仍按真实 actor 返回。
- 向量与写回：`build_analysis_job` 没有检索或嵌入调用。写回只经 `memory_service.sync_from_analysis` 和 `retrieval_service.upsert_many` 写关键词单元；`enqueue_memory_for_execution` 只在 `grant_identity is None` 且选择为 vector 时才派生 `kb.index`，协作执行返回 keyword/unavailable。
- 自动派生：`enqueue_auto` 从原 Task 的冻结输入取模型和向量绑定，继承 actor、令牌和 grant；在 认证（含作者 User）→ 发送门禁 → 作品（原 grant）锁内核验完成收据并重验授权，任何异常都统一转为 `AnalysisExecutionChanged`，不入队。章节 `_shared_embedding` 在 defer_claim 路径上总会写入（polish 也一样），所以作者润色派生不会因缺绑定而失败。
- 收据收尾：`_complete_from_analysis_receipt` 按原 UUID、queued_at、execution_key、running 状态和“无管理员意图”做 CAS，并逐项比较原 claim 与当前行捕获的执行身份；`completed_analysis_receipt` 锁内核验后才更新，result 由收据重建，只改本 Task，不派生后续任务，状态离开 running 后串行键即释放。边界与 `_complete_from_chapter_receipt` 一致。
- 测试调整没有放宽断言：e4f 默认关闭自动分析只影响按调用次数断言的用例，派生的正例和反例由 e4g 覆盖，四个收据用例显式开启。
- 评估后暂不修改：（1）`analyze_pending` 对每个候选章节在锁外各执行一次 `prepare_task_admission`（DNS/provider 预检），执行期间不持锁也不占用事务，章节多时只会拉长请求耗时，可留作后续批量证明的优化。（2）`analyze_pending` 在循环中逐个调用 `create_task`，第一次提交后就释放了 `_lock_analysis_admission` 取得的作品锁，后续章节靠 `create_task` 自行重新加锁。并发的两次一键分析可能为同一章各排一次分析，但同项目串行执行，只会多耗一次模型调用，与改动前的作者行为一致，不涉及越权。
- 验证：`uv run ruff check .` 通过；SQLite 24 个受影响文件逐文件运行（e4g 55、e4f 41、analysis_execution 128、agent_tools 54、agent_api 11、agent_runtime 20、tasks 32、chapter_claim 三个文件、分析/记忆/富文档/批量/拆书身份/E6 等），全部通过，0 failed；PostgreSQL 4 个文件逐文件单进程运行（e4g 5、analysis_execution 21、e4f 7、editor_proposal 2）全部通过。未修改生产代码。

## 进度记录：2026-11-12 E4-g2 批量协作入口

范围：只开放批量顺序生成（task type `chapter.batch`，HTTP 入口 `POST /projects/{pid}/batch-generate`；预览 `POST /projects/{pid}/batch-generate/preview` 同按 `ai.use`）。批量的重试（`/tasks/{id}/retry`，受控原模型预检）、继续（`/tasks/{id}/resume`）与提前结束沿用任务路由，对本人同轮次开放。大纲、实体、向导、知识库索引、Agent 仍失败关闭；任务列表/SSE 属 E5 未动。

做了什么：
- `BatchHandler` 声明 `admission_preflight_aware` 与 `collaboration_execution_ready`。HTTP 入口改为 `batch_service.create_batch_task`：锁外短读 `require_project(ai.use)`（viewer 403、待接受/非成员/已撤权/仅管理员 404）+ `preflight_project_model`（本人优先，仅本人无文本配置且 `allow_owner_ai` 时回退作者，本人配置无效不回退）+ `prepare_task_admission`（DNS 在锁外），再交中央 `create_task` 消费证明。`validate_start` 改为接收中央已接受的 `accepted_model`，按原 grant 锁后重验并在同一屏障冻结共享向量选择，不再持锁做 DNS；`validate_retry` 要求原任务模型绑定（含邀请轮次）与受控重试接受的绑定逐字一致；`revalidate`（继续/重试/富文档采纳继续）对协作者按原 grant 重验，不按本人重新解析配置。
- 执行链路：`build_batch_job` 去掉 grant 拒绝，协作者先 `require_write_execution`（令牌、accepted editor、原邀请轮次、作者模型授权）；`_batch_bindings` 要求模型与共享向量绑定携带与任务相同的 grant。新增 `_lock_round_parent`：所有断点/占用写（`_require_batch_round`：每章 attempt 开始、阶段推进、分析登记/失败；`_sync_pending_review`）对协作者按原 grant 锁后重验 ai.use、原令牌与作者模型授权，失效统一为受控 `permission_denied`（`CollaborationSendBlocked`），不计作单章失败、不受 `skip_on_fail` 影响、不进入下一章。每章抢占（`claim_batch_chapter` 的 `_authorize`）、每次发送（`for_task` + `BatchAttemptIdentity`）、每章保存（`require_task_write`）与事件发布（`require_write_execution` + `verify_event`）沿用既有 grant 感知原语。
- 逐章分析：`analysis_service._valid_execution` 允许批量执行带 grant（批量分析不带提案来源）；分析在本任务内以真实 actor 与受理时冻结的模型（含费用来源）执行，写回仍在 `_analysis_write_barrier` 锁内按原 grant 重验；记忆只写关键词单元，`enqueue_memory_for_execution` 对 grant 不建 `kb.index`，作者向量零调用（E6 未开放）。批量本身不派生独立分析任务（逐章分析即“派生分析”，归真实触发者、占用本任务并发名额）。
- 引擎 `TaskEngine._complete_from_batch_checkpoint`（批量断点受限收尾）：执行体内存快照无法证明（撤权复查/用户取消落在某章保存提交之后、断点同步之前，`on_abort` 的 `_sync_released` 不一致，修复前任务永久 running 并标记项目清理失败），或全部章节已持久化而完成事件/`verify_finalization` 因撤权、改密、关作者模型授权失败时，不要求当前授权，只按原 UUID/轮次/执行键 + running + 无管理员意图 + 原输入逐字一致 + 断点 `current` 为空且没有章节仍持本任务占用 CAS；断点含 `_batch_completion` 时经 `_require_resume_receipt` 锁内核验其属于本轮执行与对应版本/日志。全部步骤已持久（cursor == 总数、phase generate、无待分析收据）→ `succeeded`（`BATCH_RECEIPT_FINALIZED_NOTE`，result 由持久断点 `summarize` 重建）；否则以 `_run` 判定的停止终态（failed/cancelled/interrupted，原因取 on_abort 之前的错误）结束并保留断点；待审核点无法发布 → failed。只改原 Task、释放 `batch:{project}`，不写正文/设定/记忆、不派生；成功后清除本次登记的项目清理失败快照。
- Agent：批量改为受控受理后，作者 Agent 的 `start_task(chapter.batch)` 只能经签名提案确认的 `execute_admitted`（新增端到端用例证明作者行为保持）；至此 Agent 白名单内全部任务类型的通用 `execute` 都失败关闭。

测试：
- 无 batch 相关 strict xfail 需转正（全仓 grep 无）。
- 新增 `tests/test_collaboration_batch_entry_e4g2.py`（SQLite，54 项）：editor 批量用本人模型、Task/日志/分析归真实 actor、费用来源（`model_source`/`model_config_owner_id`）、授权后回退作者模型且任务详情不泄露作者配置 ID、作者向量已授权仍零 `kb.index`；无模型无授权 400、本人配置损坏 400 不回退、不能选作者配置；作者私人风格不可选、不进提示词；viewer/admin+viewer 403 与 pending/非成员/已撤权/仅管理员 404（预览与发起均零任务零调用）；受理后领取前 ×{撤权、viewer、PATCH 关模型授权、直接关模型授权不递增 revision、停用、重邀}；领取后构建前/首章发送前 ×{撤权、viewer} 零发送；发送后保存前 ×{撤权、viewer、关模型授权}×{本进程通知、无通知} 不写正文；两章之间 ×{撤权、viewer、直接关模型授权、停用、重邀}×{通知、无通知}：第一章保留、第二章零发送、断点 cursor=1 无占用、作者可立即续发；某章提交后断点同步前撤权（通知/无通知）→ 断点受限收尾为 failed、无项目清理失败残留；逐章分析前撤权 → 无分析行；全部完成后完成事件/终态核验前 ×{撤权、viewer、触发者改密} → succeeded（note）+ 重建结果；篡改完成收据 → 不收尾、保留 running 屏障；同轮次重试从断点继续、重邀后旧任务重试 400；继续待审核批量与降级后继续被拒；作者批量不变、作品级批量互斥 409；作者 Agent 经提案确认发起批量。
- 新增 `tests/test_collaboration_batch_entry_e4g2_postgresql.py`（PG，6 项，执行引擎在另一实例）：章节保存事务先持锁 → 撤权阻塞至提交、第一章保留、下一屏障失败关闭；撤权先提交 ×{撤权、viewer、直接关模型授权} → 保存锁内重验失败、零写入、资源释放；部分完成后 ×{某章提交后进程内复查取消、完成核验前撤权} → 不停在 running（failed 保留收据 / succeeded 重建结果）。
- 负对照：`_complete_from_batch_checkpoint` 直接返回 None → 8 个收尾用例中 7 个失败（另一个走正常路径）；协作者 `_lock_round_parent` 只取门禁不重验 → 两章之间无通知 4 项及分析前撤权失败。
- 既有测试调整（不放宽）：`test_agent_tools.py` 两项原用“仍走通用执行的批量”验证，改为断言批量通用执行失败关闭且不建任务/不提交外层事务；`test_agent_providers.py`（4 项）、`test_skill_generation.py`、`test_skill_inputs.py` 的批量预览用例补绑定原认证 actor（与真实 runner 一致），断言不变。

验证：
- `cd backend && uv run ruff check .`：All checks passed。
- SQLite 逐文件（3 路并行）46 个受影响文件 + 哨兵（含 test_tasks、test_batch_generate 27、test_batch_chapter_claim 55、test_chapter_claim_* 、e4f 41、e4g 55、E4 撤权 22、agent_api 11、agent_runtime 20、agent_tools 54、recycle_bin_tasks 12、collaboration_analysis_execution 128、task_receipt_deletion_guard 83 等）：首轮除上述 4 个文件的 6 个预期调整用例外，另有 `test_tasks.py` 1 项一次性失败（负载下，未取到用例名），其余全部通过；调整后复跑 3 文件全过，`test_tasks.py` 之后两次复跑均 32 passed，新文件在 3 路并行负载下连跑两次均 54 passed。
- PostgreSQL 16.15（隔离 collab_test，逐文件单进程顺序）11 个文件：新文件 6、batch_chapter_claim 7、review_claim 4、chapter_claim 3、chapter_claim_author 2、analysis_execution 21、ai_guard 4、e4f 7、e4g 5、E4 撤权 2、receipt_deletion_guard 18，全部通过。未跑全部 PG 文件。

遗留 / 偏差：
- 停止终态：授权失效使批量以 `failed`（固定协作文案）结束并保留断点，与“单章失败即停止、重试从断点继续”的批量语义一致；未新增“部分成功”状态。全部章节已持久后才失效时记 succeeded（结果未发布、带 note），与 E4-f/E4-g1 收据收尾一致；但若停止发生在“完成事件核验通过之后、终态写入前”的用户取消（非授权）仍按 cancelled（只对 failed 做成功折算）。
- 账号停用后终态写入无法以原身份认证，引擎走既有无正文系统收尾，错误为通用固定文案「任务执行已停止」，不是「账号状态已变化」。
- 排队中的协作重试批量若复制了带 `_batch_completion`/`rich_analysis` 的断点，撤权后按 E4-a 既有规则保留原屏障（不受限收尾），会占用 `batch:{project}` 直到管理员处理。
- 断点受限收尾的终态帧同 E4-f 为不含任务快照的 error（帧发布身份留 E5）；协作者批量 SSE 仍 404（E5）；前端批量按钮按 `can_use_ai` 显示的核对未做。
- 批量审核点的富文档提案对协作者是否可采纳未专门覆盖（采纳端沿用既有授权检查）。

设计取舍：
- 批量与单章/分析同样要求 admission-aware，作者 Agent 批量随之只走签名提案路径，少一个未受控入口；代价是无提案来源的旧批量提案确认时失败关闭。
- 授权屏障统一放在断点/占用写（每章之间必经）而不是只靠发送守卫：两章之间撤权时连第二章的抢占都不会发生，`skip_on_fail` 也不会把授权失效当成单章失败继续跑后续章节。
- 收尾证明只依赖持久断点（原执行 CAS 写入）与完成收据，不依赖执行体内存快照；无法证明时宁可保留 running 屏障。

### E4-g2 复核结论（2026-11-12，check）

- 越权：预览/发起锁外按 `require_project(ai.use)` 短读后交中央受控受理；重试路由要求原令牌轮次 + 原模型绑定逐字一致，`validate_retry` 另校验 `task.user_id`；继续/提前结束经 `_lock_user_task_project(active_grant_required=True, expected_grant=…)`，非同轮次 editor 404。未发现旁路。
- 撤权时点：构建前 `require_write_execution`，每次断点/占用写经 `_lock_round_parent` 按原 grant 重验（锁序 认证/User → 门禁 → 作品，令牌在已锁 User 上补核），发送/保存/分析/发布沿用既有原语；`_sync_released` 不取授权屏障，撤权后仍可回退原 attempt。新增 `test_skip_on_fail_does_not_swallow_revocation`（发送中/两章之间 × 通知/无通知）：`skip_on_fail=True` 时停在原位置（`step_cursor` 不前进），后续章节 pending、零发送。
- `_complete_from_batch_checkpoint`：只认原 UUID/轮次/执行键 + running + 无管理员意图 + 原输入逐字 + `current` 为空 + 无本任务章节占用；`_batch_completion` 经 `_require_resume_receipt` 锁内核验；succeeded 仅在 cursor==总数、phase generate、无待分析收据时。断点只能由本执行的 CAS 写入或重试时继承，不接受客户端输入。伪造收据保留 running（与 recover 对 `_batch_completion` 的 `batch_receipt_proof` 规则一致，交恢复/管理员核对）。
- 作者路径影响（有意、与 E4-f/E4-g1 收据收尾一致，已补规范 error-handling.md）：①作者用户取消落在保存提交与断点同步之间，修复前永久 running，现为 cancelled 并保留收据，重试从分析阶段续跑、不重写已存章节——新增作者端回归 `test_author_cancel_after_chapter_commit_before_checkpoint_sync`，负对照（收尾函数直接返回 None）超时失败；②作者全部章节已持久而仅完成发布核验失败（如可见性版本变化）时由 failed 改为 succeeded + note。作者普通批量、作品级互斥、Agent 签名提案路径测试均保持。
- 4 个既有测试调整复核：`test_agent_tools` 原“通用 execute 建任务/外层事务”断言改为“通用 execute 失败关闭、零任务、外层不提交”，属于更严格的安全行为；签名提案正路径由 e4g2 新用例与 `test_agent_api::test_start_task_is_confirmed_and_can_be_cancelled` 覆盖。providers/skill 两文件只补绑定原认证 actor，断言没有放宽。
- `test_tasks.py` 偶发失败已定位：`test_serial_key_same_key_serial_other_keys_parallel`。原因是 FakeLLM 按调用顺序消费 push，负载下 B1 先于 A1 发送并拿走 `gate_a1`，于是“释放 A1”实际放行了 B1，C1 随后合法领取并拿到 `gate_a2`。这是测试自身的排程竞争，引擎调度没有问题，与本片无关。修正：A1 到达闸门后再提交 B1/C1，语义断言不变；连跑 4 次通过。
- 回归（精简策略）：ruff 通过；SQLite 逐文件（3 路并行）39 个文件共 1 055 项全部通过（含 e4g2 59、batch_generate 27、batch_chapter_claim 55、e4f 41、e4g 55、analysis_execution 128、receipt_deletion_guard 83、tasks 32、agent 四件、skill 两件、rich_document 15 件）；PostgreSQL 逐文件顺序 9 个文件共 51 项全部通过（e4g2 6、batch_chapter_claim 7、chapter_claim 3、chapter_claim_author 2、e4f 7、e4g 5、E4 撤权 2、receipt_deletion_guard 18、admin_chapter_claim_cancellation 1）。
- 遗留：同 E4-g2 记录，未新增。

## 进度记录：2026-11-12 E4-g3a 大纲协作入口

范围：只开放大纲 AI（task type `outline.continue` / `outline.expand`，HTTP 入口 `POST /projects/{pid}/outlines/continue`、`POST /projects/{pid}/outlines/{oid}/expand`）。重试沿用任务路由，对本人同轮次开放。实体 AI、开书向导、知识库索引、Agent（协作者）、`book.import` 仍失败关闭；任务列表/SSE 属 E5 未动。大纲没有同步预览入口。

做了什么：
- 入口 `outline_pro._create_outline_task`：按 E4-g2 模板，先校验请求认证快照（会话无待写对象），结束请求事务，再在锁外短读 `require_authenticated` + `require_project(ai.use)`（viewer 403；待接受/非成员/已撤权/仅管理员 404），之后做 `preflight_project_model`（本人模型优先；仅本人无文本配置且 `allow_owner_ai` 时回退作者；本人配置无效时不回退）和 `prepare_task_admission`（DNS 在锁外），最后交中央 `create_task` 消费证明。
- `outline_ai_service.validate_outline_task`：`get_owned_project(lock=True)` 改为 `require_project(ai.use, for_update, visibility_write, grant_identity=接受绑定的 grant)`，并核对作品作者与绑定一致。章纲读取不再经过作者/管理员专用的 `plan_service.get_project_outline`，改为新增的 `_project_outline`（调用方已鉴权，只按作品过滤）。`OutlineHandler.validate_and_claim` 增加检查：冻结的共享向量选择，其 grant 和作者必须与文本绑定一致。`validate_retry` 去掉 `grant_identity is not None` 拒绝，作者 ID 取自中央接受的原绑定，再由锁内重验核对。
- `OutlineHandler.collaboration_execution_ready = True`。`build_job` 去掉 grant 拒绝：协作者先 `require_write_execution`（令牌、accepted editor、原轮次、作者模型授权），并立即结束该写事务；后续检索前的 `require_task_write` 和发送 lease 会另开事务，SQLite 下持写锁跨越它们会自锁。之后按原 grant 读取作品，并核对两份绑定的 grant 与执行身份一致。`build_outline_job` 经 `_job_project` 取作品：作者（含无执行身份的直调）仍走严格的 `get_owned_project`，协作者只按原 grant 读取。因此 `execution is None` 时的普通 `guard_provider` 直调分支，协作者无法进入。
- 检索：`_retrieval` 对协作执行不加载作者向量快照，也不构造向量 embedder，只做关键词检索，作者向量模型零调用（E6 前保持）。即使作者已授权共享向量，冻结的选择仍随文本 guard 的 extra_bindings 逐次重验，授权失效时失败关闭。
- 撤权时点（不新增原语）：领取前走 E4-a 引擎复核；构建前、检索前 `require_task_write`；每次发送走 `for_task` lease（展开的第二批同样适用）；逐帧经 `stream_guard` 和 `verify_event`；终态由 `verify_finalization` 做 `require_task_write`。
- 收尾（无收据，理由）：大纲任务只产出草稿，唯一持久化就是 Task 终态和 `result`，二者在同一终态事务内经锁内重验后写入，不存在“已提交、待证明”的业务副作用，所以不需要也没有设计完成收据。**真实缺陷**：完成事件已发布、终态事务开始前授权变化（撤权/降 viewer/关模型授权/改密），`verify_finalization` 或终态认证会抛错，引擎落入“任务结束状态保存失败”分支，任务永久停在 running，占住 `outline:{pid}`/`expand:{oid}`，项目还被记入清理失败。修复：引擎新增 `_DRAFT_ONLY_KINDS`，在 `_finalize` 的未提交分支里按原 UUID/轮次/执行键把任务改记为 `failed`、`result=None`，文案为「任务输出授权已变化，已停止」（改密时用既有的无正文系统收尾文案），并释放资源、不登记清理失败。先按原 UUID 重读终态事实，所以已提交的成功不会被改写。`recover()` 对遗留 running 的大纲任务本来就走通用 interrupted 收尾，未改。这对作者同样生效：作者在该窗口改密时，以前卡在 running，现在记 failed。
- Agent：大纲原本就是 `admission_preflight_aware`，作者 Agent 只能经签名提案确认的 `execute_admitted` 受理。本片补了端到端用例，并在 `test_agent_tools` 中补充了通用 `execute` 对 `outline.continue`/`outline.expand` 失败关闭的断言。协作者 Agent 仍关闭。

测试：
- 无大纲相关 strict xfail（全仓 grep 无结果）。关闭态断言改写（不放宽）：`test_collaboration_outline_ai.py::test_editor_outline_ai_stays_closed_until_task_identity_wiring_is_complete` 改名为 `test_editor_outline_ai_admission_boundaries_never_touch_owner_vectors`：editor 无本人模型且未获授权 → 400，viewer → 403，作者对外书章纲 → 404，零任务、零模型/向量调用，作者旧向量键不变。成功路径移到新文件。`test_collaboration_task_revocation_e4.py` 的 `blocking_outline` 夹具原先就临时声明就绪，现为冗余但无害，保留。
- 新增 `tests/test_collaboration_outline_entry_e4g3.py`（SQLite，55 项）：
  - 模型、归属与隐私：editor 续写/展开用本人模型；Task 归真实 actor；`_project_model.source/config_owner_id` 记录费用来源。作者已授权共享向量、且作者共享向量空间里存在可召回记忆时，向量调用仍为零，也不派生任务。授权后回退作者模型时，任务详情不泄露作者配置。无模型无授权、本人配置损坏、显式选作者配置均为 400/404，且零任务。作者私人风格与模板不进入协作者提示词，作者正对照仍包含。
  - 受理边界：viewer/admin+viewer → 403，pending/非成员/已撤权/仅管理员 → 404（续写和展开都零任务、零调用）；协作者展开外书章纲 → 404。
  - 撤权时点：
    - 受理后、领取前 × {撤权、viewer、PATCH 关模型授权、直接关模型授权不递增 revision、停用、重邀}；
    - 领取后、构建前，以及构建后、首次发送前 × {撤权、viewer、直接关模型授权}：零发送；
    - 构建前撤权且无本进程通知；
    - 流中 × {撤权、viewer、关模型授权} × {通知、无通知}：撤权后的分片不落库、无结果；
    - 展开两批之间 × {撤权、viewer、直接关授权、停用、重邀} × {通知、无通知}：第二批零发送；
    - 完成事件发布前 / 终态事务前 × {撤权、viewer、直接关授权、改密}：failed、无 result、无 running 残留、无清理失败。
  - 每个失败用例都核对资源键已释放（作者可立即发起并成功）。
  - 重试与作者路径：同轮次重试成功，重邀后旧任务重试 400 且零调用，降级后重试被拒；作者续写/展开不变，与协作者同资源键 409 互斥；作者 Agent 经签名提案确认后执行并产出草稿。
- 新增 `tests/test_collaboration_outline_entry_e4g3_postgresql.py`（PG，4 项，引擎在另一实例、无本进程通知）：
  - 终态事务先持锁（锁内重验已通过）→ 撤权被阻塞到提交，草稿保留为 succeeded；
  - 撤权先完整提交 × {撤权、viewer、直接关授权} → 终态锁内重验失败，改记无结果 failed，资源释放，作者可立即续写。
- 负对照（逐个临时修改后还原，校验和与修改前一致）：
  1. 去掉 `verify_finalization` 的 `require_task_write`：SQLite 终态前 3 项失败（改密一项仍被终态认证拦下，符合预期），PG 撤权先提交 3 项失败；
  2. 关闭引擎草稿终态收尾：SQLite 4 项超时失败，PG 3 项失败；
  3. 放开协作者向量检索：关键词检索用例失败，出现作者向量查询嵌入。首版用例只靠空索引，负对照没有检出，已加强为在作者共享向量空间里预置记忆后再检；
  4. 去掉入口锁外 `ai.use` 短读：非编辑者矩阵 6 项仍通过，因为 `preflight_project_model` 自身按 ai.use 返回 403/404，短读属纵深防御，与批量入口一致。构建前的屏障同样有多层（`require_write_execution`、按 grant 读取作品、检索前 `require_task_write`、发送 lease），单独去掉其中一层不会让用例失败，零发送的最终保证来自 lease。

验证：
- `cd backend && uv run ruff check .`：All checks passed。
- SQLite 逐文件（3 路并行）33 个受影响文件和哨兵全部通过，0 failed：e4g3 55、collaboration_outline_ai 41、outline_pro 16、outline_snapshots 4、outline_transfer 7、tasks 32、planning_access 14、e4f 41、e4g 55、e4g2 59、E4 撤权 22、agent_api 11、agent_runtime 20、agent_tools 54、agent_providers 59、agent_derived_task_admission 11、recycle_bin_tasks 12、sqlite_cancel_rollback 1、style_privacy 7、setting_ai_context 12+1 skipped、preflight_engine_identity 15、task_review 12、task_privacy 15+1 xfailed（既有 E5）、retry_admission_api 5、sse_engine_http 1、execution_send_guard 23、ai_guard 85、event_guard 6、execution_identity 21、receipt_deletion_guard 83、batch_generate 27、skill_generation 8、skill_inputs 15。最后两处非功能清理（去掉冗余判断、模块说明）之后，大纲相关 4 个文件复跑全部通过。
- PostgreSQL 16（隔离 collab_test，逐文件单进程顺序）7 个文件全部通过：e4g3 4、collaboration_outline_ai 1、ai_guard 4、E4 撤权 2、e4f 7、e4g 5、e4g2 6。未跑全部 PG 文件。

遗留 / 偏差：
- 协作者大纲检索只用关键词，即使作者授权了共享向量也一样，因而与章节入口（E4-f 授权后用作者向量查询）不一致；这是按本片要求的保守取舍，E6 再统一。
- 终态前授权变化时模型调用已经发生（费用已产生），草稿不发布、不保存，用户需重新发起。失败关闭，不按“结果已生成”记 succeeded。
- 展开的协作者检索用例只对续写有效：展开按章纲之前的章节召回，第 1 章纲没有候选。
- 协作者 SSE 仍为 404（E5）；前端大纲 AI 按钮按 `can_use_ai` 显示的核对未做。
- 草稿采纳（`/outlines/append`、`PUT /outlines/{oid}/plans`）是人工写入，沿用阶段 D 的 `content.edit`，本片未改。

设计取舍：
- 不为大纲设计完成收据，而是在引擎里对“只产出草稿”的类型做受限失败收尾：没有可证明的已提交副作用，硬造收据只会增加伪造面。失败终态不需要证明（不发布结果），只需按原执行 CAS。

### E4-g3a 复核结论（2026-11-12，check）

- 越权：入口锁外 `require_project(ai.use)` 短读 + `preflight_project_model` + `prepare_task_admission`，中央受理事务内 `validate_outline_task` 按接受绑定的 grant 以 `for_update + visibility_write` 重验，并核对作品作者；`require_project` 已经过滤 `deleted_at`/生命周期，`_project_outline` 只按已鉴权的作品过滤，因此读不到外书或回收站作品的章纲（外书 404 有用例）。`_job_project` 在作者路径（含直调）仍严格使用 `get_owned_project`，协作者按原 grant 读取。重试要求 `task.user_id` 与本人一致，原绑定和中央接受的同一轮次绑定逐字一致，重邀后返回 400。viewer、待接受、非成员、已撤权、仅管理员（含 admin+viewer）均为零任务、零模型调用。未发现旁路。
- 撤权时点：领取前（E4-a）、构建前（`require_write_execution`，随即结束事务）、检索前（`require_task_write`）、每次发送（`for_task` lease，含展开第二批）、逐帧（`stream_guard`/`verify_event`）、终态前（`verify_finalization`）均失败关闭，与用例矩阵对得上。
- `_DRAFT_ONLY_KINDS` 收尾：只在终态写入抛异常、事实重读未确认提交、`status == succeeded`、无待审提案、有执行身份且类型为 `outline.*` 时进入。`complete_state` 对原 UUID/`queued_at`/执行键/step_cursor/input/checkpoint 原样 CAS 且要求 `running`，所以已提交的成功不会被改写（重读失败时 CAS 也只会落空）。`abort_ok=True` 会清除执行键，`outline:{pid}`/`expand:{oid}` 随之释放。其他类型不进入该分支。有管理员意图时按原 CAS 记 `failed` 并清除标记，不会伪造 cancelled，和普通失败终态一致。`recover()` 不经过该分支，遗留 running 仍走通用 interrupted。
- **记录偏差（已修正规范）**：作者路径的行为变化不止“改密”一项。任何让作者 `verify_finalization`/终态认证抛错的变化（作者改密、AI 可见性版本/代次变化、作者模型绑定变化、作品进入回收站、SQLite 门禁限时忙）都由“停在 running 并登记清理失败”改为无结果 failed。新增 `test_author_finalization_window_change_fails_closed[actor_token|visibility]`；负对照（临时关闭草稿收尾分支）两项均失败，恢复后 sha256 与修改前一致。error-handling.md 已改写为准确范围。这些变化都是失败关闭方向，可以接受；代价是草稿因 SQLite 短暂锁忙丢失，但比永久 running 好。
- 小修：草稿收尾提交后补上 `prune_finished`（普通终态路径一直有这一步），裁剪失败只记日志，不影响已提交的终态。
- 检索不一致的判定：设计 §6.2 写的是“授权开启后协作者**可**使用作者项目向量模型”，属于允许而不是强制。硬约束是“未授权零调用、不覆盖共享索引”，只用关键词完全满足；E4-g1/E4-g2 的分析派生记忆也采用同样的保守口径。按 implement.md 的 E6 条目和设计 §6.2 第 161 行“所有检索入口都遵守本段”，统一切换到冻结的共享向量选择属于 E6 交付，本片保持现状。已在 retrieval.md 写明理由，以及 E6 需要切换并翻转的用例（`test_editor_outline_uses_own_model_and_keyword_retrieval` 的零向量断言）。冻结选择仍作为 extra_bindings 逐次重验，所以作者修改向量配置或授权时，协作者的大纲任务也会失败关闭（与章节入口一致，偏严）。
- 模型、费用、归属、私人风格/模板：本人优先，仅本人无配置且已授权时回退作者；本人配置无效时不回退；显式选择作者配置返回 400/404。`_project_model.source/config_owner_id` 记录来源；任务详情不泄露作者配置；作者私人风格和模板不进入协作者提示词（有正对照）。与 E4-f 一致。
- 调整的测试：`test_collaboration_outline_ai` 的关闭态用例改为受理边界用例，仍断言零任务、零模型/向量调用，以及作者旧向量键不变；编辑者对外书展开时先被 400（无模型）拦下，外书 404 由 e4g3 的专项用例覆盖。`test_agent_tools` 只追加了大纲通用 execute 失败关闭的断言。均未放宽。
- 回归（精简策略）：ruff 通过。SQLite 逐文件（3 路并行）18 个文件共 506 项全部通过（e4g3 57、collaboration_outline_ai 41、outline_pro 16、outline_snapshots 4、outline_transfer 7、tasks 32、collaboration_planning_access 14、planning_access 1、e4f 41、e4g 55、e4g2 59、E4 撤权 22、agent_api 11、agent_runtime 20、agent_tools 54、agent_providers 59、recycle_bin_tasks 12、sqlite_cancel_rollback 1）。PostgreSQL 逐文件顺序 4 个文件共 14 项全部通过（e4g3 4、collaboration_outline_ai 1、E4 撤权 2、e4f 7）。
- 遗留：同 E4-g3a 记录；另外，草稿收尾失败时 `output_text` 保留截至最后一个已核验帧的文本，这些内容在授权有效时已经逐帧发布过，与其他失败路径一致，没有新增泄露。

## 进度记录：2026-11-12 E4-g3b 实体协作入口

范围：开放实体 AI 的五类任务（`character.generate` / `organization.generate` / `career.generate` / `relations.parse` / `relations.generate`，HTTP 入口 `POST /projects/{pid}/characters/generate`、`/organizations/generate`、`/careers/generate`、`/relationships/generate`、`/characters/{cid}/parse-relations`）。实体没有同步预览/同步生成入口。重试沿用任务路由，对本人同轮次开放。开书向导、知识库索引、Agent（协作者）、`book.import` 仍失败关闭；任务列表/SSE 属 E5 未动。

做了什么（全部复用 E4-g3a 的 helper 与模板）：
- 入口 `entity_ai_service.create_entity_task`：去掉作者专用 `get_owned_project`，改为 E4-g3a 入口同一套：校验请求认证快照（会话无待写对象）→ 结束请求事务 → 锁外短读 `require_authenticated` + `require_project(ai.use)` → `preflight_project_model`（本人优先；仅本人无文本配置且 `allow_owner_ai` 时回退作者；本人配置无效不回退）→ `prepare_task_admission`（DNS 在锁外）→ 中央 `create_task` 消费证明。
- `validate_entity_task`：`get_owned_project(lock=True)` 改为 `require_project(ai.use, for_update, visibility_write, grant_identity=接受绑定的 grant)` 并核对作品作者；`relations.parse` 的主体角色改用新增 `_project_character`（只按已鉴权作品过滤），不再经作者专用 `story_service.get_owned`。绑定校验放宽为：作者只允许 `source=actor`、无 grant；`source=owner` 只属于带 grant 的协作者。冻结的共享向量选择 grant/作者须与文本绑定一致（新受理与重试两路）。
- `EntityHandler.collaboration_execution_ready = True`。`validate_retry` 去掉 `_collaboration_grant` 拒绝，作品作者取自中央接受的原绑定，`task_grant` 取原 Task。`build_job` 去掉 grant 拒绝：同一短会话内 `require_write_execution` 后经共用 `project_service.get_execution_project` 读作品（作者严格 `get_owned_project`，协作者按原 grant `require_project(ai.use)`），两份绑定与执行身份 grant 逐项核对；会话随即结束。
- 共用 helper：E4-g3a 的 `outline_ai_service._job_project` 提升为 `project_service.get_execution_project`，大纲改为委托（行为不变），实体 `build_entity_job` 同样使用；因此 `execution is None` 的普通 `guard_provider` 直调分支对协作者仍是严格作者 404（`test_author_direct_build_still_requires_ownership`）。`build_entity_job` 的模型绑定检查改为按执行 grant 判定；作者配置回退时只按 `config_owner_id` 核对配置实例/指纹。
- 检索：`_knowledge_for_execution` 对协作执行先以文本 guard（extra 绑定冻结共享向量选择、含已采集的 `EntitySourceSnapshot`）`require_task_write` 锁内重验并立即回滚，之后只做关键词检索（`embedder=None`），作者向量模型零调用（E6 前保持，同大纲）。作者执行不变。
- 撤权时点（不新增原语）：受理锁内（中央 + validate 两层）；领取前（E4-a 引擎复核）；构建前（`require_write_execution` + 按原 grant 读作品）；检索前（上条新增）；每次发送（`for_task` lease，含实体来源快照）；逐帧（`stream_guard`/`verify_event`，`done` 在锁内重算草稿）；终态（`verify_finalization` 的 `require_task_write` + 锁内重算）。
- 收尾选择：**草稿型，扩展 `_DRAFT_ONLY_KINDS`**（engine.py，加入五类实体）。理由：实体任务只产出草稿，唯一持久化是 Task 终态与 `result`，在同一终态事务内锁内重验后写入；保存设定需用户在前端确认后经 `characters/with-relations`、`organizations/with-members`、`careers/batch`、`relationships/batch`（阶段 D 的 `content.edit`）另行提交，不存在“已提交、待证明”的业务副作用，因此不设计收据（硬造收据只会增加伪造面）。失败终态不发布草稿、按原 UUID/轮次/执行键 CAS，释放 `relparse:{cid}`。`recover()` 对遗留 running 的实体任务本就走通用 interrupted 收尾，未改。

作者路径行为变化（如实列全，均为失败关闭方向）：
1. 完成事件已发布、终态事务前，任何令作者 `verify_finalization`/终态认证抛错的变化——作者改密、AI 可见性版本/代次变化、作者模型绑定变化、作品进入回收站、SQLite 门禁限时忙，以及实体特有的**发送后原提示词来源设定（角色/世界观/组织/职业/阶段/成员的已采集字段）被人工修改**（`entity_source_changed`）——由“停在 running、占 `relparse:{cid}`、登记项目清理失败”改为无结果 `failed`；文案为「任务输出授权已变化，已停止」（改密为既有「任务执行已停止」；来源设定被修改经复核改回「设定来源已变化，请重新发起」，见复核结论）。已用 `test_author_finalization_window_change_fails_closed[actor_token|visibility|entity_source]` 证实，负对照（去掉五类）三项均失败。
2. 其余经核对无行为变化：入口 404 语义（作者非拥有/仅管理员仍 404）；受理锁序——作者受理原本就在 `accept_preflight_project_model` 内经 `require_project(ai.use, for_update, visibility_write)` 取可见性写门禁（曾假设这是新增变化并写了用例，负对照显示改前同样调用，已删除该用例，未记为变化）；`relations.parse` 角色不存在仍是同文案 404；作者构建/直调仍严格 `get_owned_project`；作者模板/私人风格仍生效（正对照）。

Agent：实体三类原本就是 `admission_preflight_aware`，作者 Agent 只能经签名提案 `execute_admitted` 受理；补端到端 `test_author_agent_entity_confirmed_through_signed_proposal`，`test_agent_tools` 补 `organization.generate`/`career.generate` 通用 `execute` 失败关闭断言（`character.generate` 原已有）。`relations.*` 不在 Agent 白名单。协作者 Agent 仍关闭。

测试：
- 无实体相关 strict xfail（全仓 grep 仅 MCP/SSE/E6/令牌复用）。关闭态断言改写（不放宽）：`test_entity_ai_execution_identity.py::test_entity_editor_entry_remains_closed_until_full_guarded_path` → `test_entity_editor_admission_boundaries_never_touch_owner_vectors`：editor 无本人模型且未授权五入口均精确 400，降 viewer（即使开授权）均精确 403，零任务、零模型/向量调用，作者向量配置不变；原 `in (403, 404)` 改为精确码。`test_collaboration_style_privacy.py::test_job_prompt_builders_use_real_actor` 因大纲/实体改经 `get_execution_project` 读作品，monkeypatch 目标随之改为该 helper（断言不变）。
- 新增 `tests/test_collaboration_entity_entry_e4g3b.py`（SQLite，62 项）：五类成功（本人模型、真实 actor、grant、关键词命中作者共享资料但向量零调用、只产草稿不写设定不派生任务、撤权后历史不回滚）；作者模型回退记来源且详情不泄露；三类模型错误 × 五入口零任务；作者私人风格/模板不进协作者提示词（作者正对照）；viewer/admin+viewer 403、pending/非成员/已撤权/仅管理员 404 × 五入口；外书角色 parse/relations.generate/外书入口 404 不泄露；预检后受理前 × {撤权、viewer、重邀、关授权} 零任务；受理后领取前 × 6 种变化 failed 且 `relparse` 释放；领取后 × {build、retrieval、send} × {撤权、viewer、直接关授权} 零发送，build/retrieval 两阶段关键词检索也未执行；无通知构建前撤权；发送中 × 3 × {通知、无通知} 丢弃草稿；完成发布前/终态前 × 4 种变化 failed、无 running 残留、无清理失败、资源释放；作者终态窗口三项；重试同轮次成功、重邀后 400/409 零调用、viewer 不能重试；作者五类不变、`relparse` 作者与协作者 409 互斥；作者直调严格；作者 Agent 签名提案。
- 新增 `tests/test_collaboration_entity_entry_e4g3b_postgresql.py`（PG，4 项，双实例、无本进程通知，`relations.parse`）：终态事务先持锁 → 撤权被阻塞到提交、草稿 succeeded；撤权先提交 × {撤权、viewer、直接关授权} → 无结果 failed、`relparse` 释放、作者可立即解析。
- 负对照（备份到 /tmp 唯一命名文件，改后跑、还原，sha256 与改前一致）：
  1. `_DRAFT_ONLY_KINDS` 去掉五类：SQLite 7 项失败（协作者终态前 4 + 作者窗口 3；`done_event` 4 项仍过，因其走普通事件核验失败路径），PG 撤权先提交 3 项失败；
  2. 去掉检索前 `require_task_write`：retrieval 阶段 3 项失败（关键词检索被执行）；
  3. 放开协作者向量检索：成功用例 4 项失败（出现 `owner-embed` 查询嵌入；`relations.parse` 不检索故仍过）；
  4. 作者受理是否新取可见性门禁：去掉实体受理的 `visibility_write` 并关闭就绪标志后用例仍过（门禁原已由 `accept_preflight_project_model` 取得），据此删除该用例与“变化”记录。
  预检后受理前的用例是多层防御（中央 `create_task` 对已就绪 handler 也按原 grant 重验），未单独做去层负对照。

验证（精简策略，未跑全量）：
- `cd backend && uv run ruff check .`：All checks passed。
- SQLite 逐文件（3 路并行）29 个受影响文件与哨兵全部通过，共 833 项、0 failed：e4g3b 62、entity_ai_execution_identity 52、entity_tasks 6、relations_generate 31、career_tasks 11、relationships 20、collaboration_relationship_scope 9、collaboration_style_privacy 7、tasks 32、e4f 41、e4g 55、e4g2 59、e4g3 57、collaboration_outline_ai 41、outline_pro 16、E4 撤权 22、agent_api 11、agent_runtime 20、agent_tools 54、agent_providers 59、recycle_bin_tasks 12、sqlite_cancel_rollback 1、setting_ai_visibility 7、setting_ai_visibility_formats 9、setting_ai_context 12+1 skipped、ai_visibility_dispatch 19+1 skipped、agent_visibility 8、collaboration_ai_guard 85、preflight_engine_identity 15；另 collaboration_planning_access 14 通过。
- PostgreSQL 16（隔离 collab_test，逐文件单进程顺序）9 个文件全部通过：e4g3b 4、entity_ai_execution 6、e4g3 4、E4 撤权 2、ai_guard 4、e4f 7、collaboration_outline_ai 1、e4g 5、e4g2 6。未跑全部 PG 文件。

遗留 / 偏差：
- 协作者实体检索只用关键词（即使作者授权共享向量），与章节入口（E4-f）不一致，E6 统一时切换并翻转 `test_editor_entity_ai_uses_own_model_and_keyword_retrieval` 的零向量断言。
- 终态前授权或来源变化时模型调用已发生（费用已产生），草稿不发布不保存，需重新发起；作者在“发送后修改原设定”窗口内同样失去草稿（此前为永久 running，仍不可用）。
- 协作者 SSE 仍 404（E5）；前端实体 AI 按钮按 `can_use_ai` 显示与“草稿确认保存”在协作者下的端到端浏览器核对未做。
- 草稿保存（with-relations / with-members / careers/batch / relationships/batch）为人工写入，沿用阶段 D 的 `content.edit`，本片未改。

### E4-g3b 复核结论（2026-11-12，check）

- 越权：入口锁外 `require_authenticated` + `require_project(ai.use)` 短读 → `preflight_project_model` → `prepare_task_admission`，中央受理内 `validate_entity_task` 按接受绑定的 grant 以 `for_update + visibility_write` 重验并核对作品作者。`_project_character` 只在已鉴权的作品内查找（作品生命周期由 `require_project` 过滤），`_selected_for_generation`/`_relations_for_project` 均限定本书，组织关联经 `include_associations` 只能落到本书 scope 条目。viewer、待接受、非成员、已撤权、仅管理员（含 admin+viewer）零任务零调用。补测两项：`test_editor_entity_prompt_respects_owner_ai_visibility`（作者隐藏的 personality、never 条目档案不进入协作者 relations.generate / character.generate 提示词，未隐藏字段正对照在；显式选 never 条目、隐藏 relations_text 后解析均 400 `setting_not_available` 且零任务）和 `test_editor_entity_ai_rejected_once_project_is_recycled`（作品进回收站后五入口 404、零任务）。负对照：把 relations.generate 角色卡的 `hidden=` 置空后前者失败，还原后 sha256 一致。
- 撤权时点：受理、领取前、构建前、检索前、每次发送、逐帧、`done`、终态各层与记录一致，未发现旁路。
- `get_execution_project`：作者（含 `execution is None` 直调）严格 `get_owned_project`，协作者按原 grant；大纲委托后 outline 相关 6 个文件全部通过。
- `_DRAFT_ONLY_KINDS`：只含 outline.* 与五类实体；分支条件、按原 UUID 先重读事实、CAS 及 `abort_ok` 释放 `relparse:{cid}` 与 E4-g3a 复核一致，不误伤其他类型，已提交的成功不会被改写。
- **修正（文案）**：原实现中，作者“发送后人工修改原设定字段”在草稿收尾时的提示为「任务输出授权已变化，已停止」，不准确。作者本人改了设定，看到“授权已变化”会误以为权限出了问题；而完成事件核验路径对同一原因本来就给出「设定来源已变化，请重新发起」。现改为：`_finalize` 捕获终态异常，只在受控 code 为 `entity_source_changed` 时选用该固定文案，其余情况仍是「任务输出授权已变化，已停止」。不透传异常正文，协作者在完成事件路径上本来就能看到同一文案，所以没有扩大泄露面。用例 `test_author_finalization_window_change_fails_closed[entity_source]` 改为精确断言新文案（不是放宽）。负对照：去掉映射后该项失败，还原后 sha256 一致。error-handling.md 已同步。
- 一致性：模型本人优先、仅在本人无配置且已授权时回退作者并记来源，本人配置无效时不回退；任务归属真实 actor；作者私人风格与模板按 actor 解析；检索只用关键词、作者向量零调用。以上均与 E4-g3a 一致。
- 调整过的三个测试文件：关闭态用例改为精确的 400/403 断言，比原来的 `in (403, 404)` 更严；style_privacy 只是更换了 monkeypatch 目标；agent_tools 只追加了断言。均未放宽。
- 回归（精简策略）：ruff 通过。SQLite 逐文件（3 路并行）32 个文件共 860 项通过、2 项 skipped、0 failed，其中 e4g3b 64 项（含新增 2 项），另有 entity_ai_execution_identity、entity_tasks、relations_generate、career_tasks、relationships、relationship_scope、style_privacy、tasks、四个 setting/visibility 文件、agent_visibility、e4g3、collaboration_outline_ai、outline_pro/snapshots/transfer、e4f、e4g、e4g2、E4 撤权、agent 四件套、recycle_bin_tasks、sqlite_cancel_rollback、ai_guard、preflight_engine_identity、planning_access。PostgreSQL 逐文件顺序跑 9 个文件共 39 项全部通过：e4g3b 4、entity_ai_execution 6、e4g3 4、E4 撤权 2、ai_guard 4、e4f 7、collaboration_outline_ai 1、e4g 5、e4g2 6。
- 遗留：同 E4-g3b 记录。可见性收紧（`ai_visibility_changed`）落在终态窗口时，草稿收尾仍统一提示「任务输出授权已变化，已停止」，没有改成更具体的可见性文案：这样能和大纲保持一致，也不必改动已有断言。若要细化，应当大纲和实体一起改。


## 进度记录：2026-11-12 E4-g3c 向导协作入口

范围：开放开书向导四类任务（`wizard.outline` / `wizard.characters` / `wizard.world` / `wizard.careers`，HTTP 入口 `POST /projects/{pid}/wizard/{kind}`）；向导步骤进度 `PATCH /projects/{pid}/wizard-progress` 随之按 `content.edit` 开放（前端向导页对 editor 可用的前提，见 phase-g-project-ui 记录）。向导没有同步预览/同步生成入口。重试沿用任务路由（受控原模型预检），对本人同轮次开放；续写（`continue_from_task_id/key`）对本人**同一邀请轮次**的来源开放。刻意不开放：知识库索引、Agent（协作者）、`book.import`（本片范围外，保持失败关闭）；“新建作品”类入口（灵感转作品、拆书导入、备份导入）属账号级操作，不经向导路由，未改动，协作者不能借向导在作者作品外/下新建作品；作者 Agent 也不能发起向导（不在 `start_task` 白名单，预览与通用执行均失败关闭）。任务列表/SSE 属 E5 未动。

向导语义（与大纲/实体的根本区别）：四类步骤都**直接写库**——角色/世界观/职业整体替换（级联清掉旧关系、成员、挂接等），大纲覆盖（先存快照）或追加；不是草稿。跨任务状态有两种：`_wizard_pending`（出站前持久的本轮未决凭据）→ `_wizard_completion`（与内容替换同一事务提交的完成收据），以及续写来源绑定 `_wizard_resume`（指纹含来源 input，发送 lease 内重验）。

做了什么：
- 受理：新入口 `wizard_service.create_wizard_task`（同 E4-g3a/b 模板）：校验请求认证快照 → 结束请求事务 → 锁外 `require_authenticated` + `require_project(ai.use)` 短读（viewer 403；待接受/非成员/已撤权/仅管理员 404）→ `preflight_project_model`（本人优先；仅本人无文本配置且 `allow_owner_ai` 时回退作者；本人配置无效不回退）→ `prepare_task_admission`（DNS 在锁外）→ 中央 `create_task`。续写且未显式选模型时，短读本人来源任务原请求的 `ai_config_id` 作为预检配置（与 `resume_request` 原语义一致）。`WizardHandler` 声明 `admission_preflight_aware` 与 `collaboration_execution_ready`，新增 `validate_retry`（原绑定与中央接受绑定逐字一致、原轮次）；受理锁内 `lock_wizard_project` 按接受绑定的 grant 以 `for_update + visibility_write` 重验并核对作者（作者只允许 `source=actor`、`source=owner` 只属于带 grant 的协作者），再读续写来源、做覆盖预检；原 `validate_wizard`（锁内 DNS）删除。
- 续写：`load_resume_source(grant_identity=…)` 要求来源 grant 与本次执行逐字一致（作者只接无 grant 的本人任务，协作者只接本人同轮次任务，重邀后旧来源 400）；`ResumeBinding` 增加 `grant_identity`，`verify_resume_source` 对协作者来源按原 grant 重验 ai.use（作者仍按作品作者过滤）。
- 构建：`build_job` 去掉 grant 拒绝；协作执行在写入未决凭据**之前**先 `require_write_execution`（撤权不留凭据、零发送），作品经共用 `get_execution_project`（作者含直调仍严格 `get_owned_project`，协作者按原 grant）。`build_wizard_job` 去掉 grant 拒绝，绑定校验改为 `project_owner_id == 作品作者`、`grant == 执行 grant`；模型名/思考/输出上限取自原绑定锁内快照（`bound.*`，作者回退时协作者无权按本人读作者配置行），作者直调仍用 `resolve_config`。检索：协作执行先以 send guard（领取身份 + 原模型绑定 + 续写来源）`require_task_write` 锁内重验并回滚，再 `knowledge_text(embedder=None)` 只做关键词检索——不走作者旧兼容向量（`knowledge_for_project`），也不走续写分支的本人默认向量（`get_embedder`）。私人风格/模板：`project_info(actor_user_id=actor)` 与 `get_override(actor)` 本就按真实 actor，作者私人风格/模板不进协作者提示词（有作者正对照）。
- 保存/终态：`save_wizard_result` 去掉 grant 拒绝（锁内 `require_write_execution` + 模型绑定重验两层），收据写入同一事务时一并写 `output_text`（受限收尾可凭持久事实证明 stream_hash）；`verify_finalization` 对协作执行在终态事务内再 `require_write_execution`。
- 收尾选择（**写库型 → 收据收尾，不进 `_DRAFT_ONLY_KINDS`**）：理由——向导结果已随内容提交，不能像草稿那样“失败即不发布”；把已提交的内容记为失败会误导用户重跑（再次整体覆盖）。新增 `TaskEngine._complete_from_wizard_receipt` + `wizard_service.completed_wizard_receipt`：完成事件核验、取消或终态核验在收据提交后失败时（撤权/降级/关模型授权/改密/可见性变化），不要求当前授权，只认原 UUID/轮次/执行键 + running + 无管理员意图，锁作品 → Task 逐字核对收据字段、原输入摘要、模型绑定与原 grant（协作者按数据库作品作者）、结果摘要、输出全文摘要后记 `succeeded`（`WIZARD_RECEIPT_FINALIZED_NOTE`，result 取自收据），释放 `wizard:{pid}:{kind}`，不写内容、不发布、不派生；Hub 以不含快照的 error 关闭。收据破损/伪造或有管理员意图 → 保留 running 屏障。收据提交之前：`discard_wizard_pending` 改为系统清理（锁作品 → 原轮次 Task，逐字核对凭据后撤销，不要求当前授权；保存与凭据替换同事务且仅在保存从未尝试提交时调用，故安全），撤权后任务以失败结束而非永久 running。`recover()` 的“待核对”语义细化：可证完成收据 → succeeded；只有逐字可证的未决凭据 → `release_uncommitted_pending` 撤销后按普通中断收尾；无凭据/凭据破损仍待核对（`test_wizard_uncertain_cleanup.py` 三种孤立形态不变）。成功向导任务的删除/裁剪证明 `verified_wizard_terminal_receipt` 增加 `project_owner_id`，`has_unresolved_receipt` 从数据库取作品作者，协作者成功任务可证、可裁剪。
- 多步向导（四个步骤是独立任务）：中途撤权时已提交步骤保留（收据可证，撤权后不回滚），后续步骤的受理/领取/构建失败关闭。

作者路径行为变化（如实列全，均已用例证实）：
1. 发送中/保存屏障前作者改密：未决凭据改为系统清理，任务 failed 并释放资源；此前凭据清理要求原令牌而失败，任务永久 running、登记清理失败（`test_author_token_change_mid_send_fails_instead_of_staying_running`）。
2. 内容已提交、终态前令作者完成事件/终态核验失败的变化：由“停在 running、占 `wizard:{pid}:{kind}`、登记清理失败、启动后仍待核对”改为按收据 `succeeded` + 固定说明「向导结果已保存；执行授权或设置已变化，结果未发布」。已用例证实作者改密与 AI 可见性版本变化（`test_author_change_after_commit_finalizes_from_receipt[actor_token|visibility]`）；其余同类变化（模型绑定变化、作品进入删除流程、SQLite 门禁限时忙，以及保存确认丢失且即时确认失败而实际已提交）走同一分支，未逐一单测。
3. 重启遗留、只带可证未决凭据（内容未提交）的向导任务：由永久待核对改为 `interrupted`、可重试（`test_recover_releases_provable_uncommitted_pending[author]`）。
4. 重试：向导改为受控受理，重试沿用原任务固定的模型（此前按当时默认配置重新解析）（`test_author_retry_pins_original_model`）。
5. 受理顺序：锁外模型预检在前，模型错误先于续写来源/覆盖冲突报告（同为受理失败、零任务）（`test_author_model_error_now_reported_before_resume_source`）；DNS 不再在作品锁内解析。
6. `PATCH /wizard-progress`：除作者外，accepted editor 与具人工编辑权限的管理员可写（此前严格作者，管理员 404）（`test_wizard_progress_follows_content_edit`）。
其余经核对不变：作者成功路径、任务输入形状（未新增共享向量绑定）、作者检索（`knowledge_for_project` / 续写本人默认向量）、作者直调严格归属、作者模板与私人风格、作者与协作者同步骤 409 互斥。

测试：
- 无向导相关 strict xfail（全仓 grep 无）。关闭态测试改写（不放宽）：`test_wizard_collaboration_closed.py` 原“editor/viewer 一律 404”改为：viewer 四类精确 403、零任务零文本/向量调用；editor 携带伪造 `_collaboration_grant`（revision+7）/`_project_model={"source":"owner"}` 时，任务只带服务端锁内取得的真实轮次与本人模型绑定、只用本人密钥。`test_wizard_resume_identity.py::test_wrong_source_key_does_not_flush_pending_data` 因受理签名变为受控受理，改为在独立会话取得接受绑定后直调 `_claim`，断言（400、调用方暂存不被 flush）不变。`test_collaboration_style_privacy.py` monkeypatch 目标随向导改用 `get_execution_project`。`test_agent_tools.py` 追加向导四类预览/通用执行失败关闭断言。`test_series_projection_guards.py` 一项补建作者文本模型以触达系列保护（见回归）。
- 新增 `tests/test_collaboration_wizard_entry_e4g3c.py`（SQLite，83 项）：四类成功 × {作者共享向量空间、作者旧兼容向量空间}（本人模型、真实 actor、grant、内容写入、收据可证且可清理、关键词命中但向量零调用、不派生任务、撤权后已提交内容保留）；作者模型回退记来源、详情不泄露；三类模型错误 × 四类零任务；私人风格/模板（作者正对照）；viewer/admin+viewer 403、pending/非成员/已撤权/仅管理员 404；外书与回收站 404；向导进度权限；预检后受理前 × 4；受理后领取前 × 6；{build、检索、首次发送前} × {撤权、viewer、直接关授权}（检索阶段连关键词检索都未执行）；无通知构建前撤权（未决凭据从未写入）；发送中 × 3 × {通知、无通知}；保存事务前 × {撤权、viewer、直接关授权、停用} × {通知、无通知}（内容不变、凭据已清理、不停 running）；收据提交后 × {完成事件前、终态前} × {撤权、viewer、直接关授权、改密} 与本进程通知变体、无 revision 直接降级 → 收据收尾 succeeded；篡改收据保持 running 且 recover 拒绝；进程内收尾不可用时 recover 收尾；可证未决凭据 recover × {editor、author}；同轮次续写成功、作者不能接协作者来源、重邀后旧来源 400、editor 不能接作者来源；同轮次重试、重邀后旧任务重试被拒、viewer 不能重试；作者四类不变与 409 互斥；作者行为变化 1/2/4/5；作者直调严格；Agent 白名单。
- 新增 `tests/test_collaboration_wizard_entry_e4g3c_postgresql.py`（PG，5 项，引擎在另一实例、无本进程通知）：保存事务先持锁 → 撤权被阻塞至提交、内容保留（完成事件核验先后于撤权分别为普通成功/收据收尾）；撤权先提交 × {撤权、viewer、直接关授权} → 保存锁内重验失败、内容与收据都不写、凭据清理、资源释放、作者可立即发起；内容提交后完成事件前撤权 → 收据收尾 succeeded。
- 负对照（每次先把原文件备份为 /tmp/e4g3c/neg 下唯一命名文件，跑完立即还原，sha256 均与改前一致）：
  1. `_complete_from_wizard_receipt` 直接返回 None：SQLite 收据相关 13 项中 12 项失败（篡改用例本应保持 running，仍过），PG 2 项失败（含“保存先持锁”用例，说明该 PG 运行实际走到了收据收尾分支）。
  2. 保存事务只去掉协作者的 `require_write_execution`：8 项全过——模型绑定重验按当前能力重新推导 grant/角色/作者模型授权，独立拦下；两层同时去掉后 6 项失败（通知变体靠执行体取消仍过），PG 撤权先提交 3 项失败。两层各自充分。
  3. `discard_wizard_pending` 恢复为要求当前授权：15 项失败（发送中、保存前、作者改密）。
  4. 协作检索改走 `knowledge_for_project`：初版用例仅预置共享空间向量（作者旧路径因键不匹配退回关键词）未检出，已改为按向量空间参数化（共享/旧兼容各自全量预置），旧兼容空间 4 项失败。
  5. 去掉检索前 `require_task_write`：检索阶段 3 项失败（关键词检索被执行）。
  6. 去掉 `verify_finalization` 的协作 `require_write_execution`：终态前用例（含新增无 revision 直接降级）仍过——模型绑定重验同样拦下；该层为纵深防御，如实记录。
  7. `_release_wizard_pending` 直接返回 False：recover 未决凭据 2 项失败。
  8. 续写来源不核对 grant：同轮次续写用例失败（重邀后旧来源被接受）。
  9. 去掉 build_job 写凭据前的 `require_write_execution`：无通知构建前撤权用例仍过——随后的 `get_execution_project` 按原 grant 读作品先失败（未决凭据同样未写入），纵深防御。

验证（精简策略，未跑全量）：
- `cd backend && uv run ruff check .`：All checks passed。
- SQLite 逐文件（3 路并行）51 个受影响文件与哨兵：1190 passed、1 skipped、1 failed。唯一失败为 `test_series_projection_guards.py::test_wizard_queued_before_binding_cannot_replace_shared_projection`——作者无文本模型时，受理先报模型错误（400）而非系列保护 409，即作者行为变化 5；测试补建作者文本模型后断言（409 `series_setting_managed`、零覆盖）不变，复跑 9 passed。哨兵含 tasks 32、e4f 41、e4g 55、e4g2 59、e4g3 57、e4g3b 64、E4 撤权 22、agent_api 11、agent_runtime 20、agent_tools 54、agent_providers 59、recycle_bin_tasks 12、sqlite_cancel_rollback 1、style_privacy 7、agent_project_style_privacy 35、向导九件（wizard 30、resume 9、resume_identity 15、execution_identity 20、uncertain_cleanup 7、visibility_fencing 8、cross_project 9、collaboration_atomicity 8、collaboration_closed 1）、receipt_deletion_guard 83、retry_admission_api 5，以及 admin_projects、career_tasks、outline_pro/snapshots、inspiration、projects_and_story、planning/setting/kb/prompt/styles/book_import、task_event_guard/execution_identity/send_guard/statistics、log_private_views、planning_foreign_cascades、route_inventory 等。最终代码再跑 e4g3c 83 passed、agent_tools 54 passed；另补 endpoint_guard 32、ai_config_model_type 5、prompt_parity 60 全过。
- PostgreSQL（隔离 collab_test，逐文件单进程顺序）13 个文件 70 项全部通过：e4g3c 5、wizard_atomicity 2、wizard_dispatch 4、wizard_resume_identity 2、task_execution_identity 7、e4g3b 4、e4g3 4、e4g2 6、e4g 5、e4f 7、E4 撤权 2、receipt_deletion_guard 18、agent_project_style_privacy 4。未跑全部 PG 文件。

遗留 / 偏差：
- 协作者向导检索只用关键词（即使作者授权共享向量），与章节入口（E4-f）不一致，E6 统一时切换并翻转零向量断言；向导未冻结共享向量选择（保持作者任务输入不变），E6 需一并补上。
- 收据收尾的终态帧同 E4-f/g 为不含任务快照的 error（帧发布身份留 E5）；协作者向导 SSE 仍 404（E5）；前端向导页按 `can_use_ai`/`can_edit` 对 editor 的浏览器核对未做。
- 收据提交前授权变化时模型调用已发生（费用已产生），内容不保存，需重新发起。
- 无未决凭据（崩溃于凭据写入前）或凭据破损的遗留 running 向导仍需人工核对（既有测试固定的保守语义，未放宽）。
- `verify_finalization` 与 build 前两处新增的协作 `require_write_execution` 经负对照证明为纵深防御（其他层已独立拦截）。

### E4-g3c 复核结论（2026-11-12，check）

- 越权：入口（锁外 `require_authenticated` + `require_project(ai.use)` 短读 → 模型预检 → DNS）与受理锁内 `lock_wizard_project`（按接受绑定的 grant 以 `for_update + visibility_write` 重验并核对作者）一致；viewer、admin+viewer 403，待接受/非成员/已撤权/仅管理员 404，外书与回收站 404，均零任务零调用。续写来源 `load_resume_source` 按 `Task.user_id` + 来源 grant 与本次 grant 逐字一致过滤（作者只接无 grant 的本人任务），发送 lease 内 `verify_resume_source` 对协作者按原 grant 重验 ai.use。请求体由 `WizardRequest.model_dump` 重建，内部保留键（`_collaboration_grant`、`_project_model`、`_wizard_*`、令牌键）一律丢弃。写库只落在本作品（`_require_safe_replacement`/`_assert_replacement_isolation` 及 `get_execution_project` 均限定本书）。**补测** `test_editor_wizard_prompt_respects_owner_ai_visibility[outline|characters]`：作者隐藏的角色 personality、世界观 content，以及 never 角色/世界观的档案都不进入协作者向导提示词；被隐藏字段的条目标题和未隐藏字段作为正对照仍发送。负对照：把大纲向导的 world_text 改为不经 scope 投影后两项失败，还原后 sha256 一致。
- **修正 1（权限，`PATCH /wizard-progress`）**：HEAD 中 `update_project` 已改为 `get_manageable_project`，但 `update_wizard_progress` 保持严格 `get_owned_project`；admin-projects.md 的管理员人工范围没有列出向导进度，而“AI/任务保持原归属”。由此判断“管理员可写向导进度”不是既定规则，是本片顺带扩大的权限。已恢复原语义：`project_service.update_wizard_progress` 改为 `resolve_access(for_update=True)` 后只放行作者和 accepted editor；仅有管理身份 404，管理员兼 viewer 403，管理员兼 editor 按 editor 身份可写。`test_wizard_progress_follows_content_edit` 已据此改为精确断言（含两种兼任身份），outline.md 与 error-handling.md 已同步。前端 `WizardPage.saveProgress` 按 `canEditNow` 门控，管理员管理他人作品时仍会发请求并得到 404 提示，与 HEAD 一致；前端不在本次复核范围内，未修改。
- **修正 2（作者报错顺序，原变化⑤）**：在 `create_wizard_task` 锁外短读里，先对续写来源（含令牌轮次）和覆盖冲突做无锁预检，再做模型预检，恢复原顺序：续写来源 → 覆盖冲突 → 模型。受理锁内仍完整重验，预检结论不用于授权。续写未显式选模型时，改为从已校验的来源 `resume_request` 取原配置，不再单独读 `Task.input`。用例改为 `test_author_resume_source_error_still_reported_before_model`，精确断言「要继续的向导任务不存在」。`test_series_projection_guards.py` 撤回补建作者模型的改动，与 HEAD 逐字一致，9 项通过，证明系列保护 409 恢复为先于模型错误。
- 原变化④（作者重试固定原模型）**保持统一**。理由：中央受理 `consume_task_admission` 对所有 `admission_preflight_aware` 类型的重试只接受与 RetrySource 绑定的原模型预检（`is_original_model_preflight(original) != (retry_of is not None)` 即拒绝），章节、分析、批量、大纲、实体五类早已如此。要为作者恢复“按当时默认配置重解析”，就得给中央协议新开一种非原模型的重试受理，扩大重试伪造面，改动也不小。只有原请求未显式选模型时行为才不同，已在 error-handling.md 写明。
- 收据与输出全文（核对点 3）：收据本身只存 64 位 hex 的 `stream_hash`，不存全文。保存时同一 UPDATE 写入的是 Task 自有的 `output_text` 列，覆盖写入的正是引擎终态刷库本来就要写的同一文本（prefix + 全部已逐帧核验发布的 delta；runner 只有在引擎核验并消费完最后一个 delta 后才会进入保存），因此不增加存储，也不暴露未发布内容。收据另含的 `result` 副本与 `Task.result` 相同，是 E4-g3c 之前就有的设计。可见性方面：撤权（非 accepted）后 `task_visible_to_actor` 不再返回该任务；降为 viewer 后仍能看到已结束任务的 output/result，但其中内容都已写入作品，viewer 本就可以通过 content.read 读到，没有新增泄露。更小的证明方式：已经是哈希；若不写 `output_text`，节流刷库滞后最多 2 秒，受限收尾和删除/裁剪证明就无法用持久事实核对 `stream_hash`。保持现状。
- 未决凭据系统清理（核对点 4）：“只在保存从未尝试提交时调用”由数据库状态结构性保证，不依赖约定。内容替换与 `_wizard_pending → _wizard_completion` 的替换在同一事务提交；`discard_wizard_pending` 在作品 → Task 锁内要求凭据逐字等于本轮 marker，且 `_wizard_completion` 不存在。因此即使误调用发生在提交之后，也只会返回 False，不会撤销凭据。调用方的 `commit_state.attempted` 在 `db.commit()` 之前置位（commit 抛错或取消时不清理）；在 savepoint 释放处被取消时外层事务未提交，此时清理是正确的。两处调用（build 失败、runner finally）都满足该前提。
- recover 三分流（核对点 5）：收据可证 → succeeded；只有可证凭据 → `release_uncommitted_pending` 后按普通中断收尾（`complete_state` 对向导不做 checkpoint CAS，用陈旧内存对象也能落 interrupted）；无凭据或凭据破损 → 待核对。**补测** `test_recover_keeps_damaged_pending_unresolved[input_hash|nonce|with_completion]`（凭据被篡改或与收据并存时 recover 保持 running 并登记清理失败）。负对照：去掉 `release_uncommitted_pending` 的 marker 逐字核对后，input_hash/nonce 两项失败；with_completion 由引擎分支条件独立拦下，属纵深防御。还原后 sha256 一致。
- 作者路径变化（核对点 6）：①②③均为由永久 running 改为可证终态，可接受。**补记一项原清单遗漏的变化**：作者在内容提交之后、done 产出之前取消，由“停在 running、登记清理失败”改为按收据 succeeded（固定说明），不会记成 cancelled 误导用户以为内容未改。新增 `test_author_cancel_after_commit_finalizes_from_receipt`；负对照（关闭收据收尾）失败，还原后 sha256 一致。该说明文案“执行授权或设置已变化”对用户取消不够准确，但与章节/分析/批量收据收尾文案一致，为保持一致暂不细化。④见上；⑤已恢复；⑥已修正为只开放给 editor。
- 跨片一致性：模型本人优先、仅在本人无配置且已授权时回退作者并记来源、任务归属真实 actor、私人风格/模板按 actor、检索只用关键词且作者向量零调用，均与 E4-g3a/b 一致。差异：向导未冻结共享向量选择，所以作者改向量授权不会使协作者向导失败；因为向导本来就不用作者向量，这只是没有大纲/实体那么严，不构成暴露。
- 调整的五个测试文件：closed 文件中“editor 404”是关闭态断言，必须翻转；其中 viewer 精确 403、伪造键不被信任、零调用的断言都保留，待接受/非成员/已撤权/仅管理员由 e4g3c 覆盖。resume_identity 只改了受理入口，断言不变。style_privacy 只换了 monkeypatch 目标；agent_tools 只追加了断言；series_projection_guards 已撤回改动。均未放宽。
- 回归（精简策略）：`uv run ruff check .` 通过。SQLite 逐文件（3 路并行）33 个文件共 883 项全部通过、0 failed：e4g3c 89（含新增 5 项）、wizard 30、wizard_resume 9、resume_identity 15、execution_identity 20、uncertain_cleanup 7、visibility_fencing 8、cross_project 9、collaboration_atomicity 8、collaboration_closed 1、series_projection_guards 9（已撤回改动）、series 6、style_privacy 7、agent_project_style_privacy 35、tasks 32、retry_admission_api 5、receipt_deletion_guard 83、e4f 41、e4g 55、e4g2 59、e4g3 57、e4g3b 64、E4 撤权 22、agent_api 11、agent_runtime 20、agent_tools 54、agent_providers 59、recycle_bin_tasks 12、sqlite_cancel_rollback 1、outline_pro 16、projects_and_story 4、inspiration 27、admin_projects 8；PostgreSQL 逐文件顺序跑 9 个文件共 48 项全部通过：e4g3c 5、wizard_atomicity 2、wizard_dispatch 4、wizard_resume_identity 2、task_execution_identity 7、e4g3b 4、e4g3 4、E4 撤权 2、receipt_deletion_guard 18。

## 进度记录：2026-11-12 E4-e 旧无占用文档任务清理

按 design.md §6.3 裁决实现「证明后系统清理」；不补造身份、不执行旧任务、不开放任何协作入口。

做了什么：
- `kb_index_service`：抽出 `_document_task_conditions`（原 `_claim_task_conditions` 改为复用）；新增 `LegacyDocumentTask`、`capture_legacy_document_task`（只做形状检查，task_key/queued_at 取调用方读取时捕获值）、`cleanup_legacy_unclaimed_document_task(db, legacy, status=failed|cancelled)`（调用方持作品锁并提交；返回 None 须回滚）、`legacy_cleanup_committed`（提交确认丢失时按原 UUID/轮次持久事实确认，不重放 CAS）。
  - 证明（同一事务、作品锁 → Task FOR UPDATE → Document FOR UPDATE）：Task `_legacy_task_conditions`（UUID/queued_at/actor/作品/kind/`resource_key == kbdoc:{id}`/请求 document 模式且 project_id/document_id 为整数并与本作品一致、status queued、`execution_key` 与两项管理员取消意图为空）；文档 `_legacy_document_conditions`（属本作品、pending、三列全空）；无该文档其他未结束任务（OPEN 或执行键/管理员意图未清，resource_key 或请求 document_id 命中）；input 尚无清理标记。
  - 写入：Task CAS → 终态 + 固定文案 + `input._kb_legacy_unclaimed_cleanup=True`；文档 CAS（仍 pending 且三列空）→ failed「旧版建索引任务已清理，请重新索引」；两者任一 rowcount≠1 返回 None 由调用方回滚；随后 `record_state_change`。不写正文/分块/向量，不调用模型/向量，不派生任务。
- 引擎 `_claim_and_run`：`queued_document_claim` 为 None 时，若非 `_stopping` 且未登记取消意图，于领取事务内（gate → User → 作品 → Task 锁已持有）调用 `_fail_legacy_document_queued` 记 failed，关 hub（error 帧）、prune；不可证明时保持排队，`_log_legacy_document_unproven` 同任务只记一次日志。
- `task_service._cancel_legacy_document_task`：用户取消与作品删除排空共用；`lock_record`（用户取消为同事务重入，删除排空在此首次取父行锁，允许删除中作品）后调用同一函数记 cancelled；不可证明仍 409 `task_cleanup_unproven`（删除路径转为 `project_tasks_not_stopped`）；失败不登记项目级 abort 失败（状态已整体回滚，下一轮按数据库事实重试）。
- `KbIndexHandler.validate_retry`：带清理标记的旧任务重试 → 409 `kb_legacy_task_not_retryable`；作者经 `POST /kb/documents/{id}/reindex` 新受理建立带占用的新任务。

作者可见行为变化：
1. 旧无占用文档任务不再永久「排队中」：引擎领取时变为「失败」，文案「旧版知识库索引任务缺少占用记录，未执行；请在知识库中重新索引该文档」，订阅者收到 error 帧。
2. 取消该旧任务由 409 变为 200 cancelled，error「任务已取消；旧版知识库索引任务已清理，请在知识库中重新索引该文档」。
3. 含该旧任务的作品可以删除（之前 409 `project_tasks_not_stopped`）。
4. 对应文档由 pending 变为 failed，error「旧版建索引任务已清理，请重新索引」；正文、分块、向量保留不变。
5. 旧任务（清理后）重试返回 409 `kb_legacy_task_not_retryable`（之前因排队返回 409「只有失败、已取消或已中断的任务可以重试」或令牌缺失 409）。
6. 清理后「重新索引」可受理（之前因旧任务占用资源键 409「该文档正在建索引」）。
7. 不可证明的情形（文档已被占用/非 pending、另有未结束任务、管理员取消意图、执行键非空、跨作品、resource_key 不符、轮次变化、queued_at 为空）行为不变。

测试：
- 新增 `tests/test_kb_legacy_document_cleanup.py`（SQLite 16）：引擎调度自动清理 + 统计 + 不再调度 + 重试拒绝 + 重新索引端到端成功；直接领取清理；用户取消；7 种负例参数化（文档已占用、非 pending、请求指向同文档的其他 paused 任务、执行键未清的终态任务、管理员取消意图、execution_key 非空、resource_key 不符；均同时核对引擎领取、共用函数锁内拒绝与取消 409）；跨作品文档；捕获轮次/UUID 不符及读取后被重新入队；领取中途关停/登记取消意图不动手；清理与重新索引并发；用户取消后引擎领取 no-op；全部核对正文/分块/向量快照不变与零模型/向量调用。
- 新增 `tests/test_kb_legacy_document_cleanup_postgresql.py`（PG 4，双实例 + `pg_blocking_pids` 证明阻塞）：清理先持锁 → 重新索引等待后 202 且绑定新占用；重新索引先持锁 → 清理等待，受理 409 且分块不动、清理随后成功、再次重新索引 202；用户清理先持锁 → 另一实例引擎领取等待后 no-op；引擎清理先持锁 → 用户取消等待后 409，任务 failed。
- 改写：`test_task_execution_fencing.py::test_legacy_unclaimed_document_cancel_fails_closed_without_claim_proof`（可证明 → cancelled/文档 failed/重试 409/重新索引 202）+ 新增 `..._still_fails_closed_when_unproven`；`test_recycle_bin_tasks.py::test_unproven_pending_document_blocks_project_deletion`（可证明 → 204）+ 新增 `test_unprovable_legacy_document_task_still_blocks_project_deletion`；`test_task_receipt_deletion_guard_postgresql.py::test_unproven_pending_document_blocks_cross_connection_project_delete` 按文档状态参数化（pending → 204 / 非 pending → 409）。
- 负对照（备份到 `/tmp/nc_e4e_<ts>_*.bak`，改后恢复并 `shasum -a 256 -c` 全部 OK）：①去掉文档 pending 证明 → 1 失败；②去掉其他未结束任务检查 → 2 失败；③去掉重试标记检查 → 2 失败；④去掉引擎 `_stopping`/取消意图守卫 → 2 失败；⑤引擎不调用清理 → 2 失败；⑥去掉执行键/管理员意图证明 → 2 失败；⑦去掉文档归属作品证明 → 1 失败。（④初版用例因 `_claim_and_run` 入口已提前返回而未捕获，改为在锁内翻转标志后才生效；另发现测试夹具用内存 aware queued_at 导致 SQLite 领取早退，已改为读回数据库值并补正对照。）

验证（精简策略，未跑全量）：
- `cd backend && uv run ruff check .`：All checks passed。
- SQLite 逐文件 3 路并行 26 个文件全部通过（kb_legacy 16、task_execution_fencing 11、recycle_bin_tasks 13、recycle_bin 12、kb_index_claims 7、kb_document_claim_send_e6 8、kb_document_guard_integration_e6 5、kb 33、tasks 32、book_import 27、book_import_actor_identity 16、book_import_skip_identity 5、collaboration_task_revocation_e4 22、collaboration_chapter_entry_e4f 41、agent_api 11、agent_runtime 20、sqlite_cancel_rollback 1、admin_task_cancellation 30、admin_tasks 27、task_abort_identity 22、task_receipt_deletion_guard 83、task_retry_admission_api 5、task_statistics 12、task_execution_identity 21、retrieval 15、kb_index_identity_migration 2）。
- PostgreSQL 16.15（隔离 collab_test，逐文件单进程顺序）9 个文件全部通过：kb_legacy 4、kb_index_identity 1、kb_document_guard_e6 1、task_execution_fencing 1、task_receipt_deletion_guard 19（含改写的 2 项）、collaboration_task_revocation_e4 2、collaboration_shared_index_e6 4、bound_memory_index_e6 2、admin_chapter_claim_cancellation 1。

遗留：
- 不可证明的旧文档任务仍保持排队；引擎每次领取返回后 `_on_done` → `notify` 立即再次调度，形成与「授权有效但 handler 未就绪」同类的调度热循环（每轮若干条 SQL；日志已去重）。可证明的旧任务清理后不再被调度。如需消除应另行裁决（如对无法证明的同一 UUID/轮次退避）。
- `queued_at` 为 NULL 的更早任务（0026 前）无法按轮次 CAS，仍失败关闭。
- 清理提交确认丢失且独立事实复查失败时只记日志/返回失败，下一轮按数据库事实重试；未单独构造该故障注入用例（沿用 `_fail_collaboration_queued` 同一模式）。
- PG 负对照未做（锁序被作品父行锁统一串行，去掉内部行锁不改变可观察结果）；PG 用例以 `pg_blocking_pids` 证明真实阻塞顺序。


### 复核结论（2026-11-12，check agent）

- 证明与原子性：Task（FOR UPDATE + 条件）→ Document（FOR UPDATE + pending/三列空）→ 其他未结束任务扫描 → Task CAS（同一组 `_legacy_task_conditions` 重评）→ Document CAS，全部在调用方持作品父行锁的同一事务内；任一 rowcount≠1 返回 None，三个调用方（引擎 rollback、取消 `except BaseException: rollback`、删除排空经取消路径回滚）都整体回滚，无「任务已改、文档未改」半态。SQLite 下三条入口在读证明前都已经由 gate / `lock_record` 的 no-op UPDATE 取得写锁；新受理（reindex / 上传）同样先取作品锁，并受活动 `resource_key` 部分唯一索引约束，所以「扫描其他任务 → CAS」之间不会插入新任务。确认没有 TOCTOU。
- `_document_task_conditions` 抽取：`_claim_task_conditions` 仍是「公共身份条件 + phase 执行键/状态 + 非 cleanup 时两项管理员意图」，`test_kb_index_claims`（7）与 `test_kb_document_claim_send_e6`（8）通过。仓库中没有抽取前的副本，无法逐字对比，以回归测试为证。
- 不会误清的情形：带占用的新任务走 `queued_document_claim`，不进入该分支；running/刚领取的有 execution_key；管理员意图、跨作品、`resource_key` 不符、轮次变化均由 SQL 条件拒绝。`queued_at IS NULL` 时 `capture_legacy_document_task` 返回 None，本次补测试 `test_null_queued_at_legacy_task_fails_closed`（引擎不动、取消 409）。协作者任务（input 含 GRANT_KEY）：kb.index 未声明 `collaboration_execution_ready`，受理阶段无法产生此类任务。即使存在旧形态的此类任务，清理也只把任务记 failed、把无人占用的 pending 文档记 failed，不执行、不写内容、不发送，结论安全。rebuild 只刷新 ready/ready_keyword_only 文档，不会把被清理的 failed 文档改回 ready。
- 锁序：引擎 gate→User→作品→Task→Document；用户取消 resolve_access（同序）→作品重入→Task→Document；删除排空时准入已冻结、引擎 `lock_active` 拒绝删除中作品，`lock_record` 允许删除中作品只用于本次清理，安全，无新增死锁路径。
- 统计 / SSE / 裁剪：与普通失败、取消一致（`record_state_change` 带同一 `finished_at`、`close_hub("error")`、`prune_finished`）。
- **修复：领取热循环**。不可证明的旧文档任务（以及 handler 未就绪 / 原认证失效 / 旧章节无 claim / 协作失效不可收尾）被领取后，`_on_done → notify()` 会立刻重新调度。每轮都执行 `authz.gate` 与 User/作品的 no-op UPDATE，在 SQLite 下持续抢占写锁（负对照：0.5 秒内领取远多于 1 次）。修法：`engine._stalled[task_id] = (UUID, queued_at)`，在上述锁内「确认无法前进」的返回点登记；`_on_done` 只 `_wake.set()`、不清理登记；`_dispatch` 跳过登记项，但仍把其串行键记入 busy（保持同键先进先出），并剪除已不再排队或已换轮次的登记；外部 `notify()` 与兜底轮询超时会清空登记后重试，因此可清理的情形最多延迟一个 `poll_interval` 被处理，不会被掩盖。额度/并发、settings 未确认、轮次变化等暂时性返回不登记。新增测试 `test_unproven_legacy_task_is_not_hot_looped_and_retries_on_notify`：0.5 秒内仅领取 1 次；释放占用后 notify，任务被清理为 failed。负对照（恢复 `_on_done → notify()`）下该用例失败，engine.py 已恢复并通过 sha256 校验。规范同步：retrieval.md、error-handling.md「调度」。
- 三个改写测试：不可证明的变体（文档非 pending）仍断言 409 失败关闭且状态不变，未放宽；`test_legacy_unclaimed_document_cancel_fails_closed_without_claim_proof` 测的已是可证明清理的情形，函数名沿用旧名，与内容不符（仅命名问题）。
- 回归（精简）：ruff 通过。SQLite 49 个文件、1121 项全部通过：3 路并行时机器 load≈90，有 6 个文件因 5 秒等待超时失败，单独顺序重跑全部通过（agent_api 11、agent_runtime 20、batch_generate 27、book_import 27、collaboration_batch_entry_e4g2 59、task_sse_frame_guard 10）。PG 9 个文件、35 项顺序全部通过。
- 遗留：清理提交确认丢失的故障注入仍未单独覆盖；`queued_at IS NULL` 的任务仍是永久排队（现已按退避节流，不再热循环），需要另行裁决人工处置路径。

## 进度记录：2026-11-12 E5-a 任务读写面与 paused 收尾

范围：按 design.md §6.3「E5 裁决」落地「降级/撤权时待审核协作任务由系统自动收尾」，并补齐任务读写面测试矩阵与 PG 竞争用例。SSE 管线与相关 strict xfail 未动（E5-b 以后）。

做了什么：
- `TaskEngine._fail_collaboration_queued` 增加 paused 分支（复用同一受限清理，不另起一套）：类型白名单为排队清理白名单去掉 `kb.index`；拒绝待核对收据键（与排队同表）、`_chapter_claim`、批量 `checkpoint.current` 非空、任何仍以本任务 UUID 持有的章节占用（`generation_claim_task_key`）；CAS 条件为原 UUID/actor/作品/kind/`status='paused'`/queued_at/无执行键/两项管理员意图为空，置 failed + 固定协作文案（保留暂停时的 result 摘要，与本人取消待审核一致），同事务 `rich_document_review.discard_pending`（提案 discarded、批量断点审核阶段复位）与 `record_state_change`；提交确认丢失按原 UUID/轮次持久事实确认。不调用 `on_abort`（待审核无执行体、章节占用已在暂停前释放）。
- 新增 `scan_collaboration_paused(project_id?, actor_id?)`：候选 SQL 只取 paused、带 `_collaboration_grant`、非作者、无管理员意图、作品活跃的任务；无锁预筛 `task_visible_to_actor(active_grant_required=True, token_version=原令牌)` + 作者模型开关（仍有效者不取任何写锁）；对失效者 `_fail_collaboration_paused`：与领取同锁序 gate → actor/作者 User → 发送写门禁 → 作品 `lock_active` → Task `FOR UPDATE`（原 UUID + queued_at + grant 逐字）→ 锁内 `_collaboration_claim_failure` 复核 → 上述 paused 分支。不可证明时保持原屏障，同一轮次只记一次日志。在 `cleanup_scope` 内执行，作品删除排空会等待。
- 触发点：`notify_collaboration_change` 在调度器运行时立即对该作品+成员发起复查（后台任务，归入 `_collaboration_checks`，stop 时取消并等待）；调度循环按 `collaboration_scan_interval = 15s` 发起全量复查，`start()` 重置节流使启动后第一轮立即执行（覆盖重启与跨实例未收到通知）。数据库为准，通知只是唤醒。

作者可见行为变化（全部）：
1. 编辑者被降为 viewer / 撤权 / 重邀（旧轮次）/ 经 PATCH 关闭作者模型授权（revision 递增）后，其待审核批量、富文档待审核任务不再永久「待审核」：变为「失败」，文案「协作授权已失效，任务已停止」。
2. 作者模型来源的待审核任务在直接关闭模型授权（不递增 revision）后，于下一次全量复查（≤15 秒或重启）变为失败，文案「作者模型授权已关闭，任务已停止」。
3. 编辑者账号停用或原受理令牌失效（改密等）后，其待审核协作任务于下一次全量复查变为失败，文案「账号状态已变化，任务已停止」（与 E4-a 排队任务一致；此前改密后本人仍可继续，但继续后的领取本就会按原令牌失败）。
4. 作品级批量互斥随之释放：作者可立即发起新批量（此前 409「已有进行中或待审核的批量生成」直到编辑者处理，而降级后编辑者已无法处理）。
5. 该任务的待审核富文档提案被丢弃（discarded），章节正文与文档不变；已保存章节、版本、生成记录、断点保留。
6. 降级为 viewer / 重邀后的成员可在任务列表看到这条已结束（失败）记录并读输出、删除；撤权后看不到。作者看不到编辑者的任务（不变）。
7. 作者自己的任务、仍有效的编辑者任务、有管理员取消意图的任务、已继续/再次暂停（换轮次）的任务、仍有占用或待核对收据的任务不受影响。

测试：
- 新增 `tests/test_collaboration_paused_cleanup_e5a.py`（SQLite 24）：批量审核点 ×{撤权、viewer、重邀、PATCH 关模型授权} 经通知收尾（第一章与断点保留、后续章零写、零新调用、统计 failed、viewer/重邀可读已结束记录且 resume/finish 拒绝、撤权 404、作者不可读、作者可发起新批量）；作者模型来源 + 直接关授权仅全量复查发现；无通知 ×{viewer、撤权、停用、改令牌}×{直接扫描、重启}；富文档单章待审核 ×{降级、撤权}：提案 discarded、正文不变、采纳 404；不误伤（作者任务、伪造 grant 的作者任务、有效编辑者 + 无变化通知、本人模型 + 直接关授权、撤权只影响本人）；不可证明保持屏障 ×{管理员意图、管理员请求时间、批量 current、待核对收据、章节仍被本任务占用}；读后换轮次/UUID/grant 不动 + 正对照；继续先于撤权 → 由领取（E4-a）失败关闭；结束先于撤权 → 保持 succeeded。
- 新增 `tests/test_collaboration_task_surface_e5a.py`（SQLite 34）：11 种身份 × {list、detail、output、clear} × {queued、running、paused、finished} 与 × {cancel、resume、finish、delete、retry}，含他人任务 404、跨身份控制 404；管理员运维取消保持现状：`/admin/tasks` 只有元数据、可受理取消、个人任务接口读详情/输出 404，只读管理员 403；批量审核点富文档提案由协作者采纳（作者读/采纳 404，采纳后批量继续并成功，日志归协作者）；协作者 `/finish` 批量审核点 ×{原轮次、viewer、撤权、重邀}；授权变化后采纳 404、随后系统收尾丢弃提案并释放作者批量。
- 新增 `tests/test_collaboration_paused_task_postgresql.py`（PG 9，两实例 + `pg_blocking_pids`）：撤权先持锁 ×{finish、resume} → 本人等待后 404，另一实例复查收尾；本人先持锁 ×{finish、resume} → 撤权等待，finish 保持 succeeded / resume 回到排队后由另一实例领取失败关闭；系统收尾先持锁 ×{finish、resume} → 本人等待后 409，任务 failed；本人先持锁 ×{finish、resume} → 收尾等待后 no-op；预筛后换轮次/UUID 不动 + 正对照。
- 调整 `test_collaboration_batch_entry_e4g2.py::test_editor_resume_paused_batch_and_revoked_resume_rejected`：原断言「降级后仍 paused」（此前只因后台复查未完成而偶然通过），改为等待复查后断言 failed + 固定文案、已写两章保留、第三章未写。安全断言未放宽（降级后 resume 仍 403/404）。
- 负对照（备份为 `/tmp/nc_e5a_<ts>_{engine,task_service}.bak`，每次改后立即还原并 `shasum -a 256 -c` 全 OK）：N1 通知不复查 → 6 失败；N2 调度器不全量复查 → 4 失败（重启类）；N3 不丢弃提案 → 5 失败；N4 锁内不按原 UUID/轮次选取 → 1 失败；N5 待审核不检查批量 current/章节占用 → 2 失败；N6 `finish` 不要求原轮次 → 矩阵 3 失败；PG：收尾不取锁 → 2 失败。

验证（精简策略，未跑全量）：
- `cd backend && uv run ruff check .`：All checks passed。
- SQLite 逐文件 3 路并行 39 个文件 1155 passed、4 xfailed（既有 SSE strict xfail，未转正）、0 failed：新文件 2 个（24 + 34）、tasks 32、collaboration_task_{privacy 15+1x, review 12, revocation_e4 22}、review_{read 16, claim 36}、editor_proposal 30、batch_generate 27、batch_chapter_claim 55、e4g2 59、e4f 41、e4g 55、e4g3 57、e4g3b 64、e4g3c 89、notifications 11+3x、log_private_views 17、user_deletion 16、analysis_execution 128、recycle_bin_tasks 13、agent_{api 11, runtime 20, audit_integration 8}、task_sse_{scope 16, engine_http 1, frame_guard 10, forgery 3}、sqlite_cancel_rollback 1、admin_tasks 27、admin_task_{cancellation 30, adversarial 17, check_regressions 4}、task_receipt_deletion_guard 83、task_execution_fencing 11、kb_legacy_document_cleanup 18、rich_document_proposals 14、scene_review_regressions 28。
- PostgreSQL 16（隔离 collab_test，逐文件单进程顺序）12 个文件：新文件 9、collaboration_task 6+1x、revocation_e4 2、e4g2 6、review_read 2、review_claim 4、editor_proposal 2、notifications 2x、task_sse_scope 7、receipt_deletion_guard 19、admin_chapter_claim_cancellation 1、log_private 9；全部通过，xfail 不变。

补测发现：
- 缺陷（已修）：待审核协作任务降级/撤权后永久 paused、作者批量被卡（即本片主体）。
- 原用例 `test_editor_resume_paused_batch_and_revoked_resume_rejected` 断言了旧的「仍 paused」行为，已按新语义改写（见上）。
- 未发现其他读写面越权。观察（未改）：①本人 finish/resume 已提交、随后撤权在响应投影前提交时，响应为 404，但操作已生效（以数据库为准，属安全优先的既有投影行为）；②viewer/旧轮次对本人已结束任务调用重试时，受控重试路由在锁外先做原模型绑定校验与预检（可能触发 DNS 预检），再由锁内原轮次校验拒绝；不建任务、不调用模型。

遗留：
- SSE（协作者订阅、收据收尾帧、strict xfail）按计划留给 E5-b 以后。
- 全量复查间隔 15 秒内（跨实例/无通知的直接改库）待审核任务仍显示「待审核」；本人此期间的继续/结束按锁内授权被拒（撤权/降级）或在改密场景仍可操作并与收尾按提交顺序串行（PG 已证）。
- Agent `agent.turn` / 拆书的待审核任务不在白名单（协作者无法受理），保持原状。
- 前端未改（E5-g）：协作者任务面板/按钮仍按 `canUseAI` 关闭。

### 复核结论（E5-a，2026-11-12）

核对项与结论：
- 收尾正确性：`_fail_collaboration_paused` 锁序为 读作者 → gate → actor/作者 User（升序）→ 发送写门禁 → 作品 `lock_active` → Task `FOR UPDATE`，与领取路径、`project_access.resolve_access`（本人 resume/finish/采纳所用）同序，不引入新环。CAS 含原 UUID/actor/作品/kind/`status='paused'`/queued_at/无执行键/两项管理员意图为空；`discard_pending` 与 `record_state_change` 在同一事务、同一 commit，任一失败整体回滚。resume 已把任务改为 queued 时 CAS 命中 0 行（status 与 queued_at 双重拦截），随后由领取路径按 E4-a 失败关闭；PG 两种先后已证。作者任务、仍有效编辑者、管理员意图、换轮次、仍有占用/待核对收据均不动，SQLite 与 PG 用例覆盖。
- 15 秒全量复查代价：候选 SQL 走 `ix_tasks_status_id`（SQLite EXPLAIN：`SEARCH tasks USING INDEX ix_tasks_status_id (status=? AND id>?)`），不全表扫描；仍有效者只做无锁读，SQLite 下不取写锁；`collaboration_scan_interval` 为类属性，测试逐例改为 3600，启动时立即复查一次。**已修**：①候选未限定类型——白名单外（agent.turn/book.import/kb.index）的失效 paused 协作任务永远不能收尾，却会在每轮全量复查时取 gate/User/作品写锁再回滚，现于候选 SQL 加 `Task.kind IN 可收尾白名单`；②候选一次性读入全部行（含 input JSON）并在一个长读会话里逐行预筛，现改为按 `id > after_id` 键集分页（`collaboration_scan_batch = 100`），每批一个短只读会话。新增 `test_full_scan_pages_through_candidates_and_skips_non_cleanable_kinds`（分页大小 2、5 个候选全部收尾，白名单外任务零次进入加锁收尾）；负对照：去掉类型过滤 → 失败，去掉分页续取 → 失败，均已还原并通过 sha256 校验。
- e4g2 原用例改写合理：「降级后仍 paused」原本只是后台复查未完成时的偶然结果；「降级后 resume 被拒（403/404）」这条断言保留。另补充：收尾后 resume/finish 仍被拒（403/404/409），状态仍为 failed，且没有新的模型调用。
- 「不算缺陷」之一（已提交的 finish/resume 在撤权后返回 404）：成立。撤权后本人按设计不得再看到该任务，响应投影沿用 `active_grant_required` 是安全优先；改成返回已生效的结果会向已撤权者投影任务快照，扩大泄露面，故不改。降为 viewer 的成员仍可在列表中看到已结束记录。
- 「不算缺陷」之二（viewer 重试会做 DNS 预检）：原记录的描述不准确。受控重试会先在锁外做原模型短读预检 `preflight_original_project_model`（纯本地，不联网），它在 `prepare_task_admission → build_provider`（DNS/provider）之前就已拒绝：非 editor 返回 403，旧轮次返回 400「模型绑定无效」，撤权返回 404。因此 viewer、撤权、旧轮次都到不了 DNS，不构成 SSRF 探测或放大。新增 `test_stale_member_retry_rejected_before_provider_preflight[none|viewer|revoked|reinvite]` 断言 provider 构造次数为 0 且不建任务；none 为正对照，会构造 provider 并受理。负对照：短读预检改成按任务自身轮次放行并去掉角色检查后，viewer、reinvite 两项失败，证明这一短读是唯一屏障；已还原并通过 sha256 校验。
- 矩阵测试：11 种身份 × 操作 × 状态的期望值逐格固定（状态码 + 操作后状态），能区分不同实现；「管理员只能取消、不能读输出」由 `test_admin_operations_can_cancel_but_never_read_output` 断言：`/admin/tasks` 不含 output/input，个人接口详情/输出 404，取消受理后仍 404，只读管理员 403。
- 测试稳定性（已修）：`test_paused_cleanup_keeps_barrier_when_not_provable[admin_intent]` 在 3 路并行时偶发失败。原因是伪造的管理员取消意图被调度器的管理员取消排空合法地收尾为 cancelled，与断言产生竞争。改为该用例关闭管理员排空，只验证协作收尾不越过屏障，安全断言不变；单独连跑 3 次、并行重跑 1 次均通过。

回归（精简策略）：
- SQLite 29 个文件逐文件 3 路并行：首轮 28 个通过、1 个偶发失败（见上）。修复后受影响的 3 个文件（paused_cleanup 25、surface 38、e4g2 59）并行重跑全部通过。合计 687 passed、1 xfailed（既有）、0 failed。
- PostgreSQL 11 个文件逐文件单进程顺序执行：62 passed、1 xfailed（既有）、0 failed。
- `uv run ruff check .`：All checks passed。

遗留：同原记录（SSE 留到 E5-b 以后；前端留到 E5-g；15 秒窗口；Agent、拆书的 paused 任务不在白名单内）。

## 进度记录：2026-11-12 E5-b 已结束回放帧证明

范围：受控任务流（`engine.subscribe(guarded=True)` + `stream_lease.task_sse_response`）对已结束与 paused 任务回放帧的证明。生产路由 `api/v1/tasks.py` 未改（协作者仍 404，E5-f 开放），作者生产订阅仍走非受控管线、行为不变；strict xfail 未转正。

做了什么：
- `stream_evidence.verify_stream_frame`：来源为 `TaskViewEvidence` 且非 queued 时，作品正常（或个人任务）进入新分支 `_verify_persisted_replay`；作品删除中仍只走原「作者固定取消帧」分支。新分支要求：持锁行状态为已结束或 paused；`capture_task_view(持锁行) == 帧来源`；事件名与状态对应（succeeded→done、paused→paused、其余→error，snapshot 键为 task/text），键集合固定；按订阅原绑定重算 `to_out` + `project_task_out(active_grant_required=原绑定, expected_grant=原 grant, expected_queued_at=原 queued_at)`，作品任务的 text/result/detail 再经 `project_payload`（与引擎 `checked_frame` 同一投影），摘要须逐字一致。paused 富文档审核结果在同一事务内联表复核 Task 断点与 pending 提案（条件同 `checked_frame`，含原绑定的 `task_visible_to_actor`），只注入原 task_key。个人任务不经作品投影、帧不得带可见性戳（原有通用检查）。
- `stream_lease`：`_LeaseState` 增加 `binding`（`_send_frame` 登记订阅原绑定）；新增 `task_stream_binding(db, …)`，先 `require_task_stream_lease` 再逐项核对绑定与租约一致，只能在真实单帧发送租约内取得，裸锁 scope 取不到。
- 引擎回放路径未改：`persisted_source(row)` + `checked_frame` 的产出与上面重算一致（正例用例证明：作者、editor、降为 viewer 的本人、个人任务的受控回放均与详情/输出接口逐字相等）。

测试：
- 新增 `tests/test_collaboration_stream_replay_e5b.py`（SQLite 17）：作者 / editor / 降为 viewer 的本人回放已结束（成功、失败）任务 200，snapshot 与终态帧中的 task、text 与详情、输出接口一致，私人引用（log_id）按投影置空；作者与编辑者互相 404。引擎先产出带证明的帧、发送前 {撤权、重邀旧轮次、仅管理员、改令牌} → 只发 `permission_denied`、无 result；重邀后新订阅可读本人已结束记录，其余新订阅 404。伪造 view 指纹、篡改 result、篡改 output、改事件名、改 task.status → `permission_denied`；同一事实重新签发的未篡改帧可通过（正对照）。持久行在产出后被改写 → 拒绝；可见性版本在投影后变化 → 拒绝。paused 富文档帧：回放与详情一致、带原提案凭据；提案在发送前被丢弃 → 终态帧拒绝（快照单独仍可发）；伪造 token → 拒绝。系统收尾（E5-a，降级通知触发）后：待审核时订阅的旧流帧全部拒绝；本人重新订阅回放 failed 终态（固定协作文案、无 result、无 awaiting_review）。个人任务回放、篡改、行改写。
- 新增 `tests/test_collaboration_stream_replay_e5b_postgresql.py`（PG 2，两实例 + TCP + `pg_blocking_pids`，真实引擎产出帧、真实中央校验器）：帧租约先持锁（快照在实际 send 内）→ 撤权等待（证明阻塞）→ 放行后撤权提交 → 终态帧 `permission_denied`、无结果；撤权先持锁（`add_success` 处停住）→ 帧租约等待（证明阻塞）→ 撤权提交后快照也不发出。
- 负对照（备份为 `/tmp/nc_e5b_<ts>_{stream_evidence,stream_lease}.bak`，每次改后立即还原并 `shasum -a 256 -c` 全 OK）：N1 屏蔽摘要比较 → 5 失败（result/output/status 篡改、伪造 token、个人篡改）；N2 屏蔽锁内持久行复查与重算 → 9 失败；N3 屏蔽富文档联表复核 → 1 失败；N4 屏蔽租约内成员/可见性复查 → 重邀旧轮次 1 失败（撤权/仅管理员/改令牌另被 `project_task_out`/`project_payload` 的独立可见性复查与认证发送屏障拦截，属纵深防御）；N5 再屏蔽投影内可见性复查 → 仍只有重邀 1 失败（撤权由 `project_payload._final_visibility` 独立会话复查拦截）。PG：去掉帧租约共享锁 → 2 失败（等不到阻塞）；屏蔽锁内复查 → 2 失败（撤权后的帧被发出）。

验证（精简策略，未跑全量）：
- `uv run ruff check .`：All checks passed。
- SQLite 18 个文件逐文件 3 路并行：446 passed、1 xfailed（既有 strict xfail，未转正）、0 failed：新文件 17、agent_api 11、agent_runtime 20、e4g3 57、e4g3b 64、e4g3c 89、paused_cleanup_e5a 25、task_surface_e5a 38、task_privacy 15+1x、task_review 12、task_revocation_e4 22、recycle_bin_tasks 13、sqlite_cancel_rollback 1、task_sse_{engine_http 1、forgery 3、frame_guard 10、scope 16}、tasks 32。
- PostgreSQL 16（隔离库，逐文件单进程顺序）5 个文件：新文件 2、task_sse_scope 7、collaboration_task 6+1x、paused_task 9、notifications 2x；26 passed、3 xfailed（既有）。

遗留：
- 生产路由仍对协作者 404，`test_finished_stream_is_authorized_in_http_and_denied_after_revocation` 等 strict xfail 待 E5-f 挂载受控管线后转正。
- 回放帧的可见性戳在投影时读取、发送时比较：投影与发送之间若作品 AI 可见性版本变化，本次回放以 `permission_denied` 结束，需重新订阅（安全优先，不放宽）。
- 运行帧（章节/分析/批量/向导）与收据收尾帧仍按 E5-c / E5-d 处理；本片的持久证明可供收据收尾直接复用。
- 规范同步：error-handling.md 新增「受控任务流的持久回放帧证明（E5-b）」条目。

### 复核结论（E5-b，2026-11-12，check agent）

- 投影同源（**已修**）：原 `_verify_persisted_replay` 自写了一套「状态→事件名/键」映射（与引擎 `_finished_events` 平行），task 字段走 `project_task_out(to_out)` 而引擎走整帧 `project_payload(out_json)`，富文档凭据的形状校验、联表条件和最终注入也在两边各写一份。两边当前产出一致，但日后改一处另一处不会跟着变。已抽出 `stream_evidence` 公共函数并让两边都调用：`persisted_replay_events`（引擎 `_finished_events` 直接委托）、`rich_review_of` / `rich_review_shape_ok` / `rich_review_pending_conditions`、`finalize_projected_frame`（注入原 task_key 与已验证的提案凭据）。现在校验器取同一原始帧，作品任务对整帧做 `project_payload`（与 `checked_frame` 相同），再按原绑定用一条 SQL 复核（本人、原 queued_at、原 grant、`task_visible_to_actor(原 active_grant_required, 原令牌)`，富文档时同句联表 pending 提案），最后用共用注入比较整帧摘要。摘要覆盖整帧（task 全字段、text/output、result、detail），且 `capture_task_view` 指纹含 status/result/output/error/stage/progress/input/checkpoint 等全部持久列。事件映射：succeeded→done、paused→paused，其余已结束状态（failed/cancelled/interrupted）→error，由同一函数决定，没有遗漏。引擎行为未变（只做了等价搬移）。
- `task_stream_binding`：只有 `_send_frame` 在真实单帧租约里登记 `binding`，`finally` 中随 `_LeaseState` 一起删除；取用时要求 `require_task_stream_lease`（同会话、同协程 owner、同根事务和物理连接）且绑定身份逐项一致，篡改后租约作废。新增 `test_stream_binding_only_inside_real_frame_send`：裸 `_task_frame_lease` 取不到；在真实发送里可取到同一个绑定实例；换任务 ID 取不到；帧结束后同一会话取不到；登记的绑定被替换后租约失效。
- 作者路径：生产路由未改；`checked_frame` 只做等价重构。`test_tasks`、`test_task_sse_*`、`test_rich_document_proposals`、`test_agent_*` 均通过。
- 个人任务：project 为 None 时帧原样发送、不经作品投影；带任意可见性戳一律拒绝，他人的个人任务在订阅入口就返回 404。新增 `test_personal_replay_rejects_project_stamp_and_other_users`（原个人用例里的注释「不能带可见性戳」实际测的是改行，此处补上真正的戳用例）。
- 可见性版本在投影与发送之间变化则失败关闭：**维持原取舍，不改**。原因：窗口只有引擎投影到取得帧租约这几毫秒（回放只有两帧，产出后立即发送）；帧租约持作品可见性共享锁，正在进行的可见性写入会让租约等待而不是穿插。所以只有某次设定写入恰好在这几毫秒内提交才会撞上，不会「几乎总是失败」，即使在频繁编辑设定（`bump_visibility_revision` 调用点很多）的作品里也一样。另一种可行改法是对持久回放只比对戳的 project_id/last_deleted_at：校验器已在锁内整帧重算，而详情/输出接口本身也不看戳，所以这样改不降低信息安全。但它会翻转「投影后版本变化 → 拒绝」这条既有断言，属于放宽，按要求不做。改由 E5-f/E5-g 前端在收到 `permission_denied` 后先重新订阅一次（新订阅会按当前授权判定），以此消化误报，在 E5-g 落实。
- 测试补充：①单层租约用例 `test_frame_lease_alone_denies_replay_after_change[none|revoke|reinvite|admin_only|token]`：中央帧证明一律放行，只靠租约本身仍全部拒绝，none 为正对照且证明帧确实经过校验器；负对照屏蔽租约内的成员复查与 Task 可见性条件后，revoke/reinvite/admin_only 3 项失败（token 由认证发送屏障拦截）。这弥补了原 N4「只有 1 项失败」时撤权/仅管理员缺少单层证据的问题。②负对照：校验器跳过 `finalize_projected_frame` → 富文档 paused 用例失败；跳过富文档联表 → 同一用例失败；均已还原并通过 `shasum -a 256 -c`。安全断言没有放宽。

回归（精简策略）：
- `uv run ruff check .`：All checks passed。
- SQLite 23 个文件逐文件 3 路并行：e5b 24（17+7 新增）、agent_api 11、agent_runtime 20、e4g3 57、e4g3b 64、e4g3c 89、paused_cleanup_e5a 25、task_surface_e5a 38、task_privacy 15+1x、task_review 12、task_revocation_e4 22、review_read 16、rich_document_proposals 14、recycle_bin_tasks 13、sqlite_cancel_rollback 1、task_sse_{engine_http 1、forgery 3、frame_guard 10、scope 16}、task_stream_{batching 2、evidence 2}、tasks 32；全部通过，1 xfailed（既有）。其中 paused_cleanup_e5a 首轮 3 路并行时在第 9 项（restart 变体）卡住，1500 秒超时。单独跑、与两个重文件并行复跑、3 份并发复跑 restart 用例，均通过且耗时正常（约 41 秒），未能复现。该路径不经过本次改动的回放代码，记为偶发。
- PostgreSQL（隔离库，逐文件单进程顺序）7 个文件：e5b 2、task_sse_scope 7、collaboration_task 6+1x、revocation_e4 2、paused_task 9、notifications 2x、review_read 2；全部通过，xfail 不变。

遗留：同原记录；另加 paused_cleanup_e5a 并行偶发卡住一次（未复现），以及前端收到 permission_denied 后先重订阅一次（E5-g）。

## 进度记录：2026-11-12 E5-c1 章节与分析运行帧证明

范围：受控任务流（`engine.subscribe(guarded=True)` + `stream_lease.task_sse_response`）对单章（`chapter.generate/polish/revise`，含场景目标 revise）与章节分析（`chapter.analyze`）**运行中帧**的发布身份证明，以及单章富文档 `paused` 帧的终态证明。生产路由 `api/v1/tasks.py` 未改（协作者仍 404，E5-f 开放）；作者生产订阅仍走非受控管线；strict xfail 未转正；收据收尾帧（E5-d）、批量（E5-c2）、向导（E5-c3）未做。

做了什么：
- `ChapterHandler.stream_guard` / `AnalysisHandler.stream_guard`：只交出本执行刚通过 `verify_event` 的同一个 `SendGuard`。章节原来每个事件 `for_task` 新建 guard，改为 `handlers.chapter._job_guard` 缓存在 `ChapterJob.publication_guard`（新增字段，按绑定输入对象身份失效）；每帧换 guard 会使 hub 文本来源混合、晚到订阅者快照失败。分析 guard 领取时已构造一次（`job.send_guard`）。
- `collaboration_ai_guard.publication_execution_matches`：分析领取后断点会写入 pending（之后是完成收据），发布身份随之推进，不再与 `guard._task` 逐字相等。只接受：逐字相等；「原断点 + 本 guard 的 pending 文档」；「原断点 + 由同一 pending 文档派生的 `_analysis_completion`」。`issue_publication` 与 `verify_stream_frame` 共用，不放宽为 `same_execution`。
- `SendGuard._stream_readable` 取代原「一刀切拒绝」：大纲/实体、单章（`_chapter.kind == "single"`）、单独章节分析可读；批量 attempt、批量内分析引用、文档占用、向量输入仍拒绝（E5-c2 / E6）。
- `require_task_read`：分析沿用 `_check_task` 的 read_lock 分支（原执行 + pending 共享锁核对）；单章在 `_verify_task` 后调用新增 `chapter_claim.require_streaming_chapter`——只读（PG `FOR SHARE`，SQLite 沿用租约事务已持库锁）、不要求模型发送 lease、失败不回滚（租约关闭会话），核对 Task 指针与 Chapter 占用仍是本执行。保存/发送路径不变（仍为 `require_running_chapter` / `require_sending_chapter`）。
- `require_terminal_read`：单章不再比场景戳（含目标章 content_revision，本执行保存必然改变；引擎 done/paused 发布核验本来也不比），只认逐字一致的持久 Task 视图；`Task.input` 允许且只允许移除本 guard 原 `_chapter_claim` 指针（`_terminal_input_matches`，实测终态释放占用时会删除该指针）。
- `stream_evidence`：E5-b 的按原绑定可见性复核抽成 `_bound_task_visible`，持久回放与终态富文档 `paused` 帧共用；终态 paused 帧提案在产出后被丢弃/采纳即拒绝。

只读核对如何避免自锁：帧租约事务已按 认证发送屏障 → 可见性共享门禁 → 作品 FOR SHARE → 配置共享门禁 → Task FOR SHARE 取锁。若复用写路径（`require_running_chapter` → `require_project(for_update, visibility_write)`），会在同一事务里把可见性门禁从共享升为独占：PG 下两个订阅者的帧租约（或帧租约与运行体的发送 lease，都持共享）会互相等待对方释放共享锁而死锁；SQLite 下 `resolve_access` 的限时写门禁/回滚会结束租约事务。新原语只做 `_authorize(write=False)` + Task/Chapter `FOR SHARE`，与租约已持锁同为共享、顺序不变（Task → Chapter → 配置），写入方（撤权、保存）先取可见性独占门禁，与租约在第一把锁上串行，不形成环。

测试：
- 新增 `tests/test_collaboration_stream_running_e5c1.py`（SQLite 32，真实引擎 + fake_llm + Gate，受控 `/_test` 路由经 uvicorn TCP 增量读取）：章节 ×{editor, owner} 覆盖 snapshot / delta / progress / 自定义 `continue`（两轮自动续写）/ done，拼接文本 = 落库正文；分析 ×{editor, owner} 覆盖 snapshot / delta / progress / done；编辑者失败章节的 error 终态帧；单章富文档 `paused` 帧（与 pending 提案一致）及「产出后只丢弃提案、不改 Task」→ `permission_denied`。撤权矩阵 {章节, 分析} × {撤权, 降级, 关作者模型授权（PATCH）, 改密, 重邀} × {有通知, 无通知} 20 项：引擎已核验并产出帧、发送前改变 → 只发 `permission_denied`、零泄漏、无 result；无通知时断言拒绝时运行体仍阻塞、任务仍 running（拒绝来自发送侧）。正对照：持帧不改变 → 照常发出并 succeeded（2）。通知唤醒空闲流（不等下一帧）。承重：只改章节占用（Task 行与授权不变）→ 拒绝；分析伪造 pending 来源 → `issue_publication` 拒绝，模拟发布层失守后中央只读核对仍拒绝，原来源在原断点下可通过（正对照）。
- 新增 `tests/test_collaboration_stream_running_e5c1_postgresql.py`（PG 3，双实例、只启动订阅实例引擎、TCP、真实中央校验器）：帧租约先持锁（标记帧在实际 send 内）→ 撤权被阻塞（`pg_blocking_pids` 证明）→ 放行后撤权提交 → 后续帧 `permission_denied`、无 done/result、标记帧不撤回；撤权先持锁 → 已产出帧的帧租约被阻塞（`pg_blocking_pids`）→ 撤权提交后该帧不发出、运行体仍 running；另一实例撤权、本实例无通知（记录确认零通知、任务仍 running）→ 已产出帧被拒。
- 负对照（备份 `/tmp/nc_e5c1_<ts>_*.bak`，每项改后立即还原，`shasum -a 256 -c` 全 OK）：N1 章节 `stream_guard` 每次返回新建 guard → 章节正例 2 失败（快照来源混合）；N2 跳过 `require_streaming_chapter` → 占用变化用例失败；N3 `_check_task` 读路径跳过 pending 核对 → 伪造 pending 用例失败（断言点为帧被发出）；N4 终态跳过富文档联表 → 丢弃提案用例失败；N5 `publication_execution_matches` 放宽为 `same_execution` → 伪造 pending 用例失败。PG：去掉帧租约成员/可见性复查且 `require_task_read` 直接返回 → 撤权先持锁、跨实例 2 项失败（帧租约先持锁一项仍通过：它验证的是锁串行化，撤权后的终态帧由终态路径另行拒绝）。

验证（精简策略，未跑全量）：
- `uv run ruff check .`：All checks passed。
- SQLite 43 个文件逐文件 3 路并行（gtimeout 1500 + faulthandler_timeout=1200）：1128 passed、4 xfailed（既有 strict xfail，未转正）、0 failed。含新文件 32、paused_cleanup_e5a 25、task_surface_e5a 38、e5b 24、e4f 41、e4g 55、e4g2 59、e4g3 57、e4g3b 64、analysis_execution 128、analysis_task_visibility 19、chapter_execution 35、task_privacy 15+1x、task_review 12、revocation_e4 22、review_read 16、notifications 11+3x、ai_guard 85、execution_send_guard 23、task_sse_{engine_http 1、forgery 3、frame_guard 10、scope 16}、task_stream_{batching 2、evidence 2}、tasks 32、agent_api 11、agent_runtime 20、recycle_bin_tasks 13、sqlite_cancel_rollback 1、admin_chapter_claim_cancellation 12、batch_chapter_claim 55、chapter_claim_{author_wiring 12、core 24、wiring 20}、analysis 14、analysis_auto_actor_origin 4、analysis_memory_enqueue_fencing 51、foreshadow_analysis 8、rich_document_analysis 10、rich_document_proposals 14、generation_log_identity 5、batch_generate 27。
- e5a 卡住未复现：`test_collaboration_paused_cleanup_e5a.py` 本轮在 3 路并行中 45 秒通过，faulthandler 无转储。
- PostgreSQL 16（隔离库，逐文件单进程顺序）17 个文件：新文件 3、e5b 2、task_sse_scope 7、collaboration_task 6+1x、notifications 2x、paused_task 9、review_read 2、chapter_execution 11、admin_chapter_claim_cancellation 1、analysis_index_receipt 5、analysis_memory_enqueue 2、batch_chapter_claim 7、chapter_claim_author 2、chapter_claim 3、analysis_entry_e4g 5、analysis_execution 21、analysis_task_visibility 17；全部通过，xfail 不变。

遗留：
- 领取瞬间的 `reset` / `progress(status=running)` 帧来源仍是原 `TaskExecutionIdentity`（不是 `PublicationSource`），按代码推断：排队中开始的受控订阅会在领取那一刻收到 `permission_denied`（未写用例验证）；留 E5-e/f，前端重订阅一次可消化（E5-g）。
- 分析的 pending 共享锁核对与中央 `matches_running(发布身份, 持锁行)` 在正常路径上互为冗余（纵深防御）；单独去掉它只有在发布层也失守时才会漏帧（伪造 pending 用例以测试内模拟证明）。
- 批量（E5-c2）、向导（E5-c3）、收据收尾帧（E5-d）、生产路由开放与 xfail 转正（E5-f）、前端（E5-g）未做。
- 规范同步：error-handling.md 新增「受控任务流的章节 / 分析运行中帧证明（E5-c1）」条目。

### 复核结论（E5-c1，2026-11-13，check agent）

- `publication_execution_matches`（**已修**）：三种接受情况均以 guard 自己的原断点与 pending 文档为基准，伪造 pending（换 nonce）、断点夹带 pending/收据的原执行、其他执行（执行键不同）都拒绝；中央读路径还会按 guard 自己的 pending 以共享锁核对持久断点。缺口：完成收据的判断用 `receipt.get(key) != value`，缺键会被读成 None——作者 pending 的 `grant` 正是 None，缺 `grant` 的收据会被当作同源。已改为收据必须逐键包含 pending 文档后再比值（断点仍不得夹带其他键）。新增 `test_publication_receipt_must_contain_every_pending_key[editor|owner]`：缺 grant/nonce/task_key/execution_key、改 nonce、夹带其他键都拒绝，正对照通过；负对照（还原为 `.get`）→ owner 用例失败。
- `ChapterJob.publication_guard` 缓存（**已修**）：`_job_guard` 按全部 8 项绑定输入的对象身份失效，没有问题；但 `stream_guard` 只比较执行身份（`cached[0][0] is execution`），如果某个输入在核验后被替换，会交出按旧绑定构造的 guard。已抽出 `_guard_inputs` / `_cached_guard`，两边共用，`stream_guard` 要求当前全部输入与缓存一致。guard 本身是不可变的（`_after_verify=None`、没有一次性状态），缓存只省掉构造开销：`verify_event` 每次仍执行 `require_task_write` 或 done/paused 的收据 + 可见性 + 模型复核，发送路径仍由 generation_service 自己的 `for_task` → `require_sending_chapter`，保存路径仍为 `require_running_chapter`，均未因缓存减少校验。中央 `require_task_read` 每帧都会在锁内复查授权，缓存的 guard 只是「绑定描述」，不是有效证明。新增 `test_chapter_stream_guard_cache_tracks_every_binding_input`（逐项替换为等值的新对象 → `stream_guard` 拒绝、`_job_guard` 重建；执行身份换成等值的新对象也拒绝）；负对照（还原为只比执行身份）→ 失败。
- `require_streaming_chapter`：确认只读。`_authorize(write=False)` 走 `resolve_access` 的非写分支（无 User 屏障、无可见性独占门禁、无限时 PRAGMA），Task/Chapter 在 PG 下 `FOR SHARE`，SQLite 下不加锁。失败不回滚是安全的：异常由 `require_task_read` 统一转为 `CollaborationSendBlocked`，租约会话由调用方关闭；SQLite 读到 busy 时 `resolve_access` 会自行回滚，此后的 `require_task_stream_lease` 同根事务复查会失败关闭。PG 首个用例已用 `_assert_no_idle_output_transaction` 证明没有残留空闲事务。锁序：帧租约全程只取共享锁；撤权和保存先取可见性独占门禁，与帧租约在第一把锁上就串行；`release_chapter_claim` 先取作品 `lock_record`，同样不成环。**补充 PG 用例** `test_postgres_two_subscribers_and_runner_send_lease_do_not_block_each_other`：运行体在第二轮真实发送 lease 内持共享锁时停住；此时订阅者 A 的帧租约取锁，并在实际 send 内停住；订阅者 B 的快照帧租约同时取锁发出；放行后运行体的发送 lease 在 A 仍持锁时完成（模型流开始），三方最终都收到 done、没有 permission_denied、没有空闲事务。负对照（只读核对改为写锁）→ 超时失败。
- 终态不比场景戳：不构成缺口。终态帧内容全部来自本 Task 的持久行（result/output/error 已冻结），`capture_task_view` 指纹覆盖全部持久列，必须与引擎登记的 `TerminalEvidence` 逐字一致，并且 `same_execution` 绑定原执行；别的执行之后改写正文不会改变本 Task 行，也不会进入帧。场景戳防的是「上下文变化后仍发出由旧上下文派生的运行中输出」，这一保护在运行帧（`require_task_read` 仍比场景戳）和模型发送时都在。终态处的替代证明是持久视图逐字一致 + 可见性戳 + 认证/成员租约 + 模型绑定复核，与 E5-b 持久回放（同样不比场景戳）口径一致，不多给信息。`_terminal_input_matches` 只允许移除与本 guard 完全相同的 `_chapter_claim` 指针，其他输入变化都拒绝。
- `_stream_readable`（**已加固**）：原实现靠 `_batch_attempt`、`_analysis_reference` 和 `_chapter.kind` 间接排除批量，`chapter.batch` 类型且无 attempt、无章节的 guard 会被判为可读。已按类型显式排除 `chapter.batch`。新增 `test_stream_readable_whitelist_rejects_batch_document_and_embedding`（批量类型、批量 attempt、批量章节占用、批量分析引用、文档占用、向量输入、单章 guard 夹带分析执行，全部不可读）；负对照 → 失败。
- 作者路径：`_job_guard` 只是复用不可变 guard，`verify_event` 的作者分支（`require_task_write` 或 done/paused 复核）不变；作者生产订阅仍走非受控管线。e4f 41、chapter_claim_{core,wiring,author_wiring}、test_tasks 32 均通过。
- 领取瞬间的帧（**确认现象，未修，留 E5-e/f**）：`test_known_gap_queued_subscription_denied_at_claim_progress_frame`（`task_max_per_user=1` 让第二个任务排队）确认排队中开始的协作者订阅会在领取时只收到 `[snapshot, permission_denied]`，没有泄露正文。另外发现同类、而且更常见的窗口：领取后、首个事件核验前（等待模型首个 token 的整个期间）开始的订阅，hub 来源仍是裸 `TaskExecutionIdentity`，快照无法证明，直接 `permission_denied`（`test_known_gap_subscription_before_first_verified_event_is_denied[chapter|analysis]`）。两者都是安全的失败关闭。修复需要在引擎领取 / build_job 之后为「无内容」的 running progress 和空快照建立可证明的来源（例如在 build 后由 handler 签发 `PublicationSource`），要改 engine 领取路径并影响作者管线，不属于「简单且安全」，留给 E5-e/f。这几个用例断言的是现状，修复时需要翻转。
- 规范同步：error-handling.md 的 E5-c1 条目补充了缓存按全部输入失效、收据逐键包含、按类型排除批量，以及两个已知窗口。

回归（精简策略）：
- `uv run ruff check .`：All checks passed。
- SQLite 35 个文件逐文件 3 路并行：1034 passed、1 xfailed（既有）、0 failed（e5c1 39 = 原 32 + 新增 7）。
- PostgreSQL 12 个文件逐文件单进程顺序：80 passed、1 xfailed（既有）、0 failed（e5c1_postgresql 4 = 原 3 + 新增 1）。
- 负对照 4 项（收据 `.get`、去掉批量类型排除、`stream_guard` 只比执行身份、只读核对改为写锁）均按预期失败；备份在 `/tmp/nc_e5c1chk_*`，已还原并通过 `shasum -a 256 -c`。

遗留：领取瞬间的帧和首个事件前订阅（E5-e/f，前端重订阅一次可以缓解，属 E5-g）；其余同原记录。

## 进度记录：2026-11-13 E5-c2 批量运行帧证明

范围：受控任务流（`engine.subscribe(guarded=True)` + `stream_lease.task_sse_response`）对批量生成（`chapter.batch`）运行中每一帧的发布身份证明，含批量内逐章分析帧与审核点 `paused` 帧。生产路由 `api/v1/tasks.py` 未改（协作者仍 404，E5-f 开放）；作者生产订阅管线未动；strict xfail 未转正；收据收尾帧（E5-d）、向导（E5-c3）未做。

做了什么：
- `BatchHandler.stream_guard` 按当前步骤交出三种发布 guard（`SendGuard._batch_stream_shape` 判定形状）：
  - attempt：有当前章时，与 `verify_event` 同一个 guard（`handlers/batch._attempt_guard`，缓存在 `BatchJob.publication_guards`，按原执行、模型/向量绑定、可见性戳、场景戳、cipher、私网开关、章节占用、attempt、本章 ChapterJob 的对象身份失效），绑定 `_batch_attempt` 与同一批量章节占用。章切换即换 guard；构建本章 ChapterJob 之前（无场景戳）与之后（带场景戳）是两组输入。`verify_event` 原来每个事件新建 guard，现改用同一缓存 guard，避免 hub 文本来源混合。
  - analysis：逐章分析中（`BatchJob.active_analysis`，在 `_reserve_review_analysis` 登记收据后、「分析中」阶段前设置，分析结束在 finally 清除），绑定分析原执行与收据引用。只读核对走 `_check_task` 的分析分支，`analysis_service._require_batch_analysis(streaming=True)` 接受本执行的 running 或已完成收据，全程共享锁（ChapterAnalysis 也改为 `FOR SHARE`）。发送/写路径的调用与原来逐字相同，只接受 running。
  - between：无当前章（章与章之间、审核点、完成）时，用 `for_task(..., publication_only=True)` 构造的仅发布 guard，其原执行即本执行最近一次确认提交的断点，逐字比对持锁行。`SendGuard._publication_only` 新字段默认 False；为 True 时 `verify`、`prepare_provider`、`require_task_write` 一律 `CollaborationSendBlocked`。`_make` 只允许批量、无 attempt/分析/占用/场景戳时声明。
- `publication_execution_matches` 对批量的规则：between 逐字相等；attempt 要求 `same_execution`，且断点/游标恰为本 attempt 写入的状态（与持锁行共用新抽出的 `_batch_attempt_state`）；analysis 要求阶段为 analyze、无 current、游标与 `rich_analysis` 即本 guard 的引用。交出旧章 guard 会被拒绝，不放宽为 `same_execution`。
- `_stream_readable` 对 `chapter.batch` 只接受上述三种形状；非批量规则不变。`chapter_claim.require_streaming_chapter` 接受 `kind == "batch"`（核对 `checkpoint.current` 指针，仍只读）。
- 自定义事件 `chapter_start`/`chapter_end`/`reset`、阶段进度、审核点 `paused`、`done` 都经 `checked` → `hub.publish`，携带当前 `hub.source`（PublicationSource），引擎未改。
- 后继来源（新增，批量专用）：批量断点逐章推进，帧发出时原来源往往已被同一执行后续的步骤取代（例如某章最后一段 delta 发出前该章已保存、`chapter_end` 发出前下一章已抢占）。按 E5-c1 的严格规则，这类帧只能等终态，而帧租约 5 秒超时后连接会被静默断开。现在 `verify_stream_frame` 仅对批量 guard、在 hub 未关闭且无终态时，接受满足以下全部条件的**最新**来源，并用它的 guard 在锁内复核当前授权与当前章：同一 hub 已登记（`_publication_hub(latest) is hub`）、`collaboration_ai_guard.publication_successor` 成立（同 actor、绑定、原执行，游标不回退，两端各自满足发布规则）、与持锁行逐字一致。不以持锁行补签，也不跨执行。行已推进而新来源尚未发布时抛 `PublicationSourcePending`（`TerminalEvidencePending` 的子类），退出租约后等 `hub.source_changed` 或终态 latch 再重验。`TaskHub.source` 改为 property，setter 每次换值都会唤醒等待者。`stream_lease._send_frame` 改为在 `frame_timeout` 内循环重验；终态 latch 只会触发一次，单章原语义不变。
- 阶段刷新：批量在本章占用与 attempt 提交后立即重发当前阶段（`batch_service._refresh_stage`，文案与 hub 当前阶段相同，不广播）。引擎随之核验并换上本 attempt 的来源，在途帧不必等到模型首个 token。`verify_event` 在构建前只允许这个阶段事件，不允许任何正文。

作者路径影响：
- 作者订阅管线、SSE 帧序列与内容不变：刷新阶段文案相同，`set_progress` 不广播、不置 dirty。
- 作者批量每个章节 attempt 多一次 `verify_event`（含写屏障 `require_task_write` 的短事务）。每个事件都会调用 `stream_guard`：章与章之间构造仅发布 guard，分析中构造分析 guard，都只做纯构造校验，不访问数据库。若这些构造对作者任务失败，批量会失败。test_batch_generate、batch_chapter_claim、e4g2 以及新文件中的 owner 正例都覆盖了作者多章批量（含分析、审核点），均通过。
- 若可见性在章与章之间变化，失败点可能提前到刷新事件，但失败码与文案相同（`ai_visibility_changed`）；原来在 `chapter_end` 的核验处也会失败。

测试：
- 新增 `tests/test_collaboration_stream_batch_e5c2.py`（SQLite，55 项，真实引擎 + fake_llm + Gate/Event，受控 `/_test` 路由经 uvicorn TCP 增量读取）：
  - 正例：{editor, owner} 两章批量含逐章分析，帧覆盖 snapshot、delta、progress（两次「分析中」）、reset、chapter_start、chapter_end、done；按 reset 切分的实时文本与落库正文逐章相等。{editor, owner} 审核点 `paused` 帧。
  - 撤权矩阵 40 项：{章内, 章与章之间, 审核点 paused, 逐章分析中} × {撤权, 降级, 关作者模型授权, 改密, 重邀} × {有通知, 无通知}，全量参数化，未抽样。引擎已核验并产出帧后、发送前改变授权，结果只发 `permission_denied`：被持住的帧与之后的内容零泄漏，无 done/paused。无通知时断言运行体仍在运行，即拒绝来自发送侧。
  - 正对照 4 项：同样持住各时点的帧但不改变授权，照常发出并结束。
  - 通知唤醒章与章之间的空闲流。
  - 承重用例：只改本章章节占用 → 拒绝；断点换成另一 attempt 并模拟发布层失守 → 中央 attempt 核对仍拒绝，原来源在原断点下可通过；只改本章分析收据 → 分析 guard 拒绝；分析中才订阅时，快照正文来源已被推进，用最新来源复核后发出；行已推进而新来源未发布时，帧租约等待下一来源后在任务仍运行时发出。
  - 形状用例：旧章 guard 不能签发下一章身份；仅发布 guard 不能发送、预检或写入；非批量不能声明仅发布；不同执行不是后继。
- 新增 `tests/test_collaboration_stream_batch_e5c2_postgresql.py`（PG 4 项，双实例，只启动订阅实例的引擎，TCP，真实中央校验器）：
  - 帧租约先持锁（第二章开始帧在实际 send 内）→ 撤权被阻塞（`pg_blocking_pids` 证明）→ 放行后撤权提交 → 后续帧 `permission_denied`，且无空闲事务残留；
  - 撤权先持锁 → 「分析中」帧的帧租约被阻塞 → 撤权提交后该帧不发出，运行体仍在运行；
  - 另一实例撤权，本实例零通知 → 章内已产出帧被拒；
  - 运行体在第二章真实发送 lease 内持共享锁时，订阅者 A（其帧来源已被取代，改用最新来源复核）在 send 内持锁，订阅者 B 的快照照常发出，运行体发送完成，三方都收到 done。
- 负对照：备份为 `/tmp/nc_e5c2_<ts>_*.bak`，每项改后立即还原，`shasum -a 256 -c` 全部 OK。
  - N1 `stream_guard` 交出首个（旧章）attempt guard → 3 失败；
  - N2 `_check_task` 跳过 attempt 核对 → 伪造 attempt 用例失败（帧被发出）；
  - N3 跳过 `require_streaming_chapter` → 占用变化用例失败；
  - N4 流路径跳过分析收据核对 → 收据变化用例失败（修复调用签名后又以新锚点重做一次，仍失败）；
  - N5 去掉后继来源分支 → 分析中订阅、等待下一来源两项失败；
  - N6 attempt 发布规则放宽为恒真 → 伪造 attempt、旧章 guard 两项失败。
  - PG：P1 只读核对改为写锁 → 两订阅者用例超时；P2 去掉租约成员复查、租约可见性条件并让批量 `require_task_read` 直接返回 → 3 项串行化/跨实例用例全部失败（只去掉成员复查与 `require_task_read` 时仍全过，说明租约的可见性条件是独立的一层）。
- 既有测试无需调整。

验证（精简策略，未跑全量）：
- `uv run ruff check .`：All checks passed。
- SQLite 27 个文件逐文件 3 路并行（gtimeout 1500）：739 passed，0 failed。含 e5c2 55、analysis_execution 128、e4g2 59、batch_chapter_claim 55、e5c1 39、paused_cleanup_e5a 25、task_surface_e5a 38、e5b 24、batch_generate 27、ai_guard 85、tasks 32、task_sse_{scope 16、engine_http 1、frame_guard 10、forgery 3}、task_stream_{batching 2、evidence 2}、chapter_claim_{author_wiring 12、core 24、wiring 20、migration 2}、admin_chapter_claim_cancellation 12、task_execution_send_guard 23、agent_api 11、agent_runtime 20、recycle_bin_tasks 13、sqlite_cancel_rollback 1。修正分析收据调用签名后，复跑其中 6 个受影响文件全过。
- PostgreSQL 16（隔离库，逐文件单进程顺序）13 个文件：e5c2 4、e5c1 4、e5b 2、task_sse_scope 7、collaboration_task 6+1x（既有）、paused_task 9、e4g2 6、batch_chapter_claim 7、chapter_claim 3、chapter_claim_author 2、analysis_execution 21、ai_guard 4、admin_chapter_claim_cancellation 1。首轮 `analysis_execution` 有 1 项失败：`test_postgres_batch_analysis_save_wins_after_attempt_lock` 用旧签名替换 `_require_batch_analysis`，而我在写路径也传了新增的 `streaming=` 参数，导致 TypeError。已改为只在流读取路径传该参数，写/发送路径的调用与原来逐字一致；复跑 21 passed，e5c2/ai_guard PG 复跑通过。

遗留：
- 批量内富文档审核点的 `paused` 终态帧走 E5-c1 的终态 + `_bound_task_visible` 联表路径（代码路径与单章一致），本片没有专门的批量富文档受控流用例。
- 领取瞬间的帧与首个事件前订阅的已知窗口同 E5-c1（E5-e/f）；对批量来说，首个事件是 `reset`，紧随领取之后，窗口很短。
- 后继来源只用于批量；单章、分析、大纲、实体仍只认原来源或终态证明。帧在 `frame_timeout`（5 秒）内等不到下一来源时，连接静默断开（abort），前端重订阅可消化（E5-g）。
- 规范同步：error-handling.md 新增「受控任务流的批量运行中帧证明（E5-c2）」条目，并修正 E5-c1 条目中「批量失败关闭」的旧描述；batch-generate.md 顶部补充受控流说明。

### 复核结论（2026-11-13，trellis-check）

- 后继来源：只在 `_is_batch_guard` 且 hub 未关闭、无终态时进入；`publication_successor` 要求两端都是批量 guard、同 actor/绑定/extra 绑定、`same_execution`、游标不回退且两端各自满足发布规则；调用方另要求 `_publication_hub(latest) is hub` 与 `matches_running(latest.execution, 持锁行)`，再以 `latest.guard.require_task_read` 锁内复核。跨执行（执行键不同）、跨 hub、旧授权（actor 令牌轮次/grant 轮次在 binding 内）均不能通过；内容上只是把「已经核验发布过」的旧帧交给当前授权复核，不以持锁行补签。单章/分析/大纲/实体走原分支，逐字未变。
- 等待：`PublicationSourcePending` 在退出整个租约后等待，`changed` 在锁内取得，setter 先换值再 set 旧 Event，不丢唤醒；每次重验都需要一次真实换来源，且整体受 `frame_timeout` 约束，无死锁/无界等待；终态 latch 后 hub 已关闭，走终态分支不再循环。引擎仅在 guard 或执行变化时换来源，不会每个 delta 唤醒。
- `publication_only`：`_verify`（verify / verify_without_one_shot）、`prepare_provider`、`require_task_write` 均拒绝；`_make` 只许批量且无 after_verify；原「批量必须恰有 attempt 或分析」的约束只对仅发布 guard 放开。
- `analysis_service._require_batch_analysis`：与 E5-c2 前备份比对，非流路径逐字一致（`with_for_update()` 等价 `read=False`）；`streaming=True` 只在 `_check_task` 的只读分支传入，且自身强制 `read_lock`。流读取路径（Project/Task/章节/收据/祖先/ChapterAnalysis/配置）均为共享锁。
- 作者路径：阶段刷新文案与 hub 当前阶段相同，不广播；每 attempt 多一次 `verify_event` 的写屏障短事务，与每个 delta 已有的屏障同类，量级可忽略；章间可见性变化在 E4-g2 的 `verify_event` 中本来就会失败，失败点前移但码与文案不变。仅发布/分析 guard 的构造条件是 attempt guard 与分析自身发送 guard 已满足的子集，不引入新的作者失败条件。
- 发现的缺口（已补）：撤权矩阵中被持住的帧都与持锁行一致（走直接分支），没有任何用例证明「后继分支本身」在授权变化时拒绝——N5 只证明后继分支对正例必要。新增 `test_superseded_batch_frame_rechecks_current_grant_with_latest_source`：协作者无本人模型（绑定作者模型），分析中以上一章 attempt 来源构造帧，改变前发出（正对照），直接关闭 `allow_owner_ai`（不递增成员轮次、无通知、运行体仍阻塞）后被拒。负对照：去掉后继分支的 `latest.guard.require_task_read` → 该用例失败、其余两个后继用例仍过（证实此前无覆盖）；备份 `/tmp/nc_e5c2chk_<ts>_succ_noread.bak`，还原后 `shasum -a 256 -c` OK。（初版用例误用协作者本人模型，`allow_owner_ai` 不承重而通过发送，已改为 `editor_model=False`。）
- 测试审阅：无 sleep 定时（均为 Gate/Event + `wait_until` 上限）、撤权矩阵 4×5×2 全量参数化、无 xfail/skip、无放宽断言。
- 回归：`ruff check .` 通过；SQLite 3 路并行：e5c2 56、e5c1 39、tasks 32、agent_runtime 20、recycle_bin_tasks 13、batch_generate 27 全过；PG：e5c2 4 passed。
- 结论：建议接受后继来源机制。

## 进度记录：2026-11-13 E5-c3 向导运行帧证明

范围：受控任务流（`engine.subscribe(guarded=True)` + `stream_lease.task_sse_response`）对开书向导（`wizard.outline/characters/world/careers`）运行中帧（续写 stage 进度、delta、progress、error）与保存后 done 帧的发布身份证明。向导没有 `paused`、没有自定义事件。生产路由 `api/v1/tasks.py` 未改（协作者仍 404，E5-f 开放）；作者生产订阅管线未动；strict xfail 未转正；收据收尾帧（E5-d）未做。**未使用、未扩展 E5-c2 的批量后继来源机制。**

做了什么：
- `WizardHandler.stream_guard` 只交出两种 guard：
  - 未决阶段（stage/delta/error）：构建时的发送 guard `WizardJob.guard`，原执行即带本轮 `_wizard_pending` 的断点（`job.pending.active`）。要求 `guard._task is pending.active`，绑定、续写来源、可见性戳与 job 当前值为同一对象，否则拒绝。
  - 保存后的 done：内容替换与 `_wizard_completion` 收据同事务提交，断点和可见性戳都已推进。先以 `verify_wizard_completion` 按原未决凭据逐键核对收据与 done 载荷，再用 `for_task(完成断点, 原模型绑定, 保存事务内的新戳, resume=原续写来源, publication_only=True)` 构造仅发布 guard。该 guard 缓存在新增字段 `WizardJob.publication_guard`，按六项绑定输入的对象身份失效。
- `SendGuard._wizard_stream_shape`：`pending` 或 `completed`。凭据或收据必须与原执行身份逐项一致：版本 1；nonce、task_key、execution_key 为 UUID；queued_at、actor、project、kind、input_hash 一致；收据另含 result（kind 一致）、result_hash、stream_hash。guard 不得带章节、批量、分析、文档、向量、实体来源、场景戳、额外绑定或一次性回调。`_stream_readable` 对 `wizard.*` 只接受这两种形状。`_make` 允许向导声明 `publication_only`，但只能绑定「带完成收据、不带未决凭据」的断点。
- `publication_execution_matches` 增加向导分支：形状合法且与 guard 原执行**逐字相等**。未决到完成的推进不在这里放宽。
- 运行帧只读核对沿用 `require_task_read`：`_check_task` 逐字比对持锁行，续写来源走 `verify_resume_source(for_update=True)`（PG 下 FOR SHARE，协作者走 `require_project` 非写分支）。终态核对沿用 `require_terminal_read`，向导 Task.input 不变。全程只加共享锁，不升级、不回滚，中央代码（stream_evidence、stream_lease、engine）未改。

评估「同一执行后续步骤取代帧来源」：向导只有一次推进（保存）。done 帧可以用精确匹配解决：另构造带新戳的 completed guard，不需要后继来源。但未决阶段的帧如果在保存提交后才实际发送，只能走终态证明。保存收紧可见性时（角色或世界观整体替换已有条目，必然递增 revision/epoch），两处同时失效：subscriber 投影时记录的帧戳不再等于当前作品戳（中央通用检查，E5-b 维持的取舍），构建 guard 的戳也不再成立。结果是 `permission_denied`。精确匹配无法消除这个窗口，需要类似「同一执行戳后继」的机制或放宽 E5-b 取舍。**按要求未引入**，留待作者确认。

作者路径影响：
- 作者订阅管线、SSE 帧序列与内容不变；作者任务同样经 `stream_guard`（引擎对所有事件调用）。
- 新增开销：每个事件一次纯对象检查；未决阶段首帧和 done 各一次 `issue_publication`；done 时一次 `verify_wizard_completion`（纯比较）和一次 guard 构造（纯构造，不访问数据库）。
- 新增失败面：如果作者任务的 `stream_guard` 或 `issue_publication` 失败，delta 会使任务失败；done 会走既有的收据受限收尾（succeeded，结果未发布）。构造条件都是 verify_event 已核验状态的子集。四类向导作者正例、续写作者正例、test_wizard 等既有作者用例均通过。

测试：
- 新增 `tests/test_collaboration_stream_wizard_e5c3.py`（SQLite 54 项）：
  - 正例：4 类向导 ×{editor, owner}，覆盖 snapshot/delta/progress/done，拼接文本 = output_text，内容已替换；characters 断言 done guard 的戳 ≠ 构建戳（保存确实收紧）。续写（大纲）×{editor, owner}：快照含「从中断处继续」阶段与前缀，续写正文逐帧通过。编辑者失败 error 终态帧。
  - 撤权矩阵 30 项，全量参数化：{未决 delta, 续写 delta, done} × {撤权, 降级, 关作者模型授权, 改密, 重邀} × {有通知, 无通知}。运行中两点在无通知时断言运行体仍阻塞、任务 running，且内容未提交；done 点断言 done/result 不发出、已提交内容保留。
  - 正对照 3 项、通知唤醒空闲流 1 项。
  - 承重 5 项：只改续写来源任务 → 拒绝；伪造未决凭据 → guard 不能签发，模拟发布层失守后中央只读核对仍拒绝，原来源可过；done 持住时作者改可见性 → 拒绝；形状/stream_guard 单元（editor/owner 各 1）。另有 1 项校验测试输入是合法 JSON。
  - 已知窗口 3 项：characters 在途帧跨收紧保存 → 拒绝（outline 同场景照常 done）；首个事件前订阅 → 拒绝（同 E5-c1）。
- 正例与正对照在保存前加末尾 Gate，等订阅者实际收到全部帧（含随 delta 的 progress）后再放行保存，避开上述已知窗口，使结果确定。无 sleep 定时。
- 新增 `tests/test_collaboration_stream_wizard_e5c3_postgresql.py`（PG 5 项，双实例，只启动订阅实例引擎，TCP，真实中央校验器）：
  - 帧租约先持锁 → 撤权被阻塞（`pg_blocking_pids`）→ 后续帧拒绝，且无空闲事务；
  - 撤权先持锁 → 已产出 delta 的帧租约被阻塞 → 不发出，运行体仍 running；
  - 撤权先持锁 → done 帧租约被阻塞 → 不发出；
  - 另一实例撤权、本实例零通知 → 拒绝；
  - 续写向导的运行体在真实发送 lease 内持共享锁时，订阅者 A 的快照在 send 内持锁，订阅者 B 的快照照常发出，运行体发送完成，三方都收到 done。
- 负对照：备份为 `/tmp/nc_e5c3_<ts>_neg_*.bak`，每项改后立即还原，`shasum -a 256 -c` 全部 OK。
  - N1 done guard 改用构建戳 → characters 正例 done 变 permission_denied；
  - N2 运行帧只读核对去掉续写来源复核 → 来源改写用例失败（帧被发出，流等待至超时）；
  - N3 向导跳过 `_verify_task` → 伪造凭据用例失败；
  - N4 发布匹配放宽为 same_execution → 形状、伪造用例失败；
  - N5 `_stream_readable` 恒真 → 形状用例失败；
  - N6 去掉 `_make` 仅发布形状校验 → 形状用例失败；
  - N7 `stream_guard` 去掉完成收据核对 → 形状用例失败；
  - PG P1 只读核对改为写锁 → 两订阅者用例超时；
  - PG P2 去掉租约成员/可见性条件并让向导 `require_task_read` 直接返回 → 撤权先持锁、跨实例两项失败。帧租约先持锁一项仍通过，它验证的是锁串行化，与 E5-c1 一致。

验证（精简策略，未跑全量）：
- `uv run ruff check .`：All checks passed。
- SQLite 31 个文件逐文件 3 路并行：772 passed，0 failed。含 e5c3 54、wizard 系 9 个文件 107、e4g3c 89、e5c1 39、e5c2 56、e5b 24、e5a 两个文件 63、e4g3 57、e4g3b 64、ai_guard 85、send_guard 23、task_sse 四个文件 30、task_stream 两个文件 4、tasks 32、agent_api 11、agent_runtime 20、recycle_bin_tasks 13、sqlite_cancel_rollback 1。之后把 `hashlib` import 移到模块顶部（纯整理），复跑 e5c3 正例与形状用例 10 项通过。
- PostgreSQL 16（隔离库，逐文件单进程顺序）14 个文件：62 passed、1 xfailed（既有）。含 e5c3 5、e5c1 4、e5c2 4、e5b 2、task_sse_scope 7、collaboration_task 6+1x、paused_task 9、ai_guard 4、e4g3c 5、wizard_atomicity 2、wizard_dispatch 4、wizard_resume_identity 2、e4g3 4、e4g3b 4。

遗留：
- **待确认（已知窗口）**：未决阶段的帧在本任务收紧可见性的保存之后才发送时失败关闭。实测在测试环境几乎必然发生：最后一个 delta 及随之的 progress 与保存紧挨着，运行体不等订阅者。生产中，替换已有角色或世界观的协作者向导流会以较高概率以 `permission_denied` 结束，而不是 done。无泄露，已提交内容与终态不受影响，重订阅可得已结束回放。消除需要作者确认是否采纳类似后继来源的「同一执行戳后继」机制，或调整 E5-b 的可见性取舍。E5-f 开放向导前应先定。
- **既有行为（E4-g3c，非本片引入）**：续写角色或世界观并整体替换已有条目时，`verify_event` 的 done 核验经 `verify_resume_source` 比较来源 `ai_history_epoch` 与本任务保存后已递增的 epoch，必然失败，走收据受限收尾（作者同样，「结果未发布」）。因此本片的续写受控流用例使用大纲向导。
- 首个事件核验前订阅的已知窗口同 E5-c1（E5-e/f）；收据收尾帧（E5-d）、生产路由（E5-f）、前端（E5-g）未做。
- 规范同步：error-handling.md 新增「受控任务流的向导运行中帧与 done 帧证明（E5-c3）」条目，并在 E5-c1 条目的 `_stream_readable` 说明中补「`wizard.*` 只接受 E5-c3 的两种」；outline.md「向导的大纲模式与进度」补受控流说明。

### 复核结论（E5-c3，2026-11-13，check agent）

- 仅发布 guard 的新戳：`completed_stamp` 在 `save_wizard_result` 的同一事务内读取，位于 bump 与 Task 收据 UPDATE 之后、commit 之前。该事务已经 `require_write_execution` 持有可见性独占门禁（`lock_visibility_write`），其他执行无法插入改动，所以读到的只是本任务自己的结果。提交确认未知时，`confirm_wizard_commit` 会再次 `require_visibility_stamp(state.stamp)`。提交后若有任何可见性变化，帧租约内 `require_visibility_stamp` 都会拒绝（`test_done_frame_denied_when_visibility_changes_after_save`），不存在冒签。`publication_only` 在 `_verify`/`prepare_provider`/`require_task_write` 三处都会拒绝。
- 收据核对：`_wizard_marker_matches` 要求字段集合恰好相等并逐项校验类型；`verify_wizard_completion` 要求键集合恰好相等，并对原凭据逐键比较类型和值，同时整份断点与「原断点去掉 pending 再加上收据」逐字一致。审查发现形状用例存在缺口（**已补**）：原 `pytest.raises(Exception)` 范围过宽，并且收据只覆盖缺 5 个键。现改为：缺失任意一个收据字段（全部 12 个）都拒绝；对真实收据缺任一键、夹带 `grant`、改 nonce、actor_id 改为字符串时，要求 `stream_guard` 由 `verify_wizard_completion` 自身拒绝（code=`wizard_result_uncertain`），不依赖后续 `_make` 形状校验兜底。负对照：把收据核对放宽为「只比较 marker 键的值」后，用例在 `drop_stream_hash` 处失败。备份为 `/tmp/nc_e5c3chk_1794509033_wsvc.bak`，还原后 `shasum -a 256 -c` OK。
- 形状与匹配：`_stream_readable` 对 `wizard.*` 只走 `_wizard_stream_shape`。`publication_execution_matches` 的向导分支位于通用 `execution == original` 之前，所以向导比以前更严格（以前通用分支可读，但 hub 来源是裸执行身份，实际已经失败关闭）。批量分支、单章、分析、大纲和实体的分支逐字未变；`_make` 只额外允许向导声明 `publication_only`。
- 作者路径：引擎在作者任务上同样会调用 `stream_guard`。构造条件（`job.guard` 由 build 对 pending.active 构造，戳、续写来源、绑定为同一对象；done 时收据由同一凭据派生）都是 `verify_event` 已核验内容的子集，正常流程不会出现新的失败。test_wizard 等作者用例全部通过。
- 共享锁：`_lock_active_project` FOR SHARE、`_check_task` read、续写来源 Project/Task FOR SHARE（协作者走 `require_project` 非写分支）、`_verify_bindings(write=False)`，均为共享锁。PG 用例中两订阅者与发送 lease 互不阻塞。
- 测试质量：无 sleep、xfail、skip；撤权矩阵全量参数化；已知窗口用例断言现状，并在 docstring 写明修复时需要翻转。
- 规范修正：error-handling.md 原写「大纲/职业等不改可见性的保存不受影响」，与实测不符。实测发现，替换已有职业时 careers 保存同样递增 epoch/revision（world、careers 第二次运行 epoch 都从 0 变为 1）。已改为「仅大纲保存及无已有条目、无候选竞争的首次设定保存不受影响」，并把续写遗留问题的范围扩到职业。
- **既有问题核实（未修，待用户决定）**：用临时用例（已删除）复现。作者续写角色向导并替换已有角色时，任务 succeeded，error 为「向导结果已保存；执行授权或设置已变化，结果未发布」，内容已写入，done 不发出；作品没有已有角色时正常。根因：`load_resume_source` 的「来源 `input.ai_history_epoch` == 当前 epoch」是发送前检查，本应只拦截收紧后继续发送旧派生内容。但保存后的核验（`WizardHandler.verify_event(done)`、`verify_finalization`、`confirm_wizard_commit`，以及受控流 completed guard 的 `_verify_resume`）也经 `verify_resume_source` 复用这项检查，而本任务自己的保存（`targets` 非空或候选竞争时 `bump_visibility_revision(tighten=True)`）已经把 epoch 推进了，所以必然失败。影响范围：角色、世界观、职业的续写，只要替换已有条目或出现候选竞争，作者与协作者都受影响；大纲不受影响。该检查在 HEAD 中不存在，由本分支 E4-g3c 引入。修复方案（推荐 A）：A. 保存后的核验改为把来源 epoch 与**构建戳** epoch（`job.guard._stamp.epoch`，即保存事务锁内已经核对过的戳）比较，同时保留 `require_visibility_stamp(保存事务内新戳)`。两段证明串起来，可以确认 epoch 在两者之间的唯一变化是本任务自己的保存。实现上需要给 `verify_resume_source`/`load_resume_source` 增加 `expected_epoch` 参数，completed guard 也要携带该 epoch；会改动 wizard_service、handlers/wizard.py、collaboration_ai_guard 三处，并给现有用例补上「续写角色替换已有条目」的正例和「保存后他人收紧」的反例。B. 保存后不再核对续写来源，以收据加 stream_hash 作为证明，但 `_check_resume_identity` 需要为仅发布 guard 开放例外，改动面更大。
- 回归：`ruff check .` 通过。SQLite 24 个文件、3 路并行，共 552 passed（e5c3 54、wizard 系 9 个文件 107、e4g3c 89、e5c1 39、e5c2 56、ai_guard 85、send_guard 23、task_sse 四个文件 30、task_stream 两个文件 4、tasks 32、agent_runtime 20、recycle_bin_tasks 13）。PostgreSQL 逐文件顺序跑 4 个文件，共 16 passed（e5c3 5、e4g3c 5、wizard_resume_identity 2、e5c1 4）。

## 进度记录：2026-11-13 续写向导结果未发布修复（方案 A）

- 问题（E4-g3c 引入，E5-c3 复核「既有问题」）：续写角色/世界观/职业向导并替换已有条目（或候选竞争）时，本任务自己的保存 `bump_visibility_revision(tighten=True)` 推进 epoch，保存后的四处核验仍经 `load_resume_source` 拿来源 `ai_history_epoch` 与**当前** epoch 比较，必然失败 → 收据受限收尾（succeeded + 「结果未发布」，done 不发出）。作者与协作者都受影响，大纲不受影响。
- 修复：`load_resume_source` / `verify_resume_source` 增加 `expected_epoch`（None = 与当前 epoch 比，受理/构建/发送前/保存事务内保持不变；非 int 或负数失败关闭）。`WizardJob.resume_epoch` = 构建戳 `scope.stamp.epoch`（仅续写）。保存后路径传构建 epoch：`WizardHandler.verify_event` 的 done（含 `verify_finalization`）、`confirm_wizard_commit`、done 帧仅发布 guard——`SendGuard._resume_epoch`，经 `for_task(..., publication_only=True, resume_epoch=…)` 携带；`_make` 要求续写的发布 guard 必须携带、`0 ≤ resume_epoch ≤ 新戳 epoch`，非发布 guard 不得携带；`stream_guard` 缓存键加入该值。证明链：保存事务内 `require_visibility_stamp(构建戳)` + 当前 epoch 核对 ⇒ 来源 epoch == 构建 epoch；保存后 `require_visibility_stamp(保存事务内新戳)` 仍成立 ⇒ 中间唯一变化是本次保存。未放宽：保存后他人收紧仍不发布，构建后保存前他人改 epoch 仍拒。
- 测试：新增 `tests/test_wizard_resume_publication.py`（11 例：角色/世界观/职业 × 作者/协作者续写替换已有条目 → succeeded、无 error、实时 done 帧，且断言 epoch 确已推进；保存提交后（钩在 `save_wizard_result` 返回后、不持锁）作者收紧 → error=收据说明、无 done、内容已保存；构建后保存前收紧 → failed、内容不变；单元：verify_resume_source 构建 epoch 通过、当前 epoch/错 epoch/非法值拒绝，verify_event 与发布 guard `_verify_resume` 被改 epoch 拒绝，`for_task` 构造约束）。`test_collaboration_stream_wizard_e5c3.py::test_wizard_resume_frames_pass_proof_over_tcp` 参数化增加 characters（替换 _world 已有甲、乙），断言 done、`resume_epoch` 与新戳 epoch 关系；`_resume_source/_launch_resume` 增加 kind 参数（默认大纲，PG 文件导入不变）。
- 负对照（备份 /tmp/wizard_service_negctl.*，还原后 `shasum -a 256 -c` OK）：①保存后改回与当前 epoch 比 → 6 个正例 + 单元 + e5c3 两个 characters 续写失败（大纲续写仍过）；②`expected_epoch` 比较恒真 → `test_post_save_checks_bind_build_epoch` 失败。
- 回归：ruff 通过。SQLite（3 路并行）：e5c3 56、e4g3c 89、wizard 系 10 个文件 118（含新文件 11）、ai_guard 85、send_guard 23、tasks 32、agent_runtime 20、recycle_bin_tasks 13，全部通过。PG（顺序）：e5c3 5、e4g3c 5、wizard_resume_identity 2、wizard_atomicity 2、wizard_dispatch 4、ai_guard 4，全部通过。
- 规范：error-handling.md「向导协作（E4-g3c）」补充代次两种语义；E5-c3 节把续写问题改为已修复并补缓存键说明。outline.md 无相关描述，未改。

## 进度记录：2026-11-13 向导同执行戳后继

范围：消除 E5-c3 已知窗口。设定类向导（角色/世界观/职业）在保存前产出、但在本任务收紧可见性的保存提交后才实际发出的帧（最后的 delta、随之的 progress、保存后才订阅的快照），原先因帧投影戳与构建 guard 戳都失效而被拒为 `permission_denied`。生产路由、作者订阅管线、大纲/单章/分析/批量/实体的规则均未改。

做了什么：
- `collaboration_ai_guard`：新增 `wizard_successor_eligible`（仅 `wizard.characters/world/careers` 的未决形状构建 guard，非仅发布）与纯判定 `wizard_publication_successor(guard, execution, later_guard, later_execution, frame_stamp)`：后者须为同一执行的 completed 仅发布 guard（即 E5-c3 done 帧的 `WizardJob.publication_guard` 缓存），两端各自与发布身份逐字相等、`same_execution` 且游标不变；同 actor、模型绑定、cipher、私网开关、续写来源对象，续写时 `resume_epoch` 恰为构建戳 epoch；完成断点恰为「原断点去掉未决凭据 + 由同一凭据逐键（含类型）派生的完成收据」；新戳与构建戳同作品/同删除代次且不回退；帧投影戳必须等于构建戳或新戳之一。
- `stream_evidence.verify_stream_frame`：投影戳与当前作品戳只差 revision/epoch 时，仅设定类向导未决帧可延后到后继分支（其余类型仍直接拒绝，E5-b 取舍不变）；构建戳已不再成立（或投影戳已延后）时不走直接/终态分支，改入 `_verify_wizard_successor`：原未决执行仍与持锁行一致 → 拒绝（他人改戳）；同一 hub 已登记的当前来源满足后继判定 → 以该 completed guard 在帧租约锁内复核（行仍 running 带完成收据用 `require_task_read`，已终态用原 hub 登记的终态证据 + `require_terminal_read`，未登记则 `TerminalEvidencePending`）；完成来源尚未发布 → `PublicationSourcePending`（复用 E5-c2：退出整个租约，等 `hub.source_changed` 或终态 latch，受 `frame_timeout` 约束；hub 关闭后不再等待 → 拒绝）。不以持锁行补签，不跨执行/跨 hub，流读取全程共享锁。批量分支逐字未动（批量 guard 不满足 `wizard_successor_eligible`）。
- 构建戳仍成立（大纲、无已有条目且无候选竞争的首次设定保存）时不进入后继分支，路径与之前一致。
- `handlers/wizard.py` 仅补模块文档说明。

作者路径影响：
- 作者生产订阅（非受控）完全不变。受控流（作者/协作者）只在「帧投影戳或构建戳已失效」时多一次纯判定与一次锁内只读复核；其他帧多一次 `wizard_successor_eligible` 纯形状判断（仅当来源为 PublicationSource）。
- 新失败面：保存已提交、done 迟迟未完成核验时，在途帧在租约外等待完成来源，超过 `frame_timeout`（5 秒）静默断开（同 E5-c2）；done 核验失败（收据受限收尾）时不会有完成来源，在途帧拒绝（原来同样拒绝）。

测试：
- `tests/test_collaboration_stream_wizard_e5c3.py`：原 `test_known_gap_running_frame_in_flight_across_tightening_save` 翻转为 `test_running_frame_in_flight_across_own_tightening_save_is_sent`（6 项）：{角色, 世界观, 职业}×编辑者、角色×作者（替换已有条目）→ 帧照常发出、最终 done、拼接文本 = 答案、epoch 确已推进且帧经后继分支（spy 全部为真）；大纲、世界观首次保存（无已有条目）→ 戳不变、不进入后继分支、照常 done。标记帧与其余正文之间加 Gate，避免订阅侧合并积压增量造成时序依赖。
- 新增 `tests/test_collaboration_stream_wizard_successor.py`（SQLite 14 项）：保存后他人再改可见性（tighten/非 tighten）→ 拒；构建后保存前他人改 epoch → 保存失败、拒、内容不变；他人收紧后伪造完成来源（nonce 不同、其余全部成立、持锁行一致、同 hub 登记）→ 等待后仍拒（承重）；保存后 {撤权, 降级, PATCH 关作者模型授权}×{有通知, 无通知} → 拒；协作者用作者模型、直接关 allow_owner_ai（不递增轮次、无通知）→ 改变前在途帧经后继发出（正对照）、改变后下一帧被拒（承重）；保存已提交而 done 未核验时在途帧在租约外等待完成来源：无变化 → 发出并 done，等待期间撤权（无通知）→ 拒；形状单元（投影戳为构建戳/新戳通过，第三方戳/None、方向颠倒、自身、nonce 不同、断点夹带键、游标推进、跨执行、换删除代次均拒；大纲构建 guard 不适用）。
- 新增 `tests/test_collaboration_stream_wizard_successor_postgresql.py`（PG 4 项，双实例，只启动订阅实例引擎，TCP，真实中央校验器）：在途后继帧在实际 send 内持锁 → 撤权被阻塞（`pg_blocking_pids`）→ 放行后撤权提交 → 后续帧拒且无空闲事务；撤权先持锁 → 在途后继帧租约被阻塞 → 撤权提交后不发出；另一实例撤权、本实例零通知 → 拒；两订阅者（A 的在途帧、B 的快照）在租约外等待完成来源时，运行体的 done 核验与终态写入照常完成，随后两者都收到 done。
- 无 sleep 定时（Gate/Event + `wait_until` 上限）。

负对照（备份 `/tmp/nc_wsucc_1794535251_{guard,evidence}.bak`，每项改后立即还原，`shasum -a 256 -c /tmp/nc_wsucc_1794535251.sha` 全部 OK）：
- N1 后继判定去掉 `same_execution`/游标与完成收据逐项核对 → 伪造完成来源用例、形状用例失败；
- N2 后继分支去掉 `latest.guard.require_task_read/require_terminal_read`（锁内当前授权复核）→ 保存后他人改可见性 2 项、直接关作者模型授权 1 项失败（撤权/降级/PATCH 关授权由租约成员复查另行拦截，与 E5-c2 结论一致）；
- N3 去掉后继分支（`_wizard_successor_source` 恒假）→ 翻转正例 4 项（角色/世界观/职业编辑者、角色作者）与等待完成来源 2 项失败；大纲与首次保存仍过。

验证（精简策略）：
- `uv run ruff check .`：All checks passed。
- SQLite 27 个文件 3 路并行：607 passed，0 failed（e5c3 60、successor 14、e5c2 56、e5c1 39、e5b 24、e4g3c 89、wizard 系 10 个文件 118（含 wizard_resume_publication 11）、ai_guard 85、send_guard 23、task_sse 四个文件 30、task_stream 两个文件 4、tasks 32、agent_runtime 20、recycle_bin_tasks 13）。
- PostgreSQL 16（隔离库，逐文件顺序）11 个文件：43 passed（successor 4、e5c3 5、e5c2 4、e5c1 4、e5b 2、e4g3c 5、wizard_atomicity 2、wizard_dispatch 4、wizard_resume_identity 2、ai_guard 4、task_sse_scope 7）。

遗留：首个事件核验前订阅的已知窗口同 E5-c1（E5-e/f）；收据收尾帧（E5-d）、生产路由（E5-f）、前端（E5-g）未做。规范：error-handling.md E5-c3 节「已知窗口」改为「同执行戳后继（已解决）」，写明规则、证明链与禁止的改法。

## 复核记录：2026-11-13 方案 A + 向导同执行戳后继（check agent）

- 方案 A 证明链成立：受理、构建、发送前与保存事务内仍以 `expected_epoch=None` 和当前 epoch 比较，语义未变。保存事务在门禁内先执行 `require_visibility_stamp(构建戳)`，再按当前 epoch 核对来源，最后写入并读取新戳。保存后的四处核验（verify_event done/verify_finalization、confirm_wizard_commit、发布 guard `_verify_resume`）均同时要求「新戳仍成立」和「来源 epoch == 构建 epoch」。若构建后、保存前被他人改动，保存会失败；若保存后被改动，新戳失效。`expected_epoch` 为非 int、bool 或负数时失败关闭；`_make` 禁止非发布 guard 携带 `resume_epoch`，也禁止该值晚于新戳。
- 后继规则只对设定类向导的未决构建 guard 生效（`_wizard_successor_source` 为假时 `stale_stamp` 恒为假）。单章、分析、批量、大纲、实体的 E5-b、E5-c1、E5-c2 分支逻辑与原来一致；批量后继分支未改动。他人改戳由三层拦截：原执行仍为 running 时直接拒绝；保存前改戳会使保存失败、没有完成来源；保存后改戳会让 completed guard 在锁内复核新戳时失败。不使用持锁行补签：guard 只取 hub 登记的来源，并要求 `_publication_hub(latest) is hub` 且 `same_execution`。伪造完成来源会被拒，依据是收据键集合必须等于凭据键集合加三项结果键，且每个键的类型和值都与原未决凭据逐一相等（包括 nonce）；断点只能是「去掉未决凭据 + 收据」。等待在租约外进行，受 `frame_timeout` 约束；`terminal_ready` 只在 close 时置位，hub 关闭后直接拒绝，不会空转。作者生产订阅管线未改动。
- 补充测试：①`test_wizard_resume_publication.py::test_resume_commit_ack_lost_confirms_with_build_epoch[owner|editor]`。此前 confirm_wizard_commit 的续写分支没有用例；新用例模拟保存真实提交但确认丢失，断言 confirm 返回 True、任务 succeeded、发出 done，且 epoch 已推进。负对照：把该处改回与当前 epoch 比较后，2 例失败；备份 /tmp/chk_confirm_negctl.C7xB9Z，还原后 sha 校验 OK。②形状用例增加「私网开关不同 → 非后继」。负对照：去掉 `_allow_private` 比较后该例失败；备份 /tmp/chk_succ_negctl.CwfMgE，还原后 OK。
- 环境：无残留 pytest 进程；实现方备份 /tmp/wizard_service_negctl{,2}.* 与 /tmp/nc_wsucc_1794535251_{guard,evidence}.bak 的哈希与现文件一致。
- 回归：ruff 通过。SQLite 27 个文件、3 路并行，609 passed。PG 11 个文件顺序执行，43 passed（successor 4、e5c3 5、e5c2 4、e5c1 4、e5b 2、e4g3c 5、wizard_atomicity 2、wizard_dispatch 4、wizard_resume_identity 2、ai_guard 4、task_sse_scope 7）。

## 进度记录：2026-11-13 E5-d 收据收尾 done 帧

范围：按 design.md §6.3「E5 裁决」，把单章 / 分析 / 批量 / 向导的收据受限收尾终态帧从「不含任务快照的 error」改为带任务快照的 `done`，附「结果未发布」说明。`_DRAFT_ONLY_KINDS` 的失败收尾不在范围内（仍为 failed）；`recover()` 路径（无订阅者）不变；生产路由（协作者仍 404，E5-f）与作者生产订阅管线未改；strict xfail 未动。

实现：
- `engine.py`：新增 `_RECEIPT_UNPUBLISHED`（类型集合 × 固定 NOTE × 固定 DETAIL）与 `receipt_unpublished_detail(task)`：只有 `status=succeeded` 且 `error` 恰为本类型 NOTE 的行才算收据收尾成功行（NOTE 只由四个受限收尾函数写入）。`_finalize` 在十二处收尾分支（含提交确认丢失、终态核验异常回退）记下返回的已提交行 `receipt_row`；`finally` 中经新增 `_receipt_terminal_frame` 按原 UUID/statistics_key/user/project/kind/queued_at/终态/`execution_key IS NULL` 重读，生成终态帧并以该行 `capture_task_view` 设为 `hub.source` 后 `hub.close`。重读失败退回旧帧 `error {task: null, detail}`；重读期间若被取消，先关 hub 再原样传递取消（`asyncio.shield`）。
- `stream_evidence.persisted_replay_events`：收据收尾成功行映射为 `done {"task": out, "result": None, "unpublished": True, "detail": DETAIL}`，实时收尾与回放、E5-b 持久回放证明共用此一处映射。批量按断点记 failed/cancelled/interrupted 的行是普通 `error {task, detail}`（原来 task 为 null）。
- 证明：只认已提交行。受控订阅的终态帧来源是 `TaskViewEvidence`，发送时走 E5-b `_verify_persisted_replay`（帧租约锁内重算投影、`_bound_task_visible` 按订阅原绑定、`project_payload` 的本人项目校验）；不签发 `PublicationSource`/`TerminalEvidence`，不要求当前可见性戳/模型绑定。作者生产订阅仍经 `checked_frame`。
- 前端：`TaskEnd` 成功分支新增 `{status: 'succeeded', result: null, unpublished: true, detail}` 变体，`subscribeTask` 识别 `unpublished: true`（缺 detail 时用「结果已保存，请刷新后查看」）。原作者端行为：error 帧 → `status: failed` + detail（编辑器/向导显示「生成失败：正文已保存，请刷新后查看」，分析提示「本章分析失败：…」）。现在：章节编辑器按原成功分支重新读取已保存正文（result 为空即走 `chapterApi.get`），提示改为 `message.info(detail)`；向导、分析、批量不报告条数/完成，改为 `message.info(detail)`；任务结果弹窗显示 detail。`useTaskStream` 未改。原提示文案（detail）保持可见。

测试：
- 新增 `tests/test_collaboration_receipt_done_e5d.py`（SQLite 29）：四类 × {编辑者受控, 作者受控, 作者生产入口} 的实时收尾与回放（回放帧与实时 done 逐字相等；done 只有 task/result/unpublished/detail 四键，result 为 null，帧中不含 MARK；Task 行 succeeded + NOTE 且持久 result 非空）；作者生产入口保留「正文已保存，请刷新后查看」；done 核验前撤权/降为 viewer → 只得 `permission_denied`、无 done、无 unpublished；done 帧已产出、发送前撤权（通知关闭）→ 发送时拒绝，未变化时照常发送。触发方式：末尾 Gate 使运行体在保存前停住，订阅者经帧记录器（hub 发布序列与已收帧比对，无计时）收齐后放行；done 核验前再等收齐，然后作者经独立会话递增可见性 revision。
- 新增 `tests/test_collaboration_receipt_done_e5d_postgresql.py`（PG 8）：四类真实 PG 收尾 + 回放；章节/向导两种先后顺序：done 帧已在实际 send 内持帧租约 → 另一实例撤权在数据库层等待（pg_blocking_pids），帧照常发出后撤权 204；撤权先持锁未提交 → 已产出 done 帧的帧租约等待，撤权提交后拒绝为 `permission_denied`。
- 调整既有断言（仅此一处）：`test_wizard_resume_publication.py::test_resume_tightened_after_save_is_not_published[owner|editor]` 原断言「没有任何 done 帧」，现改为「最后一帧是 result=null、unpublished=true、detail=WIZARD DETAIL、task.error=NOTE 的 done，且不存在 result 非空的 done」；其余断言（行状态、内容已提交）不变。e4f/e4g/e4g2/e4g3c 只定义了收据常量，未断言帧形状，无需改动。
- 前端：`api/tasks.test.ts` 新增 2 例（未发布 done 映射与固定说明兜底、普通 done 不带 unpublished）。
- 负对照（均备份唯一命名 /tmp 文件，还原后 `shasum -a 256 -c` OK）：①`persisted_replay_events` 未发布分支改为携带 `task.result` → 16 例失败（12 个实时/回放 + 4 个发送时未变化），备份 /tmp/e5d_nc1_stream_evidence.gdjOpK。②只去掉帧租约的可见谓词/成员核对与 `_bound_task_visible` → 发送时撤权仍被 `project_payload` 的本人项目校验拒绝，8 例通过（纵深防御，记录在案；备份 /tmp/e5d_nc2_stream_{lease,evidence}.*）。③再跳过整个 `verify_stream_frame` → 发送时撤权 4 例失败，备份 /tmp/e5d_nc2b_stream_lease.AdGtpr。

回归：
- ruff 通过；前端 `npm run lint` 通过、`tsc -b` 无错误、`npm run test` 206 文件 1443 例通过。
- SQLite 逐文件 3 路并行：receipt_done_e5d 29、e5b 24、e5c1 39、e5c2 56（并行时 `test_held_batch_frame_is_sent_when_nothing_changes[analysis]` 在流开始等快照时超时 1 次，与收尾无关；单独重跑该用例 4 passed、整文件 56 passed）、e5c3 60、wizard_successor 14、e4f 41、e4g 55、e4g2 59、e4g3c 89、wizard_resume_publication 13（调整断言后）、tasks 32、task_sse_engine_http 1、task_sse_forgery 3、task_sse_frame_guard 10、task_sse_scope 16、task_stream_batching 2、task_stream_evidence 2、agent_runtime 20、recycle_bin_tasks 13。
- PG（逐文件顺序）：receipt_done_e5d 8、e5b 2、e5c1 4、e5c2 4、e5c3 5、wizard_successor 4、e4f 7、e4g 5、e4g2 6、e4g3c 5、task_sse_scope 7。

遗留 / 已知窗口：
- 收据收尾没有执行体终态证明，保存提交后才实际发送的在途运行帧（单章/分析/向导最后的 delta、批量末章保存后的 chapter_end/progress）在受控流里会以 `permission_denied` 结束（重订阅可得回放）；作者生产流不受影响。前端收到 permission_denied 后重订阅一次（E5-g）可消化。
- 回放快照帧的 `text` 仍是持久 `output_text`（与 E5-b 详情/输出接口一致，含已流式输出的正文）；本片只保证终态帧不带收据重建的 result。
- 生产路由开放（E5-f）、前端协作者任务面板（E5-g）未做；`recover()` 路径保持不含快照的 error（无订阅者）。
- 规范：error-handling.md 新增「收据收尾的终态帧（E5-d）」，并改写单章、向导收据收尾条目中「不含快照的 error」；batch-generate.md、outline.md 补充终态帧说明。

### 复核结论：2026-11-13 E5-d（check agent）

- 帧内容：done 只有 task/result/unpublished/detail 四键，result 恒为 null。临时脚本实际打印了四类编辑者受控流的终态 task 快照：章节和分析的 `checkpoint` 经作品投影后为 `{}`（完成收据、日志和模型实例键、令牌轮次均不出现）；向导为 null；批量为 `checkpoint_out` 公开断点（逐章标题、状态、字数），与运行中 snapshot/progress 帧以及详情接口已向同一订阅者公开的内容一致，不含 summarize 结果。判定可接受。快照 `error` 只有固定 NOTE，其他字段（title/stage/progress 等）不含正文。
- 识别：`receipt_unpublished_detail` 只认 `status=succeeded` 且 `error` 与本类型 NOTE 完全相同的行；NOTE 只由四个受限收尾函数写入。普通成功的 `_mark_followup_warning` 按原 `execution_key` 做 CAS，碰不到 execution_key 为 NULL 的收据行，所以不会改掉 NOTE。重读按 id/UUID/user/project/kind/queued_at/status 做筛选，并要求 `execution_key IS NULL`，同时 `populate_existing`；成功行还必须带 NOTE。重读失败时退回 `error {task: null, detail}`（各收据分支的 out 都是 None），被取消时也先关 hub。均判定安全。
- 帧租约承重：实现方的负对照②说明，撤权场景下即使去掉帧租约复查，仍会被 `project_payload` 拦住。复核时给 `test_held_receipt_done_is_rechecked_at_send` 加了 `change=viewer` 参数（已产出 done、发送前降为 viewer；成员仍是 accepted，`project_payload` 会放行），参数由 `revoke: bool` 改为 `change ∈ {None, revoked, viewer}`，共 12 例。负对照：去掉帧租约的 `task_visible_to_actor`、成员原轮次/角色核对和 `_bound_task_visible` 后，viewer 4 例失败，其余 8 例通过。由此证明这层复查独立承重。备份文件 /tmp/e5dchk_nc_lease.wIPRfm、/tmp/e5dchk_nc_evidence.KwfD45，还原后 `shasum -a 256 -c` 结果 OK。
- 前端：`subscribeTask` 的 error 帧带快照时，状态取自 `task.status`（failed/cancelled/interrupted），兼容批量带快照的 error。未处理 unpublished 的消费方有：TaskPanel 输出弹窗（只看状态标签和输出，不读 result）、Agent、大纲和实体弹窗（这几类不会走收据收尾），无遗漏。修复了 `TaskResultModal`：未发布说明原来用红色错误 Alert 展示，现改为 info（失败等情况仍是 error）。保留但未改的点：向导在 unpublished 时不调用 onDone，父组件不会自动 reload 或标记步骤完成，只显示「请刷新后查看」。这与旧行为（按失败处理）同样保守，是否自动刷新留到 E5-g 决定。
- 既有断言只改了 `test_wizard_resume_publication.py::test_resume_tightened_after_save_is_not_published`，其余断言没有放宽；e4f 的 `RECEIPT_DETAIL` 常量在测试中未被引用。
- 已知窗口已如实写入本记录和 error-handling.md。规范的回归说明补充了「发送前降为 viewer」用例。
- 复核回归：ruff 通过；前端 lint、`tsc -b` 通过，`npm run test` 206 个文件、1443 例全部通过。SQLite 逐文件 3 路并行：receipt_done_e5d 33（含新增 viewer 4 例）、e5b 24、e5c1 39、e5c2 56、e5c3 60、wizard_successor 14、wizard_resume_publication 13、tasks 32、task_sse_engine_http 1、task_sse_forgery 3、task_sse_frame_guard 10、task_sse_scope 16、task_stream_batching 2、task_stream_evidence 2、agent_runtime 20、recycle_bin_tasks 13。PG 逐文件顺序：receipt_done_e5d 8、e5b 2、e5c1 4、e5c2 4、e5c3 5、wizard_successor 4、task_sse_scope 7。

## 进度记录：2026-11-13 E5-e 早订阅

范围：修复协作者受控订阅在「排队中 / 领取瞬间 / 首个已核验事件之前」被判 `permission_denied` 的缺口（E5-f 开放生产路由前的硬前置）。生产路由 `api/v1/tasks.py` 未改（协作者仍 404，E5-f 开放）；作者生产订阅管线、帧序列与内容未变；strict xfail 未转正。

做了什么：
- `stream_evidence`：新增两种只证明无输出帧的来源（进程内弱引用登记、不序列化、不授予发送/预检/写入能力）：
  - `ClaimSource`（`issue_claim`）：引擎在领取 CAS **已确认提交**后（含提交确认丢失后按原执行键证实为 running 的分支）、发布领取帧之前，以领取者在原 queued 行锁内按本次 CAS 执行键构造的同一 `TaskExecutionIdentity` 与以该执行登记的 hub 签发（`TaskEngine._claim_source`；只对实现 `stream_guard` 的类型——单章、分析、批量、向导、大纲、实体——签发，Agent/知识库/拆书仍为裸执行身份）。`_verify_claim_frame`：非终态只许无输出形状（领取 progress、排队位次清空的过渡帧、`reset {}`、正文为空的快照），持锁行须 `matches_running_base(领取身份, 行)`；快照摘要按持锁行或同一执行领取时断点/游标（`_claim_time_task`）重算 `to_out` + `project_task_out` 后除实时 progress/stage 外逐字一致；行为另一次领取或仍排队 → 直接拒绝；同一执行已合法推进（`_same_execution_advancing`：单章保存已移除输入占用指针而终态未提交，或已有管理员取消意图）→ `TerminalEvidencePending` 等原 hub 终态证明；行已终态 → 原 hub `TerminalEvidence`（`same_execution` + 视图一致）。领取来源下终态事件只接受 `error`。
  - `QueuedSource`（`issue_queued`）：`_broadcast_queue_positions` 为排队中、无执行键的 hub 在发布位次前登记；`_verify_queued_frame` 只接受同一 UUID/入队轮次的排队 progress（四键、位次 null 或非负整数）。
  - `_verify_late_queued_snapshot`：排队快照投影后、发送前同一轮次被领取/结束时，只接受正文为空、动态字段为全新排队形状的快照，不变字段按持锁行重算一致。
  - 所有新分支都在帧租约内按订阅原绑定 `_bound_task_visible` 复核并 `_owner_ai_current` 直接核对 `allow_owner_ai`；原排队快照分支也补了作者模型授权核对。
- `engine`：`_claim_and_run` 的 `hub.source = execution` 改为 `self._claim_source(execution, hub)`；`_broadcast_queue_positions` 登记 `QueuedSource`；`subscribe` 在受控订阅读到已提交 running 行而领取来源尚未登记（领取提交与登记之间）时，不持读取会话、在 `claim_source_wait_seconds`（5 秒）内等 `hub.source_changed`/终态（`_await_claim_source`），再按最新持久行重新投影正文为空的快照；超时/无来源关闭则失败关闭。

作者路径影响：`hub.source` 赋值不发布事件；领取与排队的帧序列、内容、时序不变（发布前多一次同步登记）。作者生产订阅不读 `hub.source`。领取来源只是登记对象，不访问数据库。

测试：
- 翻转已知窗口用例：`test_collaboration_stream_running_e5c1.py` 的 `test_known_gap_queued_subscription_denied_at_claim_progress_frame` → `test_queued_subscription_receives_claim_frames_and_done`、`test_known_gap_subscription_before_first_verified_event_is_denied[chapter|analysis]` → `test_subscription_before_first_verified_event_gets_empty_snapshot[...]`；`test_collaboration_stream_wizard_e5c3.py` 的 `test_known_gap_subscription_before_first_verified_wizard_event_is_denied` → `test_subscription_before_first_verified_wizard_event_gets_empty_snapshot`。其余文件无 `known_gap` 用例。
- 新增 `tests/test_collaboration_stream_early_subscribe_e5e.py`（SQLite 83）：排队订阅（含领取帧）/ 领取瞬间订阅（在领取提交、会话退出后、登记来源前停住）/ 首个事件前订阅（build_job 后停住）× 单章、分析、批量、向导、大纲、实体 × {编辑者, 作者}（36）；排队位次帧 ×2；排队快照恰逢领取 1；撤权矩阵 {排队位次帧, 领取帧, 首事件前快照} × {撤权, 降级, PATCH 关作者模型授权, 改密, 重邀} × {有通知, 无通知} 全量 30 + 正对照 3；直接关 `allow_owner_ai`（不递增轮次、无通知）× 三时点 × {改, 不改} 6；领取行身份伪造（换执行键/输入/入队轮次、未登记来源）、无输出形状伪造（delta、带正文快照、篡改摘要、多键 progress、done 等 10 种）、排队来源伪造、未开放类型不签发；保存→终态窗口等待终态证明 1。无 sleep，Gate/Event + `wait_until` 上限。
- 新增 `tests/test_collaboration_stream_early_subscribe_e5e_postgresql.py`（PG 4，双实例，只启动订阅实例引擎，TCP，真实中央校验器）：领取事务先持锁（running CAS 后、提交前停住）→ 另一实例撤权在数据库层等待（`pg_blocking_pids`）→ 领取先提交、领取帧产出后撤权提交 → 该帧 `permission_denied`；撤权先持锁 → 领取在数据库层等待 → 撤权提交后领取锁内复核受限收尾为 failed（从未签发领取来源、模型零调用）；跨实例零通知 → 首事件前快照被拒、运行体仍 running；两订阅者 + 引擎领取：A 的排队快照在 send 内持锁时领取事务在数据库层等待 A，B 随后订阅（实测 PG 直接授予 B 共享锁，不排在领取后），放行后三方完成、无空闲事务。
- 负对照（备份 `/tmp/nc_e5e_<ts>_*.bak`，每项改后立即还原并 `shasum -a 256 -c` 全 OK）：N1 领取分支不核对领取行身份（`running_same=True`）→ 伪造帧被发出；N2a 去掉作者模型授权直接核对 → 三时点直接关授权 3 例失败；N2 去掉帧租约 Task 可见性/成员复查 + 分支内 `_bound_task_visible` + 作者模型核对 → 撤权矩阵排队/领取 16 例失败（首事件前快照仍被 `project_task_out` 的可见性复核拦住，纵深防御；只去掉租约两层时只有领取帧 8 例失败，这是追加分支内原绑定复核的原因）；N3 等待改为直接拒绝 → 领取瞬间 12 例失败；N4 不签发领取来源 → 翻转用例与排队/首事件前共 28 例失败；N5 去掉排队快照恰逢领取分支 → 1 例失败；N6 保存→终态窗口改回直接拒绝 → 1 例失败。PG：去掉全部锁内授权复查层 → 领取先持锁用例失败（撤权先持锁由领取锁内复核拦截、跨实例快照由投影复核拦截）。

过程中发现并修复：三路并行下 `test_late_queued_snapshot_sent_after_claim_commit_is_proven` 偶发 `permission_denied`。插桩定位为单章保存事务移除 `Task.input._chapter_claim` 指针后、终态 CAS 前，行仍 running 但不再逐字匹配领取身份，旧实现直接拒绝；改为 `_same_execution_advancing` 等待终态证明，并补确定性用例（N6 负对照恰好复现原现象）。

验证（精简策略，未跑全量）：
- `uv run ruff check .`：All checks passed。
- SQLite（逐文件 3 路并行，gtimeout 1500）：修复后 early_subscribe_e5e 83、batch_generate 27、e5c2 56（同一三路并行组）；其余 17 个文件 e5b 24、e5c1 39、e5c3 60、wizard_successor 14、receipt_done_e5d 33、wizard_resume_publication 13、tasks 32、task_sse_{engine_http 1、forgery 3、frame_guard 10、scope 16}、task_stream_{batching 2、evidence 2}、task_execution_fencing 11、agent_runtime 20、recycle_bin_tasks 13、wizard 30，全部通过（共 509 passed，0 failed）。
- PostgreSQL 16（隔离库，逐文件顺序）9 个文件：early_subscribe_e5e 4、e5b 2、e5c1 4、e5c2 4、e5c3 5、wizard_successor 4、receipt_done_e5d 8、task_sse_scope 7、task_execution_fencing 1，共 39 passed。新 PG 文件另连续 3 轮稳定。
- 负载下的偶发：首轮回归时我同时跑了 PG（超过 3 路），`test_batch_generate.py::test_interrupt_restores_current_chapter`（作者路径、无订阅者，首个 Gate 等待超时并伴随「刷新输出失败」）与新文件 `test_held_early_frame_is_sent_when_nothing_changes[claim]` 各失败 1 次；前者单独与连续 3 次重跑均通过。后者未能定位：随后 3 路/4 路并行共 9 轮完整文件（含插桩）未复现，帧在 build 门内被持住，不处于上面已修复的保存→终态窗口。如实记录，留待复核关注。另 4 路并行下既有 `test_collaboration_stream_batch_e5c2.py::test_held_batch_frame_is_sent_when_nothing_changes[paused]` 偶发 1 次（与 E5-d 记录的同类负载偶发一致，非本片改动路径）。

遗留：
- 排队任务被取消/系统清理时，关闭帧的来源仍是 QueuedSource/空，受控订阅收到的是 `permission_denied` 而非带快照的取消/失败终态（与原行为一致，重订阅可得回放）；如需在 E5-f 前改为可证明终态，需另行设计。
- 领取瞬间等待上限 5 秒，超时失败关闭；前端重订阅一次可消化（E5-g）。
- 生产路由开放（E5-f）、前端（E5-g）未做；E5-d 记录的「收据收尾后在途运行帧」窗口不在本片范围。
- 规范：error-handling.md 新增「受控任务流的早订阅（E5-e）」条目；E5-c1/E5-c2/E5-c3 条目中的已知窗口改为已由 E5-e 解决并指向翻转后的用例。

### 复核结论：2026-11-13 E5-e（check agent）

- **领取来源**：`issue_claim` 只在 `_claim_and_run` 中调用，时点在 CAS 提交确认之后（包括提交确认丢失后按原执行键证实为 running 的分支）、`ensure_hub` 之后、领取帧发布之前；`execution` 是在原 queued 行锁内由 `from_queued_task` 按本次执行键构造的。`_verify_claim_frame` 要求 `matches_running_base(领取身份, 持锁行)`，执行键、输入、入队轮次任一不同都会直接拒绝；未经登记的 `ClaimSource` 会因 `_claim_hub` 返回 None 被拒。允许的帧形状只有：领取 progress 的两种形状、`reset {}`、正文为空的快照；终态只接受 `error`。delta、continue、带正文快照、多键 progress、done/paused 都会被拒。来源对象本身不访问数据库，也不提供发送、预检或写入能力。判定合格。
- **等终态证明只认本执行**：`_same_execution_advancing` 要求 running、UUID、执行键、入队轮次、actor、kind 全部相同；之后只接受两种情况：移除了本执行输入中的 `_chapter_claim`（其余输入逐字相同），或已有管理员取消意图。等待受 `frame_timeout` 限制，并且发生在租约退出之后，不会持锁等待。等到后必须经过终态分支，即 `same_execution` 加持久视图逐字一致才能通过。`identity.input` 每次都从 JSON 重新解析，`pop` 不会改动共享身份。**补强**：原有伪造用例在改执行键的同时也改了输入，所以执行键这一条件本身并没有被测试覆盖。为此新增 `test_same_execution_advancing_only_waits_for_this_execution`，覆盖执行键/入队轮次/UUID/actor/kind/状态/输入各自单独变化，以及管理员取消意图只认同一执行。负对照：删掉执行键条件后该用例失败（备份 /tmp/e5echk_nc_adv.dWVAg4，还原后 `shasum -a 256 -c` OK）。
- **快照比较**：`_claim_snapshot_matches` 对持锁行和领取时的断点各做一次 `to_out` 加 `project_task_out`，然后与快照做整体摘要比较，只替换 progress（限定为 0–100 的整数）和 stage（字符串）。`QueuedSource` 要求 UUID 与 queued_at 相同，帧恰为四个键。`_verify_late_queued_snapshot` 中动态字段必须是全新排队的形状，不变字段按持锁行重新投影后逐字一致。判定严格。
- **授权**：新增的三个分支在帧租约内都会调用 `_bound_task_visible` 和 `_owner_ai_current`，最后再重验一次租约。原来的排队快照分支补加的 `_owner_ai_current` 对作者任务（无 GRANT_KEY）恒为 True，作者语义不变。
- **作者路径与非六类任务**：`_claim_source` 对 Agent、知识库、拆书仍返回原来的 `execution`。`QueuedSource` 会登记到所有排队 hub，但非受控订阅不读取 `hub.source`；受控订阅对非六类任务原本就失败关闭，排队快照走的仍是原分支。`source` setter 只会唤醒 `source_changed` 的等待方（`_await_claim_source` 会忽略 QueuedSource，批量的 `PublicationSourcePending` 被唤醒后会重新验证）。帧序列不变。
- **偶发（第 6 点）调查**：调查分三步。①4 路并行跑完整文件 2 轮，其中一次 `test_claim_source_must_match_the_claimed_row` 失败，`_send` 返回 []。日志显示提交请求后正好 5 秒出现 `deadline exceeded`，同时有连接被终止。这说明帧租约超过 `frame_timeout=5s` 后被中止，本质与 `held_early[claim]`「帧在 build 门内被持住」相同。②为查明是否有隐藏的锁持有者，我在 /tmp 下写了插件（`-p`，不改仓库）：对租约做超时快照（dump 所有协程栈和已开启事务的会话），对每条 SQL 计时，并在执行前记录其他连接的 `in_transaction`。4 路完整文件共 26 轮，加 8 路超载 1 轮。抓到的现场是：运行体停在 `_gate_build` 的 `release.wait()`；仍开着事务的会话只有租约自己；租约等待的 cursor future 已经完成。慢 SQL 中最长的一条是调度器在 autocommit 下执行的只读 `SELECT user_quotas`，耗时 3.95 秒，期间没有任何连接持有事务，属于纯粹的 CPU/IO 饥饿。其余 1–1.6 秒的慢 SQL 是合法写者（领取、build、保存、`verify_event`）短暂持有写锁造成的。没有发现长时间持锁的隐藏持有者，也没有出现未退出租约的 `TerminalEvidencePending`。③负载越高越容易出现：4 路正常负载 12 轮全部通过；机器另有外部负载时（同样 4 路，单轮 7 分钟），3 轮里有 2 例失败；8 路超载时每个进程都有失败，第一个用例总是失败（冷启动）。结论：这是环境导致的单帧租约超时，属于设计上的失败关闭（静默断开，前端重新订阅），不是 E5-e 逻辑的竞态，与 E5-c2/E5-d 记录的同类负载偶发同源。没有修改：`_mount`/`_send` 由 15 个流测试文件共用，只为测试调大 `frame_timeout` 会掩盖生产默认 5 秒下的租约时延，收益不足以抵消风险。如果以后要降低偶发，建议在测试层统一提供可选的 `frame_timeout` 参数，并单独保留一组 5 秒的用例。
- **测试质量**：SQLite 用例里没有 sleep，同步全部用 Gate/Event 加 `wait_until` 上限。PG 文件的 `_settled`/`_subscriber_b_state` 中有 10–20ms 的轮询 sleep，都是有上限的状态轮询，不是用时序做断言，可以接受。四个翻转用例都断言了：首帧是快照、空正文、running/queued 状态、最后是 done、无 `permission_denied`、正文恰为模型输出，并且 e5c1 排队用例还断言了 running progress。判定充分。
- **复核回归**：ruff 通过。SQLite 采用逐文件 3 路并行，共 19 个文件全部通过：early_subscribe_e5e 84（含新增 1）、e5b 24、e5c1 39、e5c2 56、e5c3 60、wizard_successor 14、receipt_done_e5d 33、tasks 32、task_sse_{engine_http 1、forgery 3、frame_guard 10、scope 16}、task_stream_{batching 2、evidence 2}、task_execution_fencing 11、agent_runtime 20、recycle_bin_tasks 13、batch_generate 27、wizard 30。PG 逐文件顺序执行：e5e 4、e5b 2、e5c1 4、e5c2 4、e5c3 5、wizard_successor 4、receipt_done_e5d 8，全部通过。

## 进度：2026-11-13 E5-f 生产路由开放、撤权通知与 xfail 转正

范围：生产路由 `GET /tasks/{id}/stream` 对协作者挂载 E5-b…E5-e 已完成的受控管线；撤权通知在受控订阅上落地并以真实引擎执行验证；4 个 strict xfail 函数（7 个 case）转正；`test_task_sse_scope.py` 旧「协作者 404」用例按新语义改写。前端（E5-g）、Agent（E5a）、知识库（E6）未动。

撤权通知的解读（design §6.3「E5 裁决」与 inventory E5-e/E5-f 对此只有原则，此处记录取舍，取最保守、失败关闭）：
- 通知只是唤醒，放行一律以数据库为准：`notify_collaboration_change` 已对受控订阅以三元组入队 `__collaboration_check__`（E5-a 前已存在，未改），订阅按原绑定 `_visible_task` 复核，失效即以 `permission_denied` 关闭；不依赖执行体被取消，也不等 15 秒心跳。跨实例 / 无通知沿用裁决：下一帧租约或心跳复核失败关闭，不加跨实例唤醒。
- inventory §6-7「是否在帧租约中直接核对 `allow_owner_ai`」：采纳。`_task_frame_lease` 对**活动绑定**（`active_grant_required`）的协作订阅，在任务依赖作者模型（`task_requires_owner_ai`）时锁内直接核对当前 `allow_owner_ai`；已结束回放绑定不受影响（降级/本人仍可读本人已结束记录）。这只收紧，不放宽；作者订阅不经过此路径。
- 未开放帧证明的类型：选择**入口 404**（而非首帧失败关闭）。`stream_evidence.stream_kind_open`（与领取/排队证明同一白名单 `_stream_kind`：单章、分析、批量、向导、大纲、实体）为假时，协作者订阅 Agent / 知识库 / 拆书等任务即使已结束也 404；详情/输出接口不变。

做了什么：
- `api/v1/tasks.py::stream_task`：`_capture_stream_binding` 之后，`binding.member_grant is not None`（协作者，含管理员兼协作者）→ 类型门 → `db.close()` → `task_sse_response(engine.subscribe(…原绑定…, guarded=True), sessionmaker, binding)`（默认 `frame_timeout=5s`）。作者 / 个人任务分支逐字未改（同一 `sse_response(engine.subscribe(...))` 调用与参数），帧序列、内容、时序不变。
- `stream_lease._task_frame_lease`：上述作者模型授权锁内核对。
- `stream_evidence.stream_kind_open`：公开包装 `_stream_kind`。

测试：
- 转正 `test_collaboration_notifications.py::test_http_stream_wakes_after_project_grant_change[revoke|downgrade|model_permission]`：改为真实引擎执行（编辑者本人模型章节生成，FakeLLM Gate 阻塞），经生产路由 + 本机 TCP 订阅，快照为已核验输出；`_check_collaboration_runs` 置空，使执行体不被通知取消（订阅侧唤醒独立承重），心跳 120 秒；变更后 3 秒内收到 `permission_denied`、无 delta/done、任务仍 running；放行后任务未成功、章节正文不变。
- 转正 `test_collaboration_notifications_postgresql.py::test_postgres_stream_wakes_only_after_commit[revoke|downgrade]`：同上真实执行；变更提交被 Gate 挡住时无通知、无新帧，提交后通知唤醒并 `permission_denied`。
- 转正 `test_collaboration_task_postgresql.py::test_postgres_http_stream_rechecks_after_cross_instance_revoke`：订阅实例引擎真实执行，另一实例撤权（本实例无通知）；订阅侧对下一帧的复核被挡住期间提交撤权，放行后 `permission_denied`、该帧内容不发出；原列表/详情隔离断言保留（非规范 grant 的任务改为在引擎启动后插入的 running 行，避免被调度器领取后变为已结束可见）。
- 转正 `test_collaboration_task_privacy.py::test_finished_stream_is_authorized_in_http_and_denied_after_revocation`：生产入口受控回放（新增断言无 `permission_denied`、含 done），撤权后 404。
- 改写 `test_task_sse_scope.py::test_collaborator_stream_fails_closed_without_engine_frame_proof` → `test_collaborator_stream_uses_guarded_pipeline_and_closed_kinds_stay_404`：已结束任务受控回放 `snapshot`+`done`；无可证明来源的运行中 hub 只得 `permission_denied` 且不泄露输出（限时 10 秒，回归时表现为失败而非挂起）；协作者 `agent.turn`/`kb.index` 已结束任务详情 200 但订阅 404；作者自己的 `agent.turn` 仍经原管线回放。伪造证明的负对照沿用同文件 `:54` 等既有用例。
- 新增 `test_task_sse_scope.py::test_active_member_frame_lease_rechecks_owner_ai_directly[True|False]`：中央证明以桩放行，只验证租约；直接改库关闭 `allow_owner_ai`（不递增轮次、无通知）后活动绑定的帧被拒，已结束回放绑定仍可发。
- 同步测试文件头注释（e4f/e4g/e4g2/e4g3/e4g3b/e4g3c、e5b/c1/c2/c3/e5e、chapter_execution）：协作者 SSE 已开放，`/_test` 路由仅用于插入持帧点；未改任何断言。

负对照（备份 `/tmp/nc_e5f_1794553785/{tasks.py,stream_lease.py,engine.py}.bak`，每项改后立即还原并 `shasum -a 256 -c` 全 OK）：
- N1 协作者改走非受控管线 → 作用域用例失败（运行中 hub 快照泄露输出且流不结束，限时触发 TimeoutError）。首次执行时该用例未限时而挂起，已终止本人进程（PID 60064）并加限时后重做。
- N2 去掉类型门 → 作用域用例失败（`agent.turn` 订阅 200 ≠ 404）。
- N3 `notify_collaboration_change` 不向订阅入队唤醒帧 → SQLite 通知 3 例、PG 通知 2 例全部失败。
- N4 去掉租约内作者模型授权核对 → `owner_ai_directly[False]` 失败、`[True]` 通过。
- 未做：跨实例 PG 用例的单层负对照（拒绝同时来自 `checked_frame` 与帧租约两层，各自已在 E5-b/E5-c1 的 PG 负对照中证明承重）。

验证（精简策略，未跑全量）：
- `uv run ruff check .`：All checks passed。
- SQLite 23 个文件逐文件 3 路并行：e5e 84、e5c2 56、e5c3 60、e5c1 39、e5d 33、e5b 24、wizard_successor 14、notifications 14、task_privacy 16、task_sse_scope 18、tasks 32、task_sse_{engine_http 1、forgery 3、frame_guard 10}、task_stream_{batching 2、evidence 2}、task_execution_fencing 11、agent_runtime 20、recycle_bin_tasks 13、batch_generate 27、wizard 30、task_surface_e5a 38、chapter_execution 35；全部通过、无 xfail。其中 e5c2 并行时 `test_batch_frame_produced_before_change_is_denied_at_send[chapter-owner_ai-silent]` 失败 1 次：在订阅前的 `gate0.wait_reached()`（5 秒）超时并伴随「任务 1 刷新输出失败」，与 E5-d/E5-e 记录的负载偶发同源、不经过本片改动路径；单独重跑该组 40 passed、整文件 56 passed。
- PostgreSQL 16（隔离库，逐文件顺序）12 个文件：notifications 2、collaboration_task 7、task_sse_scope 7、e5b 2、e5c1 4、e5c2 4、e5c3 5、e5d 8、e5e 4、paused_task 9、task_client_identity 4、review_read 2；全部通过，无 xfail。

遗留：
- 前端（E5-g）：协作者任务面板/订阅仍按 `canUseAI` 关闭；收到 `permission_denied` 后重订阅一次以消化 E5-b/E5-d/E5-e 记录的已知窗口（投影—发送间可见性变化、收据收尾后在途帧、领取等待 5 秒超时）。
- Agent（E5a）、知识库（E6）、拆书的协作者订阅保持入口 404。
- 排队任务被取消/系统清理时受控订阅得到 `permission_denied` 而非带快照终态（E5-e 遗留，未改）。
- 全量回归留待阶段收口。

## 复核结论：2026-11-13 E5-f（check agent）

对照基线：E5-e 末次备份 `/tmp/nc_e5e_1794547227_*`。本片生产改动只有 `stream_lease._task_frame_lease`（4 行，核对作者模型授权）、`stream_evidence.stream_kind_open`（包装函数）和 `tasks.py::stream_task` 的协作者分支；`engine.py` 与 E5-e 逐字一致。

逐点结论：
1. 协作者判定沿用 E5-e 前已有的 `_capture_stream_binding`。作者本人及个人任务的 `member_grant` 为 None，走原 `sse_response(engine.subscribe(...))`，调用和参数与 inventory 记录的 E5-e 前形态一致，帧序列不变。非成员、待接受和已撤权用户在 capture 内统一返回 404；管理员兼协作者的 `member_grant` 非空，走受控管线，管理员身份不扩权。
2. 类型门放在 capture 之后。只有本人任务且仍为 accepted 成员时才会执行到类型门，所有拒绝都返回同一个 404「任务不存在」，不会泄露他人任务的存在性。至于本人任务“详情 200、订阅 404”，这是有意的不对称：Agent、知识库、拆书的输出仍可通过详情和输出接口按原投影读取。
3. 作者模型授权的直接核对只做收紧：仅对活动绑定生效，且仅在 `task_requires_owner_ai` 为真时检查（任务使用作者来源模型，或使用非 keyword 的共享向量；破损值保守视为依赖）。任务使用本人模型（`source=actor`）时不检查，不会误拒；已结束任务的回放绑定和降级为 viewer 后的本人记录不受影响；作者路径不经过此处。负对照确实被削弱：`test_superseded_batch_frame_rechecks_current_grant_with_latest_source`（e5c2）和 `test_direct_owner_ai_close_after_own_save_is_rechecked_by_successor_guard`（wizard_successor）原本只由后继 guard 承重，现在租约层也会拦截。已修正为：这两个用例把 `stream_lease.owner_ai_allowed` 置为恒真，只屏蔽租约这一层；租约层由 `test_active_member_frame_lease_rechecks_owner_ai_directly` 单独证明。
4. 唤醒（`__collaboration_check__`）只触发 `read_visible()` 查库，可见就 `continue`，不会作为放行依据；队列满时丢弃唤醒，下一帧仍重新验证。本片没有新增等待（engine 未改），跨实例场景仍依靠下一帧租约或心跳。
5. 重写的通知用例使用真实引擎、Gate 和本机 TCP 连接，断言了 `permission_denied`、无 delta/done、任务仍为 running、正文未变、`output_text` 不含撤权后内容。所有等待都有上限（`asyncio.wait_for` / `asyncio.timeout` / httpx timeout=10）。`sleep` 只用于轮询，没有用 sleep 来断言时序。
6. e5c2 那次偶发失败超时在订阅之前的 `gate0.wait_reached()`，并伴随 SQLite 刷新输出失败，不经过本片改动的路径，与本片无关；本次 3 路并行下整文件 56 passed。

修改：
- `tests/test_collaboration_stream_batch_e5c2.py`、`tests/test_collaboration_stream_wizard_successor.py`：新增 `_disable_lease_owner_ai_layer`，恢复单层负对照，并更新 docstring。
- `tests/test_task_sse_scope.py::test_active_member_frame_lease_rechecks_owner_ai_directly`：补充反向断言：活动任务使用本人模型时，关闭作者模型授权不误拒。
- `tests/test_collaboration_task_privacy.py`：新增 `test_stream_entry_for_viewer_and_admin_member_keeps_finished_replay_only`：降级为 viewer 后可受控回放本人已结束任务、活动任务返回 404；兼任管理员后结果相同；撤权后与他人任务返回相同的 404 响应体。

负对照（备份 `/tmp/nc_e5fchk_<ts>_stream_evidence.py.bak`，还原后 `shasum -a 256 -c` 显示 OK）：去掉 `stream_evidence` 中后继来源的 `require_task_read` 和 `require_terminal_read` 三处后，上述两个用例都失败；还原后通过。

验证：ruff All checks passed；SQLite 25 个文件 3 路并行全部通过（e5e 84、e5c3 60、e5c2 56、e5c1 39、task_surface_e5a 38、e5d 33、tasks 32、wizard 30、batch_generate 27、paused_cleanup_e5a 25、e5b 24、revocation_e4 22、agent_runtime 20、task_sse_scope 18、task_privacy 17、notifications 14、wizard_successor 14、recycle_bin_tasks 13、task_review 12、execution_fencing 11、frame_guard 10、forgery 3、stream_batching 2、stream_evidence 2、engine_http 1）。PostgreSQL 逐文件顺序全部通过：notifications 2、collaboration_task 7、task_sse_scope 7、e5c2 4、wizard_successor 4、e5b 2、e5e 4。未跑全量回归。

遗留：与 E5-f 进度记录一致（E5-g 前端重订阅、Agent/KB/拆书入口 404、排队取消终态），本次复核没有新增遗留。

## 进度：2026-11-13 E5-g 前端（AI 入口、任务面板、permission_denied 重订阅、向导未发布）

范围：仅前端；后端未改（无 schema 变化，未跑后端测试）。

做了什么：
- 能力分两档（`utils/projectAccess.ts`）：`canUseAI` 改为服务端 `can_use_ai`（= 作者或 accepted 编辑者；管理员身份不给）；新增 `canUseOwnerAI`（仅作者）与 `canUseOwnerAINow`、`ProjectCapability` 增项；`canUseAIEntitled` 保留为同义别名。逐一核对全部 `canUseAI` 用法：
  - 对编辑者开放（后端已开放）：章节生成/润色/按建议重写、场景 AI、局部改写与富文档区块改写（E4 已开放同步改写受控发送）、章节分析（编辑器、阅读器、章节列表状态、概览「分析全部未分析」；自动分析开关仍只作者可切）、批量、向导（导航项、概览按钮、向导页、各面板「AI 重新生成」）、大纲续写/展开、实体五类生成、本人生成记录（后端按 actor 过滤）；大纲导入/导出按钮原本挂在 canUseAI 上，随之对编辑者显示（后端分别按 content.edit / export 校验），viewer 不变。
  - 仍仅作者（`canUseOwnerAI`）：项目智能体按钮/停靠面板及会话/撤销回调、知识库与记忆的索引状态/重建/检索调试、知识库上传的作者模式判定、灵感模式补全。
- 任务面板：`showsTaskPanel(projectId, access)` = 作品外始终显示；作品内 canUseAI 或协作者（viewer 可读本人已结束记录）；纯管理员代管不显示。列表仍为 `/tasks`（后端只返回本人任务）。`TaskOutputModal` 经 `taskOutputMode`：已结束输出按 canRead 读取；活动任务需 canUseAI，协作者只对 `COLLABORATOR_STREAM_KINDS`（单章/分析/批量/向导/大纲/实体，与后端 `_stream_kind` 白名单一致）发起订阅，Agent/知识库/拆书只显示状态与说明；不属于当前作品的任务（scope 为空）照旧订阅，由后端判定。面板内重试/继续等按钮未按角色隐藏，由后端拒绝。
- `subscribeTask`：首次 `permission_denied` 停止交付后续帧 → `taskApi.get` 核对详情 → 可读则退避 `min(baseDelayMs, maxDelayMs)` 后只重订阅一次（`onReconnect(1, delay)`，不占网络重连次数）；详情 403/404/身份变化/读取失败或二次拒绝按原逻辑（作品作用域清输出 + 复查作品 + 抛 403；个人任务 failed + detail）。作者流不发该帧，普通 error 不查详情。
- 向导「结果未发布」裁决：`WizardGenerator` 新增 `onUnpublished`（`message.info(detail)` 后调用，**不调用 onDone**）；`WizardModal.onRefresh` 由四个面板传入各自 reload（角色另刷新徽标、大纲另清选择），弹窗保持打开显示说明；向导页只 `reload()` 计数，**不写 wizard_progress 完成标记**，由用户核对后手动「下一步」。理由：未发布时无法确认生成条目，标记完成会把可能未成功替换的步骤误记为完成；刷新设定能让已提交的内容立即可见。

测试（vitest）：
- `api/tasks.test.ts` 新增 7 例：详情可读只重订阅一次并回放终态（退避取上限）、二次拒绝走原错误且不循环、作品作用域二次拒绝清输出+刷新+403、详情 404/403 不重订阅、作者普通 error 不查详情、退避期间中止不再订阅。
- `utils/projectAccess.test.ts`：能力矩阵改为 canUseAI/canUseOwnerAI 双列（owner/viewer/editor/admin/admin+viewer/admin+editor/无授权）；新增任务面板显示与 `taskOutputMode` 用例。
- 新增 `components/WizardGenerator.unpublished.test.ts`、`pages/project/WizardPage.unpublished.test.ts`：未发布触发刷新、不调用 onDone、不写进度；正常完成对照。
- 按新语义改写（未放宽安全断言）：`stores/project.test.ts`、`hooks/useProjectAccess.test.ts`（编辑者 canUseAI=true、canUseOwnerAI=false）；`pages/project/collaborationWorkspace.test.ts`（viewer 显示任务面板但无向导/分析/智能体；新增 editor 例：有任务面板/向导/分析，无智能体/灵感/备份/删除；纯管理员仍无任务面板）；`stores/projectFailure.test.ts`、`hooks/useTaskStream.test.ts` 的 permission_denied 用例改为详情 404 场景并断言详情只查一次。
- 负对照（备份 `/tmp/e5g_*.bak`，还原后 `shasum` 一致）：去掉 `onUnpublished?.()` → 未发布用例失败；关闭首次重订阅分支 → 5 例失败。

验证：`npm run lint` 通过；`tsc -b` 无错误；`npm run test` 208 文件 1457 例全部通过；`npm run build` 成功。

遗留：
- 真实浏览器（作者 / viewer / editor / 管理员交集）未验证。
- 不属于当前已加载作品的协作任务（全局面板里其他共享作品的 Agent/KB 任务）无法在前端判定角色，查看输出时会照常订阅并收到后端 404 错误提示。
- 任务面板对 viewer 仍显示重试/继续等按钮，点击由后端拒绝（403/409）并提示；未做按角色隐藏。
- Agent（E5a）、知识库（E6）、拆书对协作者仍关闭；排队任务被取消时受控订阅得到 permission_denied（E5-e 遗留）现会先查详情再重订阅一次，预期得到已结束回放（终态 cancelled），未写用例验证。

## 复核结论：2026-11-13 E5-g（check agent）

逐项核对（后端代码为准）：
- 改为 canUseAI 的入口后端均对 accepted 编辑者开放：章节/场景 generate/polish/revise（`create_chapter_task`）、局部改写与场景改写（`rewrite_service.prepare_rewrite_job` 非作者走 `_ai_project_chapter` → `ai.use`）、富文档区块改写（`rich_document_section_ai.prepare_section_rewrite` 同上）、`analyze-pending`（`require_project(ai.use)`）、分析状态（`content.read`）、本人生成记录（`require_personal_project` 允许 accepted 成员，按 `actor_user_id` 过滤）、封面（`ai.use`）。
- **问题并已修**：大纲导入/导出挂在 canUseAI 上，而后端分别是 `content.edit`（import，preview 为 content.read）与 `export`。改为导入按钮与弹窗挂 `canEdit`、导出挂 `canExport` 且请求前后复查 `canExportNow()`；空态文案随之区分。效果：查看者可导出、不可导入；只有人工编辑权限的管理员可导入；作者/编辑者不变。
- canUseOwnerAI 覆盖完整：智能体按钮/停靠面板/会话与撤销回调、知识库与记忆索引状态/重建/检索调试、灵感补全；`?agent=open` 链接只在 `canUseOwnerAI` 时挂载 AgentDock；拆书是作品外入口（新建作品），不受影响；协作者知识库上传沿用既有 keyword/unavailable 模式。
- 重订阅：`deniedRetried` 全程只置一次；退避 `reconnectDelay(1)` 受 `maxDelayMs` 封顶；可中止（详情请求与 sleep 都带 signal）；详情 403/404/身份变化/读取失败直接走原拒绝；作者流不发该帧故不查详情；不占网络失败计数且只一次，不会叠加成循环；重订阅后的 `snapshot` 覆盖本地文本（`setText(snapshotText)`），已收增量不会重复。
- **问题并已修**：任务面板对当前作品内无 AI 授权（查看者/纯管理员）仍显示继续/结束/取消/重试，而后端 `_lock_user_task_project` 对开放任务及 `active_grant_required` 一律要求 editor 且 grant 一致，必然拒绝。新增 `taskControlsAllowed(taskProjectId, project, user)`：属于当前已加载作品且快照就绪时按 canUseAI，否则交后端；删除已结束记录保留（后端不要求 AI 授权）。作者路径逻辑等价未变。
- 全局面板里不属于当前作品的 Agent/知识库/拆书任务被后端 404 时，错误文案为「任务不存在」类，不泄露；**已补**一行说明（仅非当前作品、实时订阅失败且类型不在协作者白名单时显示）：「若该作品由他人共享，此类任务暂不支持实时查看，任务结束后可在「最近」中查看输出」。
- 向导 unpublished：四个面板（角色/世界观/职业/大纲）与向导页均传入刷新回调，不调用 onDone、不写 wizard_progress，核对无误。

新增测试：`pages/project/OutlinesPanel.access.test.ts`（查看者/编辑者/作者/仅人工编辑管理员，按按钮图标断言）、`utils/projectAccess.test.ts` 新增 `taskControlsAllowed` 矩阵。负对照（备份 `/tmp/e5g_check_*.bak`，还原后 `shasum -a 256 -c` OK）：把导入/导出按钮改回 canUseAI → 2 例失败；`taskControlsAllowed` 恒 true → 1 例失败。

验证：`npm run lint` 通过；`tsc -b` 无错误；`vitest run` 209 文件 1461 例全部通过。后端未改，未跑后端测试。

遗留：
- 真实浏览器（作者/查看者/编辑者/管理员交集）仍未验证。
- 编辑者在授权轮次变化（撤权后重新邀请）后，旧轮次任务的重试/继续仍会显示并被后端拒绝（前端无法判定任务 grant 与当前轮次是否一致）。
- 向导未发布时若之前同一生成器有上次成功结果，结果区仍显示上次条目（`setResult` 未清空），仅外观问题，未改。

## 浏览器验证：2026-11-13 E5（主会话）

环境：/tmp/e5g_browser 下隔离 SQLite 库、本地假 OpenAI 兼容服务（逐块慢速流式）、后端 :9137、Vite :5187；账号 e5g_owner / e5g_editor（允许作者模型）/ e5g_viewer。启动配置写在 `.claude/launch.json`（指向 /tmp 脚本）。

通过：
- 编辑者无本人模型、作者已授权：生成任务回退作者模型成功（后端路径）；任务面板「查看输出」实时推送正文。
- 推送中作者将编辑者降为查看者：页面即时切换为查看者（提示条、AI 入口隐藏、输出弹窗关闭），任务以「协作授权已失效，任务已停止」失败，章节正文未写入。
- 降为查看者后：任务面板最近任务无重试/继续/取消，仅保留查看输出与删除；已完成任务输出可回看；大纲页无导入、无 AI 续写，新增/建章按钮禁用。
- 恢复编辑者并配置本人模型：章节页「生成当前场景正文」实时出字；刷新页面后经任务面板订阅进行中任务，先收快照再续收增量（E5-c1/E5-e 路径）；任务面板「取消」可取消本人任务。
- 查看者：无 AI 助手/分析入口，编辑按钮禁用；任务列表为空，他人任务详情与订阅 404，发起生成 403。
- 作者回归：智能体、分析入口在；重新生成确认后实时出字，停止正常。

发现（未修）：
- **前端文本 AI 入口不支持作者模型回退**：`AIConfigSelect` 在本人无该类型配置时直接显示「尚未配置文本模型」，`ChapterEditorPage` 等按 `useHasAIConfig()===false` 禁用生成/润色/分析按钮。PRD R4 规定本人无配置且作者已授权时回退作者默认配置，后端已实现，但编辑者在界面上无法发起；封面生成（`coverChoice`）已有回退处理，可作参照。涉及约 23 处 `AIConfigSelect` 调用与 `hasConfig` 禁用条件。
- 章节页刷新后不关联页面外/刷新前发起的任务（显示「暂无法安全关联先前的任务」）：既有设计，作者同样如此，非协作者特有。
- 未在浏览器触发：`permission_denied` 后重订阅一次（时序难以手工构造，依赖单测）；向导「结果未发布」；管理员兼协作者路径。

## 进度：2026-11-13 E5-g2 作者模型回退（前端）

处理 E5 浏览器验证发现的「前端文本 AI 入口不支持作者模型回退」。后端未改。

实现：
- `utils/projectAccess.ts` 新增纯函数 `aiModelSource(access | null, loaded, configCount) → 'loading' | 'own' | 'owner' | 'none'`：作品快照未就绪/本人配置未加载 → loading；无 canUseAI → none；有本人该类型配置 → own；无配置且 accepted 编辑者 + `allow_owner_ai` → owner；作者永不 owner；access=null（作品外）只看本人配置。
- 新增 `hooks/useAIModelSource.ts`（读路由 `:pid` + auth/project/aiConfigs store，不引入 ref/effect）；作品外与 `embedding` 传 null。
- `useHasAIConfig` 改为由 `useAIModelSource('text')` 派生（loading→null、none→false、own/owner→true），因此全部 `hasConfig === false` 调用点自动按判定生效，签名不变、既有测试 mock 不受影响。
- `AIConfigSelect` 在 owner 模式渲染 info Alert「未配置本人X模型，将使用作者授权的默认X模型」+「本次可能使用作者密钥并产生作者费用，实际来源以生成结果为准」，value 保持 null；其余分支（含无配置警告）不变。

调用点（随 useHasAIConfig/AIConfigSelect 生效，均在 `projects/:pid` 路由内、后端已对编辑者开放）：ChapterEditorPage（生成当前场景/章节、重写、局部改写选择、分析抽屉 analyzeDisabled、重写阻断原因）、WizardGenerator（生成/从中断处继续）、OutlineTaskModal、EntityTaskModal、BatchGenerateModal 确定按钮及各自 AIConfigSelect。
刻意未改：inspiration/OptionStep（`/inspiration/:sid`，无 pid，账号级 + 仅作者）、StyleTrialDrawer/StyleExtractModal/bookImport OptionsStep（作品外）、AgentConversationView（canUseOwnerAI 门控，作者永不 owner）、CoverGenerateModal（已有 coverChoice，自行渲染 owner 提示）、embedding（hook 内排除）。本人已有配置但无效 → 仍由后端报错，不回退。

测试：`utils/projectAccess.test.ts` 新增 aiModelSource 矩阵（作者无配置 none、编辑者授权 owner/未授权 none/有配置 own、查看者/管理员/无授权 none、加载中与快照未就绪 loading、作品外）；新增 `components/AIConfigSelect.ownerFallback.test.ts`（SSR：owner 模式无警告且有作者费用说明；未授权编辑者/作者显示尚未配置；有本人配置显示选择框；作品外与向量模型不回退；WizardGenerator 生成按钮在 owner 模式可用、未授权编辑者与作者无配置禁用）。
负对照（备份 `/tmp/e5g2_neg_72850`，还原后 `shasum -a 256 -c` 全 OK）：判定不返回 owner → 3 例失败；AIConfigSelect 去掉 owner 分支 → 1 例失败；useHasAIConfig 恢复只看本人配置 → 向导按钮用例失败。
验证：`npm run lint` 通过；`tsc -b` 无错误；`npm run test` 210 文件 1474 例全部通过；`npm run build` 成功。

遗留：真实浏览器未复验（编辑者无本人模型 + 作者授权下点击生成/润色/分析）；ChapterEditorPage 未单独补页面级用例（以向导按钮 + 共享 hook 覆盖）。

## 浏览器复验：2026-11-13 E5-g2 作者模型回退（主会话）

同一隔离环境，删除编辑者本人模型、作者授权开启：
- 章节页显示「未配置本人文本模型，将使用作者授权的默认文本模型…可能产生作者费用」，「重新生成当前场景」「分析」可用；点击生成后实时出字（任务 6，后停止）；分析任务正常受理并用作者模型执行（假模型非 JSON 导致解析失败，属测试桩限制）。
- 向导页同样提示，「AI 生成世界观」可用。
- 作者仅关闭 allow_owner_ai 后：刷新页面后变为「尚未配置文本模型」且按钮禁用（正确）；但未刷新时 2.5 秒内页面仍停留在回退可用状态（角色降级则即时切换）——已转交复核排查前端授权刷新触发条件。

## 复核结论：2026-11-13 E5-g2（check agent）

逐点：
1. 波及面：`useHasAIConfig` 仅 ChapterEditorPage、WizardGenerator、OutlineTaskModal、EntityTaskModal、BatchGenerateModal、inspiration/OptionStep 使用；`AIConfigSelect` 另在 Agent、风格试写/提取、拆书、封面中挂载。owner 判定只在路由有 `:pid` 时生效：风格/拆书/灵感路由无 pid，行为不变；Agent 仅 `canUseOwnerAI`（作者）挂载，作者永不 owner；封面在 owner 模式不渲染 AIConfigSelect。章节生成/润色/重写/改写（rewrite_service 协作分支 `resolve_project_model`）、分析、批量、大纲、实体、向导后端均走 `resolve_project_model`/`preflight_project_model`，个人无该类型任一配置且 `allow_owner_ai` 时回退，与前端 `configCount>0 → own` 一致。知识库/记忆无 hasConfig 入口。
2. 判定矩阵正确（作者/查看者/纯管理员/未授权/有配置/快照与 pid 不一致→loading）；撤权/降级后 store 替换快照即切换。
3. 选择本人配置路径不变。补：owner 模式下若父组件残留旧 `value`（本人配置被删除后），`AIConfigSelect` 用 effect 置回 null，避免带配置 ID 请求。
4. 加载中不禁用与改动前语义一致（原 hook 加载中亦 null），快照未就绪时 ProjectLayout 不渲染子页面，后端仍拒绝，保留。
5. 补 `pages/project/ChapterEditorPage.ownerFallback.test.ts`（不 mock useHasAIConfig：owner 模式生成按钮/分析/重写可用；未授权编辑者与作者无配置禁用并给出「尚未配置 AI 模型」；有本人配置可用）。
6. 主会话补充点（仅关闭 allow_owner_ai 后编辑者页面不切换）：前端 `sameAccess` 已比较 `allow_owner_ai`，问题在刷新触发——作品权限快照只在 load、认证身份变化、项目请求 403/404 时复核；关闭作者模型授权后，回退请求被后端以 400「尚未配置…」拒绝，不触发刷新（改角色时页面上其他请求 403 才触发）。后端成员变更通知只是本进程唤醒，不推送前端，未改后端。修复：`utils/visiblePolling.ts` 新增 `startForegroundPolling` / `startCollaboratorAccessPolling`（协作者在前台每 15 秒、切回可见/聚焦立即 `refresh()`，作者与纯管理员不轮询），ProjectLayout 接入。测试 `utils/collaboratorAccessPolling.test.ts`、`stores/project.test.ts` 新增「只关闭作者模型授权：刷新即更新快照并递增作用域、来源变 none」。

负对照（备份 `/tmp/e5g2chk_w3aeUu`，还原后 `shasum -a 256 -c` 全 OK）：轮询恒不启动、去掉残留值清除、useHasAIConfig 恢复只看本人配置、sameAccess 去掉 allow_owner_ai 比较 → 共 7 例失败。
验证：`npm run lint` 通过；`tsc -b` 无错误；`npm run test` 212 文件 1481 例全部通过；`npm run build` 成功。
遗留：真实浏览器复验（轮询最长 15 秒延迟，切回窗口即时）；后端缺模型 400 无 code，前端无法据此精确触发复核；无服务端推送。
- 复核加入协作者权限前台轮询（15 秒/聚焦即时）后复验：关闭 allow_owner_ai 后向导页未刷新即切换为「尚未配置」+ 禁用。

## 进度：2026-11-13 E6-a 受理与任务身份（不开放协作者入口）

实现（无新迁移）：
- `KbIndexHandler.shared_embedding_admission = True`（不依赖文本模型的协作受理）；仍不声明 `collaboration_execution_ready`。`validate_and_claim` 透传 `ctx.cipher` / `allow_private`。
- `kb_index_service.validate_index_task`：作者沿用原受理（`get_owned_project(lock=True)`，输入无协作保留键）；非作者 → `_validate_collaborator_index_task`：`require_project(ai.use, for_update, visibility_write)` → `resolve_shared_embedding(require_vector=True, for_update=True)`，服务端写 `_collaboration_grant`（`bind_task_grant`）+ `_shared_embedding`（作者默认向量；协作者本人向量配置不参与）。未授权 403 `owner_embedding_not_allowed`；作者无向量 400；memory 模式 409 `collaboration_embedding_unavailable`（Q4 本次条目绑定属 E6-c）。
- `task_service.create_task`：handler 声明该标志、无 `accepted_model`、非重试且 spec.input 带轮次时，按该轮次 `require_project(ai.use, grant_identity=…)` 再锁作品（原 `get_owned_project` 分支对其余情况不变）；Task.user_id = 真实 actor，文档占用照常绑定。
- `build_index_job`：删除 `project.user_id != task.user_id` 假设。带 grant → `_build_collaborator_index_job`（执行身份/轮次/令牌/`_shared_embedding` 逐项一致；反序列化冻结选择须 vector 且配置属作者；`require_authenticated(targets=作者)` + `require_write_execution` + `revalidate_shared_embedding(for_update=True)`；文档 claim 属同 actor）→ `IndexJob(embedder=None, selection, actor_id, grant_identity)`，**不调用 resolve_embedder**。无 grant → 只能是作者本人执行，`resolve_embedder(project.user_id)` 运行时解析（原行为）。`IndexJob` 新增 `actor_id` / `grant_identity`。
- `run_index_job`：带 grant 的作业在受控发送（E6-b）接入前直接失败关闭（不退回未受控 embedder，也不伪装关键词成功）。
- 同类假设 grep：KB 链路只剩此处一处 `project.user_id == task.user_id`；`require_bound_index_job` / `_verified_analysis_index_source` / `store_owner_legacy_embedding_snapshots` / `enqueue_memory` / `may_enqueue_vectors` 的「仅作者」限制属 E6-c，`stream_evidence.py:746` 为作者订阅路径（Q5 保持 404），`kb_service.index_status` / `context_builder.retrieval_preview` 属 E6-d，本片未改。
- 向量空间键保持作者旧键（裁决 1）；本片不写向量。

测试：新增 `tests/test_kb_index_admission_e6a.py`（SQLite 19 例）：作者上传/重建身份与行为不变（关键词降级→配置后重建 ready、只调作者模型、输入无保留键）；内部协作受理（document/rebuild）记录真实 actor、原邀请轮次、令牌轮次、作者配置选择（≠ 协作者本人配置）、文档占用；任务详情与 TaskOut 不含 `_shared_embedding`/`config_id`/模型名；未授权/memory/伪造保留键/非成员/viewer/作者无向量均无 Task 无占用；build 使用冻结作者选择、embedder 为空、运行失败关闭零请求；关闭模型授权（HTTP 与不递增 revision 的直接改库）/撤权/重邀轮次/令牌/作者换默认向量/降级 viewer 时 build 失败零请求；无 grant 的非作者任务被拒；引擎直接领取：有效时保持排队（未就绪），失效时受限收尾为 failed 并释放文档占用。新增 `tests/test_kb_index_admission_e6a_postgresql.py`（3 例）：撤权先提交 → 受理阻塞后 403/404、无 Task 无占用；受理先提交（`pg_blocking_pids` 证明撤权被阻塞）→ 撤权后 build 失败关闭；受理后撤权 → 引擎领取 failed 并释放文档占用；全部零向量请求。

负对照（备份 `/tmp/e6a_neg_PhjZ91`，还原后 `shasum -a 256 -c` 全 OK）：删除协作 build 分支（恢复作者假设）→ 2 例失败；协作 build 改用 `resolve_embedder(task.user_id)` → 2 例失败；受理不写 `_shared_embedding` → 4 例失败；中央受理退回 `get_owned_project` → 13 例失败；去掉 `require_write_execution`+`revalidate_shared_embedding` → 6 例失败（令牌变体由显式 `require_authenticated` 兜住）；去掉 `run_index_job` 的协作者失败关闭 → 0 例失败（冗余层：selection 非空时 `_run_bound_memory` 的 `require_bound_index_job` 同样拒绝 grant，零发送）。

精简回归（SQLite 逐文件 ≤3 路）：test_kb 33、test_retrieval 15、test_memories 15、test_kb_index_claims 7、test_kb_legacy_document_cleanup 18、test_kb_document_guard_integration_e6 5、test_kb_document_claim_send_e6 8、test_bound_memory_index_e6 17、test_memory_index_actor_e6 1、test_collaboration_shared_index_e6 8、test_collaboration_memory_write_e6 20、test_analysis_index_receipt_enqueue_e6 13、test_shared_http_knowledge_e6 3 xfailed（预期）、test_collaboration_embedding_snapshot_send_e6 3、test_collaboration_task_revocation_e4 22、test_task_admission 7、test_task_sse_scope 18、哨兵 test_tasks 32、test_task_execution_fencing 11、test_recycle_bin_tasks 13、e5b 24、e5e 84、test_batch_generate 27 —— 全部通过。PG（逐文件顺序）：e6a 3、kb_index_identity 1、kb_legacy_document_cleanup 4、kb_document_guard_e6 1、collaboration_shared_index_e6 4、bound_memory_index_e6 2、collaboration_task_revocation_e4 2、task_execution_fencing 1 —— 全部通过。`ruff check .` 通过。

遗留 / 交给后续切片：
- E6-b：协作者（及作者，裁决 6）文档/重建 `_embed_stale` 迁到快照 + `for_task` + 每批 `require_task_write`；接入后移除 `run_index_job` 的协作者失败关闭并声明 `collaboration_execution_ready`（届时需复核引擎领取退避与 E6-a 引擎用例中“有效时保持排队”的断言翻转）。
- E6-c：kb_service 上传/重新索引/重建与记忆入队的协作入口开放（当前仍 409/403）；协作者 memory 模式绑定本次条目。
- `KbIndexHandler.validate_retry` 对带 `_shared_embedding` 的协作任务沿用既有拒绝（不续签旧轮次）；`tasks.py` 重试入口对未就绪 handler 也拒绝非作者。
- `run_index_job` 的协作者失败关闭层目前没有独立区分它的用例（见负对照）。

## 复核结论：2026-11-13 E6-a（check agent）

- 作者路径：受理只多一次无锁 `Project.user_id` 读取用于分流，作者分支仍为 `get_owned_project(lock=True)`，输入不带协作保留键；build 由 `resolve_embedder(task.user_id)` 改为 `resolve_embedder(project.user_id)`，前置检查要求两者相等，因此语义等价。关键词降级、rebuild 缺配置报 400、结果均不变。
- 中央分支的进入条件：必须同时满足 handler 声明 `shared_embedding_admission`（目前只有 kb.index）、`accepted_model is None`、`retry_of is None`，并且 spec.input 中带 GRANT_KEY。spec.input 由 handler 在服务端从零构造；客户端参数中的 `_collaboration_grant` / `_shared_embedding` 无论放在顶层还是嵌套位置，都会先被 `_contains_reserved_task_key` 以 409 `task_actor_unavailable` 拒绝。受理时先无锁分流，分流后两个分支都会在锁内复核作者身份。协作者的 HTTP 入口（`_require_vector_task_owner`，以及在授权时的上传）在调用 create_task 之前就已关闭；记忆入队直接插入 Task，只允许作者。重试：`validate_retry` 遇到 `_shared_embedding` 一律拒绝。
- 泄露：TaskOut 和详情中没有配置 ID 或模型名；`resolve_shared_embedding` 的错误文案不带配置名；运行时失败的统一文案为「建索引失败：服务器内部错误」；协作者订阅仍为 404。
- build 重验：执行身份、轮次、令牌、作者更换默认向量、撤权、重邀、降级、直接改库关闭授权，这 7 种变体都已覆盖。PG 用例通过 `_assert_project_row_locked` 和 `pg_blocking_pids`，分别证明“撤权先提交”与“受理先提交”两种锁序；没有用 sleep 来断言时序。
- 补测：`test_run_index_job_rejects_grant_job_even_with_unguarded_embedder` 用作者 rebuild 作业挂上 grant 来构造，此时 selection 为空、embedder 未受控，可以绕开 `require_bound_index_job`。负对照（备份 `/tmp/e6a_chk_y476O1`）去掉 `run_index_job` 的协作者失败关闭后，该用例失败；还原后 `shasum -a 256 -c` 结果 OK。另外新增 `test_client_reserved_keys_rejected_for_author_and_collaborator`（2×2：作者/协作者 × 两个保留键，顶层与嵌套都测），断言零任务、零占用、零请求。
- 验证：e6a 共 24 例（原 19 例 + 新 5 例），SQLite 精简回归包括 test_kb 33、retrieval 15、memories 15、kb_index_claims 7、kb_legacy_document_cleanup 18、bound_memory_index_e6 17、collaboration_shared_index_e6 8、tasks 32、task_execution_fencing 11、recycle_bin_tasks 13、e5b 24、e5e 84、batch_generate 27，全部通过。PG 方面 e6a 3 例、kb_legacy_document_cleanup 4 例通过。`ruff check .` 通过。
- 遗留：没有新增遗留，原 E6-b/E6-c 的交接保持不变。`test_collaborator_build_fails_closed_when_grant_changes` 只断言抛出 AppError，没有细到错误码（负对照已证明各层有效）；如需更严，可以在 E6-b 翻转时一起收紧。

## 进度：2026-11-13 E6-b 受控发送与每批重验（= E4-h KB 重鉴权）

实现（无新迁移）：
- `kb_index_service._embed_stale`（文档 / 重建 / 作者旧记忆，作者与协作者共用，裁决 6）改为每批：短事务快照（stale 行 → `snapshot_embedding_inputs(allow_orphans=True)` + 当批可见性戳，立即回滚）→ 孤儿行在 `_require_index_write` 下条件删除 → 按截断文本去重后 `collaboration_ai_guard.for_task(execution, selection, stamp, document_claim, embedding_inputs)` → `prepare_provider` → 真实发送租约内重验 → 新写事务 `guard.require_task_write` + 新增 `retrieval_service.store_index_legacy_embedding_snapshots`（快照批量 CAS，写作者旧键 `provider:model:dim`，裁决 1；非重建只补空向量）。写入未成功时不写任何向量，用 `_require_index_write` 判定：授权/执行/占用/配置失效 → `EmbeddingPolicyError` 失败关闭；仍有效 → 视为来源或可见性代次变化，重新快照（每作业 ≤3 次，超出文档记 `ready_keyword_only` + 提示）。普通向量服务故障（`_policy_cause` 为空）不重发，降级为关键词（裁决 11）。
- `_require_index_write`：原 token + 作者 User 屏障 → `require_write_execution` → 文档占用（`require_writing_document_claim`）→ `require_project(ai.use, grant)` → `revalidate_shared_embedding(for_update)`。文档首尾状态写、`_refresh_doc_statuses`、孤儿删除都在其下进行。`_require_job_identity` 校验作业身份字段与领取冻结的执行身份一致（取代 E6-a 的「带 grant 一律失败关闭」层）。
- 作者 build：要求原执行身份与 `_task_actor_token_version`；`resolve_embedder` 只作可用性探测（保留原降级提示），实际构建期绑定作者当前默认向量 `ResolvedSharedEmbedding(source="actor")`（不写入 Task），`IndexJob.embedder=None`。`IndexJob` 新增 `project_owner_id`、`bound_memory`、`incomplete`；分析派生绑定记忆以 `bound_memory=True` 走原收据路径。
- 守卫：`_matches_task_binding(..., legacy_owner_index=kind=="kb.index")` 与 `_require_index_snapshot_identity`（`allow_legacy_owner=True`）仅对 kb.index 接受「Task 不含选择 / grant、actor 即作者、作者本人默认配置」的旧作者形状（`retrieval_service.legacy_owner_index_selection`）；作者旧记忆不得带分析绑定键，`_lock_embedding_snapshot_sources` 仅对带选择的绑定记忆核收据。
- `KbIndexHandler.collaboration_execution_ready = True`；`run_index_job` 去掉协作者失败关闭层；`verify_event` 对非绑定模式在引擎 `_event_task` 重验后补文档占用与配置重验（`done` 帧只核原执行，内容已在受控收尾事务提交）；`verify_finalization` 走 `require_write_execution`。策略类错误的任务错误文案为「建索引已停止：授权、模型配置或索引状态已变化，请重新索引」。
- 顺带修复：`_run_bound_memory` 引用不存在的 `retrieval_service._policy_failure`（普通故障会 AttributeError 被当成内部错误），改为 `_policy_cause`。
- HTTP 入口仍对协作者关闭（E6-c）；协作者订阅 kb.index 仍 404（裁决 5）。

测试：
- 新增 `tests/test_kb_index_guarded_e6b.py`（SQLite 31 例）：协作者经引擎执行（任务归协作者、只调作者模型、40 行全为 `openai_compat:owner-emb:64`、文档 ready）；协作者矩阵 {撤权、降 viewer、关作者模型授权、令牌、作者换默认向量}×{HTTP 通知、直接改库无通知}×{每批之间（第二批过 prepare 未进发送门禁）、发送后写入前} 16 例：首批 32 行保留、后续批零发送（between）或已发送但不写入（after_send）、任务 failed、文档 failed 且占用清空；作者 {换默认向量、改令牌}×2 时序；并发修改资料（作者/协作者）不写旧正文向量、重新快照后按新内容建向量；普通故障（首批/第二批 × 作者/协作者）降级 `ready_keyword_only`、已写批保留；文档模式不覆盖其他空间向量；协作者重建按作者旧键替换并刷新状态；重建批间撤权/关授权不刷新文档状态。
- 新增 `tests/test_kb_index_guarded_e6b_postgresql.py`（3 例）：写批事务持锁时撤权被阻塞（`pg_blocking_pids`）→ 首批保留、撤权后下一批零发送；撤权事务持锁时写批重验被阻塞 → 零写入；正例。撤权由另一应用实例提交（执行体所在实例无本进程通知）。
- 翻转 / 调整：`test_kb_index_admission_e6a.py` 有效协作者任务改为正常执行（job 运行 + 引擎领取 succeeded）、作者 build 断言改为构建期绑定、原「带 grant 的未受控 embedder」用例改为作业身份篡改（3 种）失败关闭；`test_collaborator_build_fails_closed_when_grant_changes` 收紧错误码（授权类 409 `task_execution_changed`、令牌 401、作者换默认向量 400「向量模型已变化」）。`test_setting_ai_context.py::test_memory_embedding_does_not_send_canonical_names_from_old_internal_index` 改为经作者旧记忆任务 build+run（原直接调用 `_embed_stale` 的签名已变）。`tests/helpers.py` FakeLLM 新增 `embed_before_gates` / `embed_after_gates`（默认空，不影响其他用例）。
- `test_kb_index_admission_e6a_postgresql.py::test_admission_commits_before_revoke_then_build_and_claim_fail_closed` 偶发失败（与本片无关：将 `collaboration_execution_ready` 临时置回 False 仍 4/4 失败）：受理提交后、返回前的任务投影复核与撤权提交竞争，撤权先提交时 `task_out` 合法地 404。改为接受该合法赢家并按库内 Task 继续断言，复跑 4×3 通过。

负对照（备份 `/tmp/e6b_neg_PdGr3X`，每次还原后 `shasum -a 256 -c` OK）：
- 去掉每批写前重验（仅去 `guard.require_task_write`，保留 CAS 内的配置重验）→ 2 例失败（令牌 after_send）；去掉全部写时重验（直接按快照条件 UPDATE）→ 7 例失败（所有无通知 after_send），HTTP 通知变体由引擎通知取消兜住；PG 同一负对照 → 2 例失败。
- 去掉写时快照条件（保留 `_require_index_write` 鉴权，改无条件 `store_embeddings`）→ 并发修改资料 2 例失败。
- 去掉受控发送（直接用工厂构造未受控 provider）→ 12 例失败（全部 between 变体及重建批间用例，含 HTTP 通知变体：通知取消不早于放行）。

精简回归（SQLite 逐文件 ≤3 路）：test_kb 33、retrieval 15、memories 15、kb_index_claims 7、kb_legacy_document_cleanup 18、kb_document_guard_integration_e6 5、kb_document_claim_send_e6 8、bound_memory_index_e6 17、memory_index_actor_e6 1、collaboration_shared_index_e6 8、collaboration_memory_write_e6 20、analysis_index_receipt_enqueue_e6 13、shared_http_knowledge_e6 3 xfailed（预期）、collaboration_embedding_snapshot_send_e6 3、kb_index_admission_e6a 26、kb_index_guarded_e6b 31、collaboration_retrieval_core 27、collaboration_retrieval_callers 30、collaboration_retrieval_cache_preflight 54、collaboration_shared_context_e6 2、analysis_memory_enqueue_fencing 51、analysis_index_receipt_migration 8、kb_index_identity_migration 2、setting_ai_context 12（1 skipped）、collaboration_ai_guard 85、collaboration_task_revocation_e4 22、task_admission 7、task_sse_scope 18、哨兵 tasks 32、task_execution_fencing 11、recycle_bin_tasks 13、e5b 24、e5e 84、batch_generate 27、analysis 14、analysis_auto_actor_origin 4、collaboration_analysis_execution 128、collaboration_analysis_entry_e4g 55 —— 全部通过。PG（逐文件顺序）：e6b 3、e6a 3、kb_index_identity 1、kb_legacy_document_cleanup 4、kb_document_guard_e6 1、collaboration_shared_index_e6 4、bound_memory_index_e6 2、collaboration_memory_write_e6 9、collaboration_task_revocation_e4 2、task_execution_fencing 1、analysis_index_receipt 5、analysis_memory_enqueue 2、collaboration_retrieval 3、retrieval_callers 6、retrieval_cache_preflight 2 —— 全部通过。`ruff check .` 通过。

偏差与遗留：
- 作者行为变化（裁决 6 的预期代价）：运行中作者换默认向量或令牌变化现在失败关闭（原先按构建期 embedder 静默续写）；可见性戳改为每批读取，作者在索引期间调整设定 AI 可见性不会使整个索引失败，只会让该批重新快照；同批重复分块按截断文本去重、复用同一向量。
- 未补真实 HTTP 传输层（响应头前后撤权）的索引路径用例：本片用 FakeLLM 的模拟响应头门禁构造时序，真实 SDK 发送门禁由既有 `test_kb_document_guard_integration_e6.py` / `test_kb_document_guard_e6_postgresql.py` 覆盖原语。
- 未单独覆盖：作者 A→B→A 换配置、索引中文档被删除 / 重新索引（活动任务时删除已由 409 拦截）、重建与新上传并发（同项目 `serial_key` 串行）。
- PG 负对照的第一例因去掉了挂点而超时失败，不构成干净证明；有效证明为第二例（撤权先提交 → 无重验时写入）。
- E6-c（协作者上传/重新索引/重建/检索入口与记忆条目绑定）、E6-d（同步检索与大纲/实体/向导切作者向量）未动。

## 复核结论：2026-11-13 E6-b（check agent）

- 作者行为变化（重点 1）：运行中换默认向量 / 令牌变化 → 后续批零发送、不写入，任务 failed，文档经 on_abort 置 failed 并释放占用；已提交批次是作者当时有效配置的合法向量（作者旧键），与文档 failed 状态一致，属失败关闭非数据损坏；作者直接重试（validate_retry 走新受理）或 reindex / rebuild 即可恢复。结果载荷、关键词降级文案、进度行格式、done 事件不变。新增 A→B→A 用例：发送前 A 已恢复为同一实例默认 → 按 A 正常完成，B 零调用零写入。
- 发现并修复的作者回退：
  1. 文档模式改为只补空向量后，模型变更前失败的旧任务留下的其他空间分块会被保留，但文档仍被标 `ready`（HEAD 会替换为当前模型）。现于收尾写事务内按当前作者旧键检查本文档分块，仍有过期分块 → `ready_keyword_only` + 提示「请重建索引」。写入语义未放宽（仍不覆盖其他空间）。建议：是否让**作者本人**文档模式像 HEAD 一样替换本文档分块的旧空间向量，需用户裁决（协作者保持不覆盖）；作者旧记忆任务同理（HEAD 会替换过期 key，现只补空向量，依赖 index-status 的 needs_rebuild 引导重建）。
  2. `_embed_stale` 快照读取在 PostgreSQL 读已提交下分两条语句读检索行与业务来源，并发修改可使其暂时不一致而抛 `retrieval_reference_invalid`，原实现直接把整个（作者）索引记为「授权已变化」失败。现按「本批未发送未写入 → `_require_index_alive` 重验 → 重新快照」处理，计入 ≤3 次重试；授权失效仍失败关闭。
  3. `_require_index_alive` 把 SQLite/PG 写门禁短暂繁忙（`ai_visibility_busy`）误判为策略失效；现视为可重试（下一轮发送与写入仍各自完整重验），其余异常仍为 `EmbeddingPolicyError`。
- 条件写入（重点 2）：非重建仅选取并只写 `embedding IS NULL AND embedding_key IS NULL` 的行，store 再拒绝带向量的快照；重建按 `_stale_condition` 选取并 CAS 替换；写入条件 = 行快照（id/version/text/embedding/key）+ 来源快照 + 锁内重快照逐项相等。同批重复文本去重后按每行自身截断文本映射向量，每行仍各自 CAS——新增真实 HTTP 用例证明两段请求文本写入三行且向量对应正确。
- 重试语义（重点 3）：`not stored` 一律先 `_require_index_alive`（完整 `_require_index_write`：原 token+作者屏障、原执行、文档占用、ai.use+原轮次、配置重验）；授权失效不会进入「仅关键词」。新增「快照冲突 + 撤权」用例证明失败关闭。残留：若发送守卫因 `_require_index_write` 未覆盖的持久原因（如发送专属门禁）连续拒绝，3 次后降为 ready_keyword_only + 「资料变化」提示，零发送零写入，属保守降级而非泄露；文案可能不准确。
- 守卫放宽（重点 4）：`_matches_task_binding(legacy_owner_index=...)` 两处调用都以可信 Task/执行身份的 `kind == "kb.index"` 计算；`deserialize_shared_embedding(allow_legacy_owner)` 仅在 payload 缺席且 actor==owner、无 grant 时返回 None，再要求 `legacy_owner_index_selection`（source=actor、配置属作者）。其他 kind 不受影响。
- 降级分类（重点 5）：普通故障 = 无 `RequestPolicyError` 因果链的 `AIProviderError` → 降级；策略类（守卫 RequestPolicyError、AppError、配置键变化）→ 重验后失败关闭或重试。`_run_bound_memory` 的 `_policy_failure` → `_policy_cause` 修复正确；补 `test_bound_memory_ordinary_failure_degrades_with_warning`（真实 HTTP 400），负对照还原为 `_policy_failure` 时失败。
- E6-a PG 放宽（重点 6）：受理已提交后、返回前的投影复核与撤权竞争，撤权先提交时 404 是合法赢家；用例仍断言 Task 已落库、归协作者、带原轮次、文档占用已绑定，再继续验证 build 失败关闭，未掩盖问题。复跑 e6a+e6b PG 3×6 全过。
- helpers（重点 7）：`embed_before_gates/after_gates` 默认空列表，只在非空时消费；全部回归通过。
- 测试质量（重点 8）：原负对照已覆盖发送守卫（12 例）、写时重验（7 例）、写时快照 CAS（2 例）三层；「仅去 require_task_write 只 2 例失败」是因 store 内 `require_writing_embedding_snapshots` 冗余重验，属纵深防御，不需再补。
- 未覆盖项（重点 9）：已补 A→B→A、真实 openai_compat HTTP 端到端（含去重）。索引中删除文档：活动任务时 DELETE 409，不可达。真实传输层「请求在途撤权」：SQLite 下发送租约持写锁至响应头，撤权必然排在其后，时序不确定，未补；PG 锁序由 e6b PG 两例及既有 `test_kb_document_guard_e6_postgresql.py` 覆盖。
- 升级窗口：0045/本分支前入队、缺 `_task_actor_token_version` 的作者 kb.index 任务按引擎通用规则记 failed（「任务执行身份尚未确认」）并释放文档，作者 reindex 恢复；与其他 identity-aware 任务一致。

修改：`kb_index_service.py`（快照冲突重试、写门禁繁忙可重试、文档收尾过期分块状态）；`tests/test_kb_index_guarded_e6b.py`（`_world` 增 sections/base_url 参数；其他空间用例补状态断言；新增 8 例：快照冲突暂时/持续/撤权、alive 分类单元、A→B→A、绑定记忆普通故障、真实 HTTP 去重端到端）。负对照（备份 `/tmp/e6b_chk_NpmGob`，还原后工作文件 `shasum -a 256 -c` OK）：去快照冲突重试 → 3 例失败；去繁忙分支 → 1 例；去状态检查 → 1 例；恢复 `_policy_failure` → 1 例。

验证：SQLite（≤3 路）e6b 39、e6a 26、kb 33、retrieval 15、memories 15、kb_index_claims 7、kb_legacy_document_cleanup 18、kb_document_guard_integration_e6 5、kb_document_claim_send_e6 8、bound_memory_index_e6 17、memory_index_actor_e6 1、collaboration_shared_index_e6 8、collaboration_memory_write_e6 20、analysis_index_receipt_enqueue_e6 13、collaboration_embedding_snapshot_send_e6 3、shared_http_knowledge_e6 3 xfailed、setting_ai_context 12（1 skipped）、analysis 14、analysis_memory_enqueue_fencing 51、collaboration_ai_guard 85、哨兵 tasks 32、task_execution_fencing 11、recycle_bin_tasks 13、e5b 24、e5e 84、batch_generate 27 全过。PG 逐文件：e6b 3、e6a 3、kb_document_guard_e6 1、collaboration_shared_index_e6 4、bound_memory_index_e6 2、kb_legacy_document_cleanup 4 全过；e6a+e6b PG 另复跑 3 次全过。`ruff check .` 通过。

## 进度：2026-11-13 E6-c 编辑者向量入口开放 + 裁决 12（无新迁移）

实现：
- 裁决 12：`kb_index_service._stale_statement` 对文档模式（`source="kb"` 且限定本文档分块 ref_ids）改用 `_stale_condition`，与重建一样选取旧模型向量；`retrieval_service.store_index_legacy_embedding_snapshots` 允许文档模式 CAS 替换（快照身份已限定 `source_document_id`），记忆模式仍只补空向量。作者与授权编辑者均适用；其他文档与记忆不动。收尾复核保留：本文档仍缺当前作者旧键向量 → `ready_keyword_only` + `_STILL_STALE`（原 `_OTHER_SPACE` 改名改文案）。
- 上传 `kb_service.upload_document`：编辑者先 `lock_content_write_actor(visibility_write=True)`（门禁先于作品父行锁）；无授权 → 同步关键词（原行为）；已授权但作者无向量配置 → 同步关键词 + `KEYWORD_ONLY_OWNER_UNCONFIGURED`（裁决 2）；已授权且作者有配置 → `create_task` 建归编辑者的文档任务（E6-a 协作受理冻结原轮次与作者向量）。
- 重新索引 / 重建：`_locked_embedding_actor` 对非作者带 `visibility_write`；`_require_vector_task_owner` 无授权仍 403 `owner_embedding_not_allowed`（裁决 10），已授权放行（原 409 解除；裁决 3）。
- 人工记忆（裁决 4）：`memories._enqueue_vectors(…, memory_id)` 对 accepted editor 调新 `kb_index_service.enqueue_collaborator_memory`；`_validate_collaborator_index_task` memory 模式要求 `expected_grant`（写入时轮次，锁内比对）与 `memory_ids`，`_manual_memory_targets` 只绑定仍缺向量的本次条目写入服务端保留键 `_manual_memory_ref_ids`（登记 `_SERVER_TASK_KEYS`、个人投影内部字段），都已有向量 → `MemoryIndexCurrent` 不建任务。执行经 `_build_collaborator_index_job`（`IndexJob.memory_ref_ids`）走 E6-b 受控批次；`_require_index_snapshot_identity` 新增人工条目分支（须带 grant、快照属绑定条目）；`_lock_embedding_snapshot_sources` 的收据核验只对分析绑定记忆。`memory_service.may_enqueue_vectors` 同步为「作者或授权编辑者」。
- 分析派生（单章与批量）：`analysis_service.enqueue_memory_for_execution` 去掉 `grant_identity is None`；`_verified_analysis_index_source` / `require_bound_index_job` / `store_owner_legacy_embedding_snapshots` 改为 grant 一致性检查（作者 ⇔ 无 grant 且 actor=作者；编辑者 ⇔ 执行/选择/收据 grant 相同、配置属作者）；子 Task 经 `bind_task_grant` 携带原轮次；`build_index_job` 遇分析绑定键走新 `_build_bound_memory_job`（编辑者按 `require_authenticated(targets=作者)` → `require_write_execution` → `require_project(ai.use, grant)`）。`store_owner_legacy_embedding_snapshots` 另收紧为仅接受带收据的分析绑定记忆任务（复核回归时发现放宽后文档执行也能调用，已堵上）。
- 重试（评估后实现，改动小）：`KbIndexHandler.validate_retry` 对协作文档/重建任务重新受理（按当前作者授权与配置重新冻结选择），新轮次 ≠ 原轮次 → 409 `task_round_changed`；中央 `create_task` 的共享向量受理分支允许「重试且新旧 grant 相同」；`retry_task` 对 `shared_embedding_admission` handler 先取发送写门禁。分析派生 / 人工条目任务仍不可重试。实际行为：同轮次可重试（任务归编辑者）；HTTP 关作者模型授权（递增 revision）或撤权重邀 → 404（`_lock_user_task_project`）；同轮次直接改库关授权 → 403 `owner_embedding_not_allowed`。
- 订阅仍 404（裁决 5）；`index-status` / 检索调试 / `context-preview` / 大纲·实体·向导切作者向量属 E6-d，未动。

测试：
- 新增 `tests/test_kb_editor_entries_e6c.py`（24）：授权编辑者上传（任务归编辑者、费用来源=作者配置、只调作者模型、写作者旧键、其他文档不动）；作者无配置上传关键词；重新索引替换 + 全书重建；无授权编辑者 reindex/rebuild 403、上传/记忆仅关键词；{viewer 403、待接受/非成员/已撤权/仅管理员 404} 零任务零调用；记忆只向量化本次条目（作者遗留空向量不动、只改置顶不建任务、改正文只重建这一条）；作者无配置记忆不建任务；{上传、记忆}×{撤权、关授权、降 viewer} 受理后领取前失败关闭、文档占用释放；分析派生子任务发送前直接改库撤权/关授权零调用零写入；同轮次重试成功；关授权/重邀/直接改库关授权重试被拒；记忆受理绑定写入时轮次。
- 新增 `tests/test_kb_editor_entries_e6c_postgresql.py`（3）：撤权先提交 → 上传阻塞后 403/404 零副作用；上传受理先提交（`pg_blocking_pids`）→ 撤权后领取失败关闭释放占用；记忆受理先提交 → 撤权后领取失败关闭。
- 翻转（均因 E6-c 开放的预期语义）：e6b `test_document_mode_keeps_vectors_of_other_spaces` → `test_document_mode_replaces_old_model_vectors_only_in_this_document[author|editor]` + 新增收尾缺失保护用例；`test_collaboration_shared_index_e6.py::test_authorized_editor_does_not_silently_start_unfenced_vector_task`（409）→ 建归编辑者任务；e6a memory 模式缺绑定码 `collaboration_embedding_unavailable` → `task_admission_changed`；e4g `test_editor_manual_analysis_runs_as_real_actor_and_writes_back[True]` 与 e4g2 `test_editor_batch_uses_own_model_and_analyzes_as_real_actor` 零派生 → 子任务归协作者、带原轮次、作者向量。锁替身（shared_index e6 SQLite/PG）透传新的 `visibility_write` 参数。

负对照（备份 `/tmp/e6c_neg_IxaWem`、`/tmp/e6c_neg2_eElvAv`，每次还原后 `shasum -a 256 -c` OK）：文档模式恢复只补空 → 2 例失败；存储层拒绝文档替换 → 2；文档模式不限本文档 → 2；作者无配置不降级 → 1；记忆受理不核原轮次 → 1；编辑者记忆补全书 → 1；子任务不绑定 grant → 1；绑定记忆构建走作者分支 → 1（路径覆盖证明）；入口恢复 409 → 5；中央重试分支恢复 `retry_of is None` → 1；`store_owner_legacy` 不限分析绑定 → `test_collaboration_embedding_snapshot_send_e6` 1 例失败（即回归发现处）。

精简回归 SQLite（≤3 路逐文件）：analysis 14、analysis_auto_actor_origin 4、analysis_index_receipt_enqueue_e6 13、analysis_index_receipt_migration 8、analysis_memory_enqueue_fencing 51、batch_chapter_claim 55、batch_generate 27、bound_memory_index_e6 17、collaboration_ai_guard 85、collaboration_analysis_entry_e4g 55、collaboration_analysis_execution 128、collaboration_analysis_task_visibility 19、collaboration_batch_entry_e4g2 59、collaboration_embedding_snapshot_send_e6 3、collaboration_memory_write_e6 20、collaboration_retrieval_{cache_preflight 54, callers 30, core 27}、collaboration_shared_context_e6 2、collaboration_shared_index_e6 8、e5e 84、e5b 24、collaboration_task_privacy 17、collaboration_task_revocation_e4 22、kb 33、kb_document_claim_send_e6 8、kb_document_guard_integration_e6 5、kb_editor_entries_e6c 24、kb_index_admission_e6a 26、kb_index_claims 7、kb_index_guarded_e6b 41、kb_index_identity_migration 2、kb_legacy_document_cleanup 18、memories 15、memory_index_actor_e6 1、recycle_bin_tasks 13、retrieval 15、setting_ai_context 12（1 skipped）、shared_http_knowledge_e6 3 xfailed（预期，E6-d）、task_admission 7、task_execution_fencing 11、task_sse_scope 18、tasks 32、collaboration_planning_access 14、admin_projects 8、rich_document_analysis 10、project_transfer 37、collaboration_route_inventory 1 —— 全部通过。PG（逐文件顺序）：e6c 3、e6b 3、e6a 3、collaboration_shared_index_e6 4、bound_memory_index_e6 2、collaboration_memory_write_e6 9、analysis_memory_enqueue 2、analysis_index_receipt 5、kb_legacy_document_cleanup 4、kb_document_guard_e6 1、kb_index_identity 1、task_execution_fencing 1 —— 全部通过。`ruff check .` 通过。

偏差与遗留：
- 授权编辑者上传遇作者配置存在但无效（解密/端点）→ 400（不回退、不降级）；作者本人同情形仍降级关键词。
- 记忆受理：编辑者写入提交后、派生受理前若轮次变化，派生静默不建（路由吞异常记日志），记忆本身保留关键词；返回前的最终写屏障仍按原规则 401/403。
- `validate_retry` 的轮次比对是纵深防御：轮次变化通常先被 `_lock_user_task_project` 404 拦住，无独立负对照。
- 编辑者上传/重建的锁序改动（发送写门禁先于作品父行锁）无专门的死锁复现用例；PG 两种提交顺序用例通过。
- 前端（费用确认、作者未配置提示、记忆向量状态）属 E6-e/f；E6-d（index-status 精简、检索调试、context-preview、大纲/实体/向导切作者向量、`knowledge_for_http` xfail 转正）未动。

## 复核结论：2026-11-13 E6-c（check agent）

逐点：
1. 裁决 12：`_stale_statement` 只有在文档模式且 `source="kb"`、`ref_ids` 为本文档分块时才用 `_stale_condition`。`_require_index_snapshot_identity` 要求文档模式下每条快照都满足 `source=="kb"` 且 `source_document_id==request.document_id`，写事务中 `_lock_embedding_snapshot_sources` 还会按数据库重算快照并比对，所以替换不会越出本文档。记忆模式（作者旧记忆／编辑者本次条目）只补空向量。作者和编辑者使用同一条路径。收尾时按作者旧键重查缺失（`_STILL_STALE`）的逻辑仍然有效。补测：文档模式替换用例新增「本作品记忆的旧模型向量保持不变、正文不被发送」的断言。
2. 授权矩阵：上传、重新索引、重建，以及 viewer、pending、非成员、已撤权、仅管理员这几类身份，都由 e6c 矩阵覆盖。人工记忆：未授权编辑者和管理员只维护关键词、不建任务。分析派生：单章与批量的编辑者正例由 e4g/e4g2 覆盖。**缺少的负例已补**：未授权编辑者用本人文本模型分析时，即使作者和编辑者本人都有向量配置，也不应派生 kb.index、不应调用向量（收据为 no_work）。负对照：去掉 `resolve_shared_embedding` 中的 `allow_owner_ai` 判定后，该用例失败。作者路径只多取了门禁锁，test_kb、test_memories 和 test_tasks 全部通过。
3. 五处放宽：`_verified_analysis_index_source`、`require_bound_index_job`、`store_owner_legacy_embedding_snapshots`、`enqueue_memory_for_execution` 都限定为「作者 ⇔ 无 grant，编辑者 ⇔ 三方 grant 相同且配置属作者」。`store_index_legacy_embedding_snapshots` 拒绝分析绑定记忆，人工条目必须带 grant，并且只能写已绑定的 ref。`enqueue_memory`（作者旧记忆）仍只允许作者。`may_enqueue_vectors` 已无调用方，只是展示用的辅助函数。kb.index 只能通过 kb_service/memories 路由、分析收据和重试进入，不在 Agent `start_task` 白名单中。`_manual_memory_ref_ids` 已登记为服务端保留键；`expected_grant`/`memory_ids` 只是 handler 的入参，不会被复制进 input，且受理时在锁内比对原轮次。没有发现与 `store_owner_legacy` 同类的漏洞。
4. 锁序：所有写路径都先经过 `require_authenticated(write=True)`，依次取全局授权门（authorization_state）、User 升序、发送写门禁、作品父行。撤权和成员变更（`collaboration_service._lock_parent`）、章节受理、`require_task_write`、暂停收尾都是同一顺序。发送租约依次取 User 读锁、共享门禁、作品，方向一致。引擎领取 kb.index 时只锁作品、不取门禁，不会形成反向等待。结论：无死锁风险。补了 PG 并发冒烟用例：两实例下编辑者／作者上传、记忆、重建、成员 PATCH 与引擎领取、受控发送同时进行，连跑 3 次，无 500、无超时，任务全部结束，只调用作者模型。该用例不是确定性的死锁证明，主要依据仍是上面的锁序论证。
5a. **偏差已修正**：作者配置存在、但本地校验不可用（密钥无法解密、端点格式不对或被内网拦截、绑定无效）时，编辑者上传原先返回 400，现改为与作者本人上传一致，降级为同步关键词并提示 `KEYWORD_ONLY_OWNER_UNAVAILABLE`。这样零任务、零调用，作者也不产生费用；授权类 403/404 仍原样返回。依据：这属于配置不可用，按裁决 11 归为普通故障，不是授权或策略问题。降级不会给编辑者新增能力（未授权编辑者本来就能上传关键词文档），提示文案也不泄露配置细节。重新索引、重建是显式向量操作，仍返回 400（与裁决 10 的取向一致）。新增 `test_owner_embedding_unusable_upload_degrades_to_keyword[editor|author]`。负对照：去掉该降级分支后 editor 用例失败。规范 retrieval.md 已同步。
5b. 记忆提交后、派生受理前若轮次变化：撤权、关闭模型授权、降级都会递增 revision，路由最后的 `lock_content_write_actor` 会返回 403/404，用户能看到授权已变。只有作者删除向量配置、或出现暂时性故障时才完全无提示，此时与作者本人「无配置不派生」的行为一致。可以接受；建议由 E6-d 的 index-status 待建向量计数（裁决 7）来提示，不需要另加响应字段。
6. 重试：同轮次可以重试，任务仍归编辑者；关授权或重邀后返回 404（`_lock_user_task_project` 的 expected_grant）；直接改库关授权返回 403 `owner_embedding_not_allowed`。分析派生、人工条目和旧清理任务都不可重试。这与 E5 其他协作任务的重试规则一致（轮次变化即 404，不补签）。
7. 旧断言的调整：e6b 的「保留其他空间」改为按裁决 12 替换；shared_index 的 409 改为建任务、e6a 的错误码改为 task_admission_changed、e4g/e4g2 的零派生改为派生，都是 E6-c 开放带来的预期语义变化，理由成立。e4g[False] 分支只说明「作者无向量配置」这一种情况，未授权的负例由第 2 点新补的用例覆盖。
8. 「绑定记忆构建走作者分支」：把编辑者的绑定记忆送进作者分支，会被 `get_owned_project` 以 404 拒绝，本身就是失败关闭，因此原负对照只能证明代码执行到了这里。安全性另有证明：e6c `test_editor_analysis_child_index_fails_closed_after_grant_change`（发送前直接改库，结果零调用、零写入），以及引擎领取时的 `_collaboration_claim_failure`。不需要再补。

修改：`kb_service.py`（新增 `_owner_embedding_usable`、`KEYWORD_ONLY_OWNER_UNAVAILABLE`，上传遇配置不可用时降级）；`tests/test_kb_editor_entries_e6c.py`（+3：配置不可用降级 editor/author、未授权编辑者分析不派生）；`tests/test_kb_editor_entries_e6c_postgresql.py`（+1：并发锁序冒烟）；`tests/test_kb_index_guarded_e6b.py`（文档模式补记忆不受影响断言）；`.trellis/spec/backend/retrieval.md`（上传降级描述）。负对照备份 `/tmp/e6c_chk_WPdKhz`、`/tmp/e6c_chk2_ntNaYB`，还原后 `shasum -a 256 -c` 均 OK。

验证：SQLite（3 路逐文件）e6c 27、e6b 41、e6a 26、kb 33、retrieval 15、memories 15、kb_index_claims 7、kb_legacy_document_cleanup 18、kb_document_guard_integration_e6 5、kb_document_claim_send_e6 8、bound_memory_index_e6 17、memory_index_actor_e6 1、collaboration_shared_index_e6 8、collaboration_memory_write_e6 20、analysis_index_receipt_enqueue_e6 13、collaboration_embedding_snapshot_send_e6 3、shared_http_knowledge_e6 3 xfailed（预期，E6-d）、analysis 14、analysis_memory_enqueue_fencing 51、batch_generate 27、collaboration_ai_config 43、e4g 55、e4g2 59、tasks 32、task_execution_fencing 11、recycle_bin_tasks 13、e5b 24、e5e 84，全部通过。PG（逐文件顺序）：e6c 4、e6b 3、e6a 3、collaboration_shared_index_e6 4、bound_memory_index_e6 2、collaboration_memory_write_e6 9，全部通过。`ruff check .` 通过。

遗留：编辑者的人工记忆派生任务失败后不可重试，补救方式是重新编辑该条目或做授权重建，前端需提示（E6-e/f）；`may_enqueue_vectors` 已无调用方，可在 E6-d/f 决定删除或接入前端展示。

## 进度：2026-11-13 E6-d 同步检索与上下文（d1 + d2，无新迁移）

实现：
- d1 `kb_service.search_access(db, user_id, project_id, env) -> (embedder, grant)` 取代 `keyword_search_access`（删除 `_shared_index_unavailable` / 原「授权编辑者检索 409 collaboration_embedding_unavailable」）：作者不变；accepted editor 回滚旧事务后锁内固定原邀请轮次，无授权 / 作者无向量 / 作者配置本地不可用 → 关键词（持锁）；已授权且有配置 → `resolve_shared_embedding(for_update)` + 可见性戳 → 回滚 → `for_http` → `prepare_provider` → 作者旧键 `Embedder` → `guard_embedder`。`kb/search`、`memories/search` 共用；返回前 `_lock_search_result` 原轮次复核。
- `kb_service.knowledge_for_http`（新）：同选择 → `retrieval.search` → 原轮次复核 → 重新物化命中 → 与 `knowledge_text` 同格式；普通故障降级空串，策略 / 关联错误原样抛出。
- context-preview：作品作者 ≠ 请求者 → `context_builder._collaborator_retrieval_preview`（`require_project(ai.use)`，viewer 403；待接受 / 非成员 / 已撤权 / 仅管理员与不存在章节统一 404「章节不存在」）→ `search_access` → 原组装 → embedder 再按本次 `scope.stamp` guard → `scope.verify` → `_lock_search_result`。作者路径逐字不变。
- index-status（裁决 7/8）：`require_project(ai.use)`；作者完整视图不变；编辑者 `embedding_model=null`。`IndexStatus` 新增 `owner_embedding_configured`、`retrieval_mode`、`keyword_reason`（作者也返回）；`stale_count` 即「N 条资料待建向量」，作者 / 编辑者同口径（含编辑者关键词上传与人工记忆未派生 / 派生失败——E6-c 复核 5b 的提示落点）。`memory_service.may_enqueue_vectors` 删除（无引用）。
- d2（裁决 1/9）逐行核对：大纲 `_retrieval` 原先仅 `grant_identity is None` 才载入向量、并按候选键在旧键 / `shared:v2` 间选择；实体原先 grant → 只关键词、作者 → `shared:v2`（作者实体检索实际恒退关键词，因无生产写入方）；向导 grant → 只关键词、未冻结选择。现统一为受理冻结选择：vector → `for_task(execution, selection, stamp, extra_bindings=(文本绑定,))` 受控作者向量，只读作者旧键（不设 `_space_model_key`）；keyword → 原锁内重验后关键词。向导：`WizardHandler._claim` 仅协作执行冻结 `_shared_embedding`（作者输入不变），`build_job` 有键才反序列化（无键的旧协作任务按关键词），文本 send guard 与完成仅发布 guard 带该额外绑定；`_wizard_stream_shape` 允许恰一份共享向量额外绑定（主绑定须文本、须带 grant），`wizard_publication_successor` 另比较额外绑定。分析派生记忆召回：`analysis_service` 不做检索，无需改动。

测试：
- 新增 `tests/test_kb_sync_retrieval_e6d.py`（SQLite 31）：授权编辑者 kb / 记忆检索只调作者模型一次（hybrid）；{未授权、作者无配置}×{kb、记忆} 关键词零调用；作者检索不变；{viewer 403、待接受 / 非成员 / 已撤权 / 仅管理员 404}×{kb 检索、记忆检索、index-status、context-preview} 零调用且不存在章节与无权章节同 404 同 detail；context-preview 编辑者 200（授权作者向量 / 未授权关键词）且授权时与作者预览一致；`prepare_provider` 后、真实发送前 {撤权、HTTP 关授权、直接改库关授权、降 viewer}×{kb、记忆、预览} 零请求失败关闭；index-status 不泄露配置、两种降级原因、待建计数（含编辑者关键词上传）；协作者本人向量配置永不使用。
- 新增 `tests/test_kb_sync_retrieval_e6d_postgresql.py`（4）：撤权先提交 → 检索阻塞（pg_blocking_pids）后 403/404 零请求；发送租约先持锁 → 跨实例撤权阻塞，放行后返回前失败关闭不含资料；跨实例无通知直接关授权 → 409 零请求；实体任务在第二实例执行：授权只调作者向量，无通知关授权 → 失败零向量零文本。
- 转正 `tests/test_shared_http_knowledge_e6.py`（原 3 strict xfail → 3 passed）：按裁决 1 删除把索引改写为 `shared:v2` 的步骤（`openai_compat:owner-embedding:2` 即作者现用空间），另断言请求只含作者模型名。
- 翻转（裁决 9 / 1 的预期语义）：`test_editor_outline_uses_own_model_and_keyword_retrieval` → `..._and_author_shared_vectors`（夹具改写作者旧键 + 64 维向量、记忆移到第 0 章以覆盖展开）；`test_editor_entity_ai_uses_own_model_and_keyword_retrieval` → `..._and_author_shared_vectors`（新夹具 `_index_owner_kb_in_author_space`，全部分块）；`test_editor_wizard_uses_own_model_writes_content_and_keyword_retrieval[*-shared|legacy]` → `..._and_author_vectors`（legacy = 作品空间 → 一次作者向量；shared = `shared:v2` → 零向量）；`test_collaboration_outline_ai.py::test_owner_outline_recalls_legacy_or_shared_vectors_with_bound_model` → `..._only_author_legacy_vectors_...`（shared / 混用退回关键词）；`test_entity_ai_execution_identity.py::test_entity_embedding_send_rechecks_original_execution` 夹具改为旧键；`test_kb.py::test_upload_index_keyword_only` 精确字典补三个新字段；向导流两文件的 `for_task` 构造补 `extra_bindings`。新增：大纲 / 实体 / 向导各「未授权零向量」与「发送前 {直接改库关授权、撤权、降 viewer} 失败关闭」，向导「作者输入不冻结」，e5c3 编辑者 guard 恰一份额外绑定且缺失不可构造，successor「额外绑定缺失非后继」。

负对照（备份 `/tmp/e6d_neg_M68hWz`，每次还原后在工作区 `shasum -a 256 -c` 全 OK）：SQLite——授权编辑者退回关键词 → 4 失败；检索去掉 for_http 发送重验 → 13；index-status 返回配置名 → 1；context-preview 去掉协作分支 → 2；大纲协作仍关键词 → 2；大纲读 `shared:v2` → 6；实体协作仍关键词 → 4；向导不冻结 → 10；向导流形状不允许额外绑定 → 1；后继不比较额外绑定 → 1；向导检索去掉向量守卫 → 3。PG——检索去掉发送重验 → 2；实体去掉向量守卫 → 1；去掉检索返回前 `_lock_search_result` → 0（向量路径由搜索末尾 `_preflight_guarded_embedder` 先行拦截为 409，属纵深防御冗余；关键词路径由既有 `test_collaboration_shared_index_e6_postgresql` 覆盖）。

精简回归 SQLite（≤3 路逐文件）：kb_sync_retrieval_e6d 31、shared_http_knowledge_e6 3、kb 33、retrieval 15、memories 15、kb_editor_entries_e6c 27、kb_index_admission_e6a 26、kb_index_guarded_e6b 41、kb_index_claims 7、kb_legacy_document_cleanup 18、kb_document_claim_send_e6 8、kb_document_guard_integration_e6 5、bound_memory_index_e6 17、memory_index_actor_e6 1、collaboration_shared_index_e6 8、collaboration_memory_write_e6 20、analysis_index_receipt_enqueue_e6 13、collaboration_embedding_snapshot_send_e6 3、collaboration_shared_context_e6 2、collaboration_retrieval_{core 27, callers 30, cache_preflight 54}、setting_ai_context 12（1 skipped）、context_builder 19、setting_context_preview 5、e4g3a 62、e4g3b 69、e4g3c 95、collaboration_outline_ai 41、entity_ai_execution_identity 52、entity_tasks 6、relations_generate 31、outline_pro 16、wizard 30、wizard_resume 9、wizard_collaboration_{atomicity 8, closed 1}、wizard_cross_project_cascade 9、wizard_execution_identity 20、wizard_resume_identity 15、wizard_resume_publication 13、wizard_uncertain_cleanup 7、wizard_visibility_fencing 8、stream_wizard_e5c3 60、stream_wizard_successor 14、task_stream_evidence 2、e4f 41、e4g 55、e4g2 59、analysis 14、analysis_memory_enqueue_fencing 51、batch_generate 27、chapters 9、scene_generation 47、collaboration_chapter_execution 35、哨兵 tasks 32、task_execution_fencing 11、e5b 24、e5e 84、task_sse_scope 18、collaboration_route_inventory 1、admin_projects 8、project_transfer 37 —— 全部通过。PG（逐文件顺序）：e6d 4、e4g3a 4、e4g3b 4、e4g3c 5、stream_wizard_e5c3 5、stream_wizard_successor 4、collaboration_outline_ai 1、entity_ai_execution 6、e6c 4、e6b 3、e6a 3、collaboration_shared_index_e6 4、collaboration_retrieval 3、task_execution_fencing 1、wizard_dispatch 4 —— 全部通过；e6d PG 另复跑 2 次全过。`ruff check .` 通过。

偏差与遗留：
- 作者行为变化（裁决 1 的预期）：作者实体任务原读 `shared:v2`（无写入方，实际恒退关键词），现读作者旧键、可真正混合检索；作者大纲任务不再在旧键 / `shared:v2` 间自动选择，`shared:v2` 或混用键的候选退回关键词。作者向导任务输入与检索路径未改（仍 `knowledge_for_project`），与大纲 / 实体「作者也冻结」不一致，见裁决问题 1。
- 向导重试 / 续写按当前授权重新冻结共享向量选择（大纲重试沿用原冻结并重验）；文本模型绑定仍沿用原任务。升级前入队、无 `_shared_embedding` 的协作向导任务按关键词执行（不发送向量）。
- 偏严（与大纲 / 实体一致）：授权编辑者的向导 / 大纲 / 实体任务运行中，作者关闭授权或更换默认向量配置 → 冻结选择随文本 guard 重验失败，任务失败关闭（向导收据之后按既有收据收尾）。
- 检索选择对作者配置「存在但本地不可用」静默退关键词（与作者本人检索一致）；index-status 此时仍报 `retrieval_mode=vector`（只看是否配置），不额外区分。
- `IndexStatus` 新字段与 context-preview 对编辑者开放尚未同步前端类型 / 面板（E6-e）。`shared_embedding_model_key` / `store_embedding_snapshots` 等 `shared:v2` 原语保留，仅剩原语级测试（`test_collaboration_retrieval_{core,cache_preflight,postgresql}`），无生产读写方，见裁决问题 2。
- `_lock_search_result` 在向量路径为冗余层（见 PG 负对照）。

## 复核：2026-11-13 E6-d（check agent）

逐点：
1. 授权矩阵：检索调试（kb/记忆）、context-preview、index-status 四入口，作者路径不变；已授权编辑者只调作者向量（旧键）；未授权 / 作者无配置 / 作者配置本地不可用 → 关键词零调用；viewer 403；待接受 / 非成员 / 已撤权 / 仅管理员 404，context-preview 不存在与无权章节同 404 同 detail。大纲 / 实体 / 向导三类任务的授权正例、未授权零向量、发送前三种变化失败关闭均有覆盖。成立。
2. 不泄露配置：index-status 编辑者视图 `embedding_model=null`，新字段只含布尔 / 枚举。**发现并修复**：检索调试的 `warnings` 原样带出 `embed_query` 的提供方错误原文（`查询向量生成失败（{exc}）`），可能含作者模型名 / 端点；新增 `kb_service.public_search_warnings`，协作者只见固定文案（白名单：待建向量、超时、固定失败文案），作者原样。kb 与记忆两处均接入。
3. 旧键一致：生产代码中 `shared_embedding_model_key` / `store_embedding_snapshot(s)` 非 legacy 分支已无调用方，大纲 / 实体 / 向导 / 同步检索均构造不带 `_space_model_key` 的 `Embedder`（读 `provider:model:dim`）。`test_shared_http_knowledge_e6` 夹具 `openai_compat:owner-embedding:2` 即作者现用空间，删除改写 `shared:v2` 步骤后断言（命中资料、恰一次请求、只含作者模型名、零请求负例）未减弱，属裁决 1 的合理修正。
4. 翻转断言均可追溯：大纲 / 实体 / 向导编辑者零向量 → 作者向量（裁决 9），并新增未授权零向量负例；作者大纲 `shared` / 混用退关键词（裁决 1），仍断言无关查询下只有作者旧键向量能注入、无关空间不注入；`test_kb` 精确字典仅补三个新字段。未发现为通过而削弱的断言。
5. 发送守卫：每次查询嵌入经 `for_http` / `for_task` 租约重验；检索末尾 `_preflight_guarded_embedder` 再验。**发现并修复（裁决 11）**：`prepare_provider` 把 DNS / 端点构造等普通故障与授权变化统一为 `CollaborationSendBlocked`，编辑者同步检索因此 409「权限或模型授权已变化」，而作者同情形退回关键词。新增 `_require_unchanged_shared_selection`：失败后新事务锁内复核原轮次、accepted editor、`allow_owner_ai` 与冻结选择（`revalidate_shared_embedding`），全部成立 → 关键词（持锁返回）；任一变化 → 403。降级不扩权（未授权编辑者本就只用关键词）。任务侧（大纲 / 实体 / 向导）的 prepare 失败仍使任务失败关闭，与 E6-d 前作者大纲行为一致，未改。
6. 向导帧形状：`_wizard_stream_shape` 只允许恰一份 `ResolvedSharedEmbedding` 额外绑定且须 Task 带 grant、主绑定为文本；`_check_extra_bindings` 要求与 Task 输入 `_shared_embedding` 一致，且 Task 输入带该键时文本 guard 必须携带；向量 guard 主绑定为共享向量，形状判定返回 None，不能充当发布 guard；后继比较 `_extra_bindings`。其他任务类型不经此分支。成立。
7. 锁序：search_access / 新复核函数为 gate → User → 项目 → 配置；发送租约在独立会话；向导受理在已持锁的受理事务内冻结。与既有约定一致。
8. 偏差逐条：(a) 作者实体任务用上向量——裁决 1/9 预期，费用为作者本人配置，无安全问题；(b) 作者大纲遇 `shared:v2` 退关键词——该空间无生产写入方，无数据损失；(c) 向导重试按当前授权重新冻结、大纲沿用原冻结——两者都以当前授权在锁内重验，不会扩权，仅一致性差异，列为待裁决；(d) 配置本地不可用时 index-status 仍报 vector——仅提示不准确，不影响安全，列为待裁决。`_lock_search_result` 协作者分支未取写屏障：向量路径的权威复核在发送 / 末尾预检租约内，SQLite（非 WAL、读语句自动提交）与 PG（READ COMMITTED）下最终读取均为最新数据，仅剩返回前的微小窗口，属可接受线性化；已修正其 docstring 以免误导。

修改：`backend/app/services/kb_service.py`（`_require_unchanged_shared_selection`、`public_search_warnings`、search_access prepare 失败分支、`_lock_search_result` docstring）；`backend/app/services/memory_service.py`（warnings 脱敏）；`backend/tests/test_kb_sync_retrieval_e6d.py`（+7：提供方错误提示脱敏 ×2、prepare 失败授权未变降级 ×2〔kb、预览〕、prepare 期间授权变化失败关闭 ×3）；`.trellis/spec/backend/retrieval.md`（同步检索段补充）。

负对照（备份 `/tmp/e6d_chk_9YqI9E`，每次还原后工作区 `shasum -a 256 -c` OK）：去掉提示脱敏 → 2 失败；prepare 失败恢复直接抛出 → 2 例降级用例失败；降级不复核授权 → `owner_ai_direct` 1 例失败（撤权 / 降 viewer 另由 `_lock_search_result` 拦截，纵深防御）。

回归：SQLite（3 路逐文件）kb_sync_retrieval_e6d 38、shared_http_knowledge_e6 3、kb 33、memories 15、retrieval 15、collaboration_shared_index_e6 8、collaboration_retrieval_callers 30、setting_context_preview 5、context_builder 19、kb_editor_entries_e6c 27、tasks 32、collaboration_route_inventory 1、collaboration_retrieval_core 27、collaboration_retrieval_cache_preflight 54，全部通过。PG（逐文件顺序）：kb_sync_retrieval_e6d 4、collaboration_shared_index_e6 4、collaboration_retrieval 3，全部通过。`ruff check .` 通过。

待用户裁决：
- 向导重试 / 续写重新冻结共享向量选择 vs 大纲重试沿用原冻结：是否统一（建议统一为「沿用原冻结并锁内重验」，与大纲 / 实体一致）。
- index-status 在作者配置存在但本地不可用（解密 / 端点）时是否增加 `keyword_reason=owner_embedding_unavailable`（需改 schema 字面量与前端类型，属 E6-e）。
- 任务侧（大纲 / 实体 / 向导）向量 provider 构造的普通故障是否也按本次同步检索的方式「锁内复核后退关键词」，目前为任务失败关闭。

## 进度：2026-11-13 E6-e 知识库协作前端 + 裁决 16（无新迁移）

后端（裁决 16）：
- `IndexStatus.keyword_reason` 字面量新增 `owner_embedding_unavailable`（`app/schemas/kb.py`）。`kb_service.index_status(..., env)` 新增判定：已授权（或作者本人）且作者有向量配置时，经新 `_shared_embedding_usable`（只读 `resolve_shared_embedding(require_vector=True)`，不加锁、不联网，与上传 `_owner_embedding_usable` 同一 `_validate_local` 口径，仅 400 视为不可用）判定不可用 → `retrieval_mode=keyword` + `owner_embedding_unavailable`；未授权编辑者仍只报 `owner_embedding_not_allowed`（不探测作者配置）。路由 `GET /kb/index-status` 加 `EmbedEnvDep`。编辑者视图 `embedding_model` 仍为 null，响应不含故障细节。作者视图同样报该原因（作者横幅前端未改，见遗留）。
- 测试 `tests/test_kb_sync_retrieval_e6d.py` +5：`test_index_status_owner_embedding_unavailable[{editor,author}×{decrypt,endpoint}]`（同时断言检索确为关键词、零向量调用、不泄露密钥/端点/配置名）、`test_index_status_unauthorized_reason_wins_over_unavailable`。负对照（备份 `/tmp/e6e_kb_service.bak`，还原后 grep 核对）：去掉新分支 → 5 例失败。

前端：
- 类型：`IndexStatus` 补 `owner_embedding_configured`、`retrieval_mode`、`keyword_reason`（新 `IndexKeywordReason` 含 `owner_embedding_unavailable`）。
- `utils/kb.ts`：协作者纯函数 `collaboratorKbAccess`（能力只由 `retrieval_mode` / `keyword_reason` 派生）、`collaboratorUploadHint`、`collaboratorSearchNotice`、`collaboratorIndexBanner`（不显示作者配置名与「去模型配置」，`embedding_model=null` 不当作未配置；`stale_count>0` →「N 条资料待建向量」；vector 时可全书重建；记忆页附补救说明）、`collaboratorDocStatusHint`、`kbActionError`（403 `owner_embedding_not_allowed` 友好文案）；作者 `indexBanner` 仅把「需要重建索引」标题改为「需要重建索引：N 条资料待建向量」。
- `knowledgeUpload.ts`：模式改为 `owner | collaborator | none`（E6-c 已对授权编辑者开放上传，原 `unavailable` 不再成立）；协作者收到任务时通知附「使用作者共享的向量模型，费用由作者承担」。
- `KnowledgePanel` / `MemoriesPanel`：accepted 编辑者读 index-status（`enabled`），上传区按服务端结果提示费用来源或关键词原因；文档「重建索引」vector 时带费用 Popconfirm，否则置灰 + 原因 Tooltip；`IndexStatusBanner audience="collaborator"`（全书重建 Popconfirm 费用确认、发起前 `canEditNow` 复核）；检索调试对已授权编辑者显示并带模式/费用说明；viewer 不读状态、无写入口。作者界面除待建数标题外不变。
- 进度：`useIndexStatus` 新 `foreground`（协作者）→ `startIndexStatusPolling` → `startVisiblePolling`（页面可见才轮询、回前台立即刷新、结束停止）；作者保留原 3 秒 `setInterval`。`revalidateKey`（授权轮次 + allow_owner_ai）变化即重读，作者关授权后 15 秒前台权限轮询刷新快照，横幅随之回到关键词。`enabled=false` 时不返回旧状态。
- 记忆向量失败：后端 Task 无 input 模式字段，`TaskPanel` 以 `isCollaboratorMemoryIndexTask`（kind=`kb.index` + 服务端固定标题「剧情记忆向量」+ 当前已加载作品内协作者）识别，失败/取消/中断时不显示重试、显示「无法直接重试；可重新编辑此条或由有权限者全书重建」；记忆页横幅同样提示。文案常量放 `utils/projectAccess.ts`（入口 chunk 不得引 `utils/kb.ts`，构建产物已核对）。
- `ContextPreviewPanel`：403（`api/memories.ts` 的 `contextPreviewDenied`）就地显示「无权限查看注入预览」，不出错误 Alert/toast；AI 页签本就只对 canUseAI 挂载，编辑者 E6-d 后不再 404。

测试：新增 `pages/project/KnowledgePanel.access.test.ts`（7：授权向量 / 三种关键词原因 / 状态未读到 / viewer / 作者不变）、`pages/project/MemoriesPanel.access.test.ts`（3）、`hooks/useIndexStatus.access.test.ts`（3：可见暂停恢复、后台与无 visibility、作者定时不变）、`components/chapter/ContextPreviewPanel.access.test.ts`（2）；`utils/kb.test.ts` +5、`utils/projectAccess.test.ts` +1；翻转（E6-c 语义）`knowledgeUpload.test.ts` 模式断言、`KnowledgePanel.upload.test.ts` 首例改为以 index-status 为准。负对照（备份 `/tmp/e6e_neg_FWyV`，还原后 `shasum -a 256` 一致）：协作者不读状态/不前台轮询 → 4 失败；记忆任务判定去掉协作者条件 → 1；能力改看 `owner_embedding_configured` → 6。

验证：前端 `npm run lint` 通过、`tsc -b` 无错误、`npm run test` 216 文件 1502 例通过、`npm run build` 成功（入口仅含任务面板补救文案）。后端 `ruff check .` 通过；SQLite（3 路逐文件）kb_sync_retrieval_e6d 43、kb 33、kb_editor_entries_e6c 27、collaboration_route_inventory 1、memories 15、retrieval 15、shared_http_knowledge_e6 3、collaboration_shared_index_e6 8、tasks 32 全过；PG（顺序）kb_sync_retrieval_e6d_postgresql 4、collaboration_shared_index_e6_postgresql 4 全过。

规范：`backend/retrieval.md`（index-status 判定顺序与新原因）、`frontend/component-guidelines.md`（协作者视图、useIndexStatus）、`frontend/type-safety.md`（IndexStatus）。

偏差与遗留：
- 作者本人配置不可用时后端已报 `owner_embedding_unavailable`，但作者横幅按约束未改（仍按原逻辑显示「需要重建索引」并可点重建，后端会 400）；是否给作者也加提示待定。
- 记忆向量任务识别依赖服务端固定标题；任务不属于当前已加载作品时不判定（保持原重试按钮，后端 409）。如需稳健可另加服务端只读字段（需改 TaskOut，未做）。
- 检索调试对「已授权但作者未配置 / 配置不可用」的编辑者也显示（结果为关键词并带原因说明）；未授权编辑者隐藏（后端仍允许关键词检索）。
- 未做真实浏览器验证。

## 复核：2026-11-13 E6-e（check agent）

逐点：
1. 不泄露作者配置：编辑者 index-status `embedding_model=null`，新字段仅布尔 / 枚举；`_validate_local` 对 owner 来源只给固定文案，且 index-status 吞掉 400 不回传。前端协作者横幅 / 上传区 / 文档 Tooltip / 检索调试 notice / toast 只用 `KEYWORD_REASON_TEXT` 与固定费用文案，不显示「去模型配置」；`kbActionError` 仅映射 403 `owner_embedding_not_allowed`，其余沿用后端 detail（协作者路径的配置故障 detail 已在后端脱敏）。成立。
2. 后端判定：`_shared_embedding_usable` 调 `resolve_shared_embedding(for_update=False)`，经 `resolve_access(for_update=False)` 只读、不取写屏障，`_validate_local` 不解析主机名、不联网。优先级 未授权 → 未配置 → 不可用 成立，未授权分支不调用探测函数。**补测**：`test_index_status_unauthorized_editor_cannot_probe_owner_config_health`：未授权编辑者在作者配置可用、解密失败、端点无效三种情形下的响应 JSON 逐字一致。`owner_embedding_configured` / `stale_count` 对未授权编辑者透露「作者是否配置」是 E6-d 已接受的口径（裁决 8 只禁止名称和 ID），不透露是否可用。`EmbedEnvDep` 只读 `app.state`，无副作用；`svc.index_status` 只有这一个调用方，路由清单回归通过。
3. 前端能力判断：`collaboratorKbAccess` 只看 `retrieval_mode` / `keyword_reason`。`knowledgeUploadMode` / `collaborator` 只决定是否读取状态、展示哪个视图，不推算向量能力。成立。
4. 身份矩阵：viewer `enabled=false`，没有上传、重建和检索调试入口；未授权编辑者重建置灰并附原因、隐藏检索调试；已授权编辑者的全书重建和单文档重建都先经费用 Popconfirm，发起前 `canEditNow` 复核；403 返回友好文案，横幅在出错后刷新状态。后端协作者索引受理 `require_vector=True`，与「非 vector 置灰」一致。成立。
5. 作者界面：只把标题改为「需要重建索引：N 条资料待建向量」，作者用例断言其余不变。成立。
6. 轮询：协作者用 `startVisiblePolling`，作者保持原来的 3 秒定时；effect 清理会移除定时器和监听；`alive` 标志阻止旧响应写入。**发现并修复**：`revalidateKey` 或 `pid` 变化后，新状态读到之前 hook 仍返回旧状态（例如作者刚关闭授权时，仍短暂显示「向量可用」的横幅、重建按钮和费用文案）。现在状态按 `indexStatusKey(pid, revalidateKey)` 存储，`currentIndexStatus` 只返回键一致的结果；作者同键刷新（version / tick）不会闪烁。新增 2 例纯函数测试。
7. 任务面板：「剧情记忆向量」标题由服务端在 4 处固定写入；kind 必须为 `kb.index`，不会与文档或重建任务重名。只有当前作品的协作者才隐藏重试按钮，作者的旧 memory 模式任务（可重试）不受影响。协作者名下的这类任务在后端都不可重试：绑定键 409，无绑定的旧任务也会落到协作受理并因缺少条目而失败，所以隐藏重试按钮不会误伤。其他作品中的这类任务仍显示重试，点击后后端 409 并给出安全文案，没有副作用。可以接受。
8. ContextPreviewPanel：任何 403 都就地显示无权限，不弹错误 Alert；全局 client 只对 `permission_denied` 触发能力刷新，不弹 toast。404 等其他错误仍显示错误 Alert，没有退化。
9. 测试：`knowledgeUpload.test.ts` 中 `allow_owner_ai=true` 改为 `collaborator`，`KnowledgePanel.upload.test.ts` 改为以 index-status 为准，两处都能追溯到 E6-c（授权编辑者开放上传，裁决 2/3）。新的 access 测试按 owner / editor / viewer 分别断言具体文案、按钮的 disabled 状态和 `useIndexStatus` 入参，不是快照测试。

修改：
- `frontend/src/hooks/useIndexStatus.ts`：按键存储状态，并导出 `indexStatusKey` / `currentIndexStatus`。
- `frontend/src/hooks/useIndexStatus.access.test.ts`：+2 例。
- `backend/tests/test_kb_sync_retrieval_e6d.py`：+1 例。
- 规范：`frontend/component-guidelines.md`、`backend/retrieval.md` 的回归清单。

负对照（备份 `/tmp/e6e_chk_neg_mcMK`、`/tmp/e6e_chk_fneg_AllV`，每次还原后 `shasum -a 256 -c` 均为 OK）：
- 后端：未授权分支先探测作者配置并报不可用 → 2 例失败。
- 后端：未授权编辑者的 `owner_embedding_configured` 改为反映作者配置是否可用 → 新用例失败（另有 4 例同时失败）。
- 前端：`currentIndexStatus` 忽略读取键 → 2 例失败。

回归：
- 后端 SQLite（3 路逐文件）：kb_sync_retrieval_e6d 44、kb + memories 48、collaboration_route_inventory + retrieval + kb_editor_entries_e6c 43，全部通过。
- 后端 PG：kb_sync_retrieval_e6d_postgresql 4，通过。
- `ruff check .` 通过。
- 前端：`tsc -b` 无错误，`npm run lint` 通过，全量 vitest 216 个文件、1504 例通过。
- 未做真实浏览器验证。

待用户裁决：
- 作者本人配置不可用时，后端已报 `owner_embedding_unavailable`，但作者横幅仍提示「需要重建索引」并允许点击（后端 400）。是否给作者也显示「配置不可用，去模型配置检查」？（实现方遗留项，建议做）
- 记忆向量任务按标题识别。是否在 TaskOut 增加服务端只读字段（如 `retryable`），以覆盖其他作品中的同类任务、减少依赖标题？目前没有安全问题。

## 进度：2026-11-13 E6-f 收口（裁决 13、14、17、18、19，无新迁移）

实现：
- **裁决 13（作者向导冻结）**：`WizardHandler._claim` 对作者与协作执行都在同一受理事务 `resolve_shared_embedding(for_update=True)` 并写 `_shared_embedding`（作者：无 grant、`source=actor`；无向量 → keyword）。`build_job` 去掉「带键须有 grant」限制，改为 grant 与执行一致；`build_wizard_job` 的引擎执行（`execution` 非空）作者与协作者走同一检索分支（文本 guard `require_task_write` → 冻结 vector 时 `for_task(selection, extra_bindings=(文本绑定,), resume=…)` 受控作者向量；否则关键词）。`knowledge_for_project` / 续写本人 `get_embedder` 路径只剩无执行身份的直调。`SendGuard._wizard_stream_shape` 允许额外绑定的 grant 与执行一致（作者为 None）；`wizard_publication_successor` 原本已比较前后额外绑定，未改。
  - **兼容（升级前已入队、无冻结选择的作者向导任务）**：引擎执行时无 `_shared_embedding` → 只用关键词、零向量发送，任务照常完成；不补冻结、不回落到未受控的作者运行时解析。同样处理：受理时作者本人默认向量配置本地不可用（`BadRequestError`）的作者任务——受理成功、不写该键（保持原「退回关键词」行为，不因裁决 13 变成 400）；协作执行遇作者配置本地不可用仍按 E4-g3c 原样 400。
  - 裁决 15 不变：重试 / 续写经 `_claim` 按当前授权重新冻结（作者首次 keyword、配置向量后重试为 vector，有用例）。
- **裁决 14（删除 `shared:v2` 原语）**：先 grep 确认生产代码无读写方。删除 `retrieval_service.shared_embedding_model_key`、`store_embedding_snapshots`、`store_embedding_snapshot`、`Embedder._space_model_key`（及 `_GuardedEmbeddingState.space_key`）、`_SHARED_MODEL_KEY_RE` 与 `key_matches_model` / `_stale_condition` 的 `shared:` 分支；`_store_embedding_snapshots` 去掉 `legacy_owner` 参数（恒写作者旧键），原「当前配置 vs 快照配置」比较改用私有 `_bound_config_identity`（同一组身份字段，只比较不作空间键）。遗留 `shared:v2:*` 键行只会被视为非当前模型。
  测试逐个处理：
  - `test_collaboration_retrieval_core.py`：`test_bound_author_space_is_versioned_and_not_interchangeable`（只测 v2 键格式 / 匹配 / 版本）**删除**，其中「配置换密钥即不同」的价值并入 `test_changed_author_model_fails_closed_before_vector_write[model|api_key]`；`test_invalid_shared_embedding_key_stays_stale_and_is_not_sent`（只测 v2 键的严格维度匹配分支）**删除**；CAS 内核相关 7 例（比较交换、批量原子、不 flush 无关草稿、模型变化失败关闭、批中来源竞争、版本耗尽、撤权重邀）**改写保留**——内核仍被作者旧键两个写入口共用，改为直接调用 `_store_embedding_snapshots` 并断言旧键 `openai_compat:同名:model:2`。
  - `test_collaboration_retrieval_cache_preflight.py`：参数 `space`（篡改 `_space_model_key`）三处**删除**（字段已不存在），其余篡改项保留；夹具不再构造 v2 键。
  - `test_collaboration_retrieval_postgresql.py`：写入改调内核、`Embedder` 去掉 `_space_model_key`（**改写保留**，PG 串行化价值不变）。
  - `test_collaboration_embedding_snapshot_send_e6.py::test_shared_space_rejects_v1_and_config_integer_id_reuse` → `test_bound_selection_rejects_config_integer_id_reuse`：删 v2 / v1 键断言，**保留**「同整数 ID 重建配置不能冒充原绑定」并改断言 `_bound_config_identity` 不同。
  - `test_collaboration_entity_entry_e4g3b.py` / `test_collaboration_outline_ai.py`：「非作者现用空间」夹具改用字面键 `shared:v2:000…`（模拟遗留数据，**保留**退回关键词的覆盖）。
- **裁决 17（任务侧构造故障降级）**：新增 `collaboration_ai_guard.prepare_shared_embedding(guard, sessionmaker, factory, request_timeout)`，大纲 `_retrieval`、实体 `_knowledge_for_execution`、向导检索三处共用：`prepare_provider` 抛 `CollaborationSendBlocked` 后新事务以同一 vector guard `require_task_write` 锁内复核（原令牌、原 Task 执行、原轮次与 accepted editor、`allow_owner_ai`、冻结选择 `revalidate_shared_embedding`、可见性戳、实体来源、续写来源），全部成立 → None（关键词继续，零向量发送）；任一变化 → 失败关闭；续写来源 / 设定来源等非 `CollaborationSendBlocked` 的策略错误原样抛出。作者任务同样适用。
- **裁决 18（作者横幅）**：`utils/kb.ts` `indexBanner` 在 `keyword_reason === 'owner_embedding_unavailable'`（建索引中仍优先）返回 warning「向量配置不可用，请到模型配置检查」+ `configLink` + `rebuildDisabledReason`；`IndexStatusBanner` 作者视图显示「去模型配置」、重建按钮置灰带 Tooltip；`KnowledgePanel` 作者文档「重建索引」同样置灰。协作者视图未动。
- **裁决 19（TaskOut 只读字段）**：`TaskOut.retry_blocked_reason: memory_bound | legacy_index | binding_invalid | request_invalid | null`。唯一判定 `kb_index_service.retry_blocked_reason(task_input)`，`KbIndexHandler.validate_retry` 与 handler 钩子 `retry_blocked_reason(task)`（`task_service.to_out` 读取）共用；列表 / 详情 / SSE 快照同源。管理端任务监控用独立白名单 schema，不复用 TaskOut、不提供重试，故不加该字段。顺带修复：中央 `retry_task` 对非对象 `request` 原先 `dict(...)` 抛 500，改为交 handler 判定（409「信息无效」）。前端 `Task.retry_blocked_reason?`、`taskRetryBlockedHint`（`utils/projectAccess.ts`），`TaskPanel` 改看该字段，删除按标题识别的 `isCollaboratorMemoryIndexTask`。

测试：
- 新增 `backend/tests/test_task_shared_embedding_e6f.py`（20）：作者向导冻结 vector 并经守卫只调一次作者向量；作者无向量冻结 keyword；作者配置本地不可用仍受理且零向量；升级前无键作者任务零向量；作者向导发送前改向量配置 / 换默认 → 零向量零文本、内容不变；作者重试重新冻结；{大纲、实体、向导}×{编辑者、作者} 构造普通故障 → 关键词成功零向量；{大纲、实体、向导}×{直接关授权、直接撤权} 构造失败期间变化 → 失败关闭、零向量零文本、且未做关键词检索；作者续写向量查询带续写来源。
- 新增 `backend/tests/test_task_retry_blocked_e6f.py`（11）：10 种输入下字段 ⇔ `validate_retry` 是否在受理前拒绝；HTTP 列表 / 详情字段一致、重试 409 一致；非 kb.index 恒 null。
- 翻转：`test_collaboration_wizard_entry_e4g3c.py::test_author_wizard_input_unchanged_without_shared_embedding` → `test_author_wizard_freezes_shared_embedding_without_grant`；`test_collaboration_stream_wizard_e5c3.py::test_wizard_publication_shapes_and_stream_guard[owner]` 由「作者无额外绑定」改为「作者恰一份无 grant 额外绑定，伪造 grant 即非向导形状」；`test_wizard_resume_identity.py::test_changed_source_before_kb_embedding_never_sends` 的 hook 点由已不在引擎路径上的 `retrieval_service.get_embedder` 改为向量 provider 构造后（断言不变）。
- 前端：`utils/kb.test.ts` +1、`KnowledgePanel.access.test.ts` +1（作者不可用：文案、去模型配置、两处重建均置灰）、`utils/projectAccess.test.ts` 以 `taskRetryBlockedHint` 用例替换原标题识别用例。

负对照（后端备份 `/tmp/e6f_bneg_*`、前端 `/tmp/e6f_fneg_*`，每次还原后工作区 `shasum -a 256 -c` 全 OK）：
- 13：作者不冻结 → 6 失败；作者引擎任务回到旧未受控路径 → 4 失败；帧形状仍要求 grant → 2 失败。（「向量 guard 去掉续写来源」不构成有效对照：`for_task` 在构造时即拒绝缺续写绑定，已有结构性保证。）
- 14：内核写入键改回 v2 形 → 2 失败。
- 17：不降级直接失败 → 6 失败；降级不锁内复核 → 6 失败（加强后：断言失败关闭发生在关键词检索之前；加强前因后续文本 guard 兜底为 0 失败）。
- 18：去掉 `indexBanner` 不可用分支 → 2 失败；文档按钮不置灰 → 1 失败。
- 19：`to_out` 不填字段 → 1 失败；`validate_retry` 不按判定拒绝 → 5 失败；前端提示函数恒 null → 1 失败。

回归：
- SQLite（≤3 路逐文件）：e6f 20 / 11、kb_sync_retrieval_e6d 44、shared_http_knowledge_e6 3、kb_editor_entries_e6c 27、kb_index_admission_e6a 26、kb_index_guarded_e6b 41、kb_document_claim_send_e6 8、kb_document_guard_integration_e6 5、bound_memory_index_e6 17、memory_index_actor_e6 1、collaboration_shared_index_e6 8、collaboration_memory_write_e6 20、analysis_index_receipt_enqueue_e6 13、collaboration_embedding_snapshot_send_e6 3、collaboration_shared_context_e6 2、collaboration_retrieval_{core 26, callers 30, cache_preflight 51}、retrieval 15、kb 33、memories 15、kb_index_claims 7、kb_legacy_document_cleanup 18、e4g3c 95、e4g3a 62、e4g3b 69、collaboration_outline_ai 41、entity_ai_execution_identity 52、entity_tasks 6、relations_generate 31、outline_pro 16、wizard 30、wizard_resume 9、wizard_collaboration_{atomicity 8, closed 1}、wizard_cross_project_cascade 9、wizard_execution_identity 20、wizard_resume_identity 15、wizard_resume_publication 13、wizard_uncertain_cleanup 7、wizard_visibility_fencing 8、stream_wizard_e5c3 60、stream_wizard_successor 14、task_stream_evidence 2、tasks 32、task_execution_fencing 11、task_sse_scope 18、collaboration_route_inventory 1、ai_config_instance_migration 11、collaboration_ai_config 43；哨兵 collaboration_chapter_receipt_identity 10、task_execution_identity 21、setting_ai_context 12（1 skipped）、task_event_guard 6、prompt_parity 60、e5e 84、e5d 33、collaboration_style_privacy 7、endpoint_guard 32、project_lifecycle 12、series_projection_guards 9、outline_snapshots 4、task_statistics 12、projects_and_story 4、collaboration_log_private_views 17、admin_projects 8——全部通过（e5c3 与 resume_identity 首轮各 1 例失败，即上述两处翻转，修正后复跑通过）。
- PG（顺序）：kb_sync_retrieval_e6d 4、e4g3c 5、e4g3a 4、e4g3b 4、stream_wizard_e5c3 5、stream_wizard_successor 4、collaboration_outline_ai 1、entity_ai_execution 6、e6c 4、e6b 3、e6a 3、collaboration_shared_index_e6 4、collaboration_retrieval 3、collaboration_retrieval_cache_preflight 2、wizard_dispatch 4、wizard_resume_identity 2、task_execution_fencing 1、bound_memory_index_e6 2、kb_legacy_document_cleanup 4——全部通过。
- `ruff check .` 通过；前端 `tsc -b` 无错误、`npm run lint` 通过、全量 vitest 216 文件 1506 例通过。未做真实浏览器验证。

规范：`backend/retrieval.md`（向导冻结与兼容、裁决 17 降级、`shared:v2` 原语删除、`retry_blocked_reason`）、`frontend/component-guidelines.md`（TaskPanel 改看服务端字段、作者不可用横幅）、`frontend/type-safety.md`（`Task.retry_blocked_reason`）。

偏差与遗留：
- 裁决 17 未直接复用 `kb_service._require_unchanged_shared_selection`：该函数按 HTTP 成员轮次复核且拒绝作者本人，任务侧需要按原 Task 执行身份复核（并覆盖作者任务）。新 helper 复用同一 vector guard 的 `require_task_write`，其中冻结选择判定与 HTTP 版同为 `revalidate_shared_embedding`，口径一致，没有另写复核逻辑。因 `revalidate_shared_embedding` 含本地可用性校验，构造后作者配置变为「本地不可用」（例如密钥轮换后无法解密）会失败关闭而不是降级，与 HTTP 版行为一致。
- 作者行为变化（裁决 13 预期）：作者向导查询向量改经受控守卫发送，运行中作者改了向量配置或换了默认向量 → 任务失败关闭（原先按运行时默认静默使用新配置）。作者绑定类 `kb.index` 失败任务（如分析派生记忆）在任务面板也不再显示重试（原先点了 409）。
- 管理端没有 TaskOut，未加字段（见上）。

## 复核：2026-11-14 E6-f（check agent）

逐点：
1. 裁决 13：`WizardHandler._claim` 对作者与协作执行都在受理事务冻结 `_shared_embedding`；`build_job` 要求冻结选择的 grant 与执行一致（作者为 None）。引擎执行时作者与协作者走同一分支：文本 guard 带额外绑定，向量查询走 `for_task` 守卫。`_wizard_stream_shape` 限定向导四种类型，只接受恰一份共享向量额外绑定，其 grant 须与原执行一致；`_check_extra_bindings` 在构造时对照 Task 输入。e5c3 用例覆盖了作者伪造 grant 和编辑者伪造 None 两种反例。没有冻结选择的作者旧任务只走关键词，不发送向量（有用例）。作者配置本地不可用时，只吞掉作者分支（grant 为 None）的 `BadRequestError`，不写键、只用关键词，使用的仍是作者本人资料，没有越权。重试会重新冻结（有用例）；续写的冻结与来源复核由 resume 用例覆盖。成立。
2. 裁决 14：生产代码中已无 `shared:v2` 原语。测试里只剩字面量夹具，用于模拟遗留数据。`_bound_config_identity` 比较 (config_id, instance_key, owner_id, provider, base_url, model, api_key_enc)，并做类型和范围校验。按同一整数 ID 重建的配置会得到新 instance_key，因此身份不同，有用例覆盖；只换密钥时 api_key_enc 不同，`test_changed_author_model_fails_closed_before_vector_write[api_key]` 覆盖。删除的 2 个用例只测 v2 键格式；改写的用例仍断言 CAS、原子性和失败关闭，强度没有下降。
3. 裁决 17：`prepare_shared_embedding` 只在遇到 `CollaborationSendBlocked` 时进入复核。复核在新事务里调用同一 vector guard 的 `require_task_write`，加锁顺序为：认证 gate → User（`require_authenticated(write, targets=owner)`）→ `lock_visibility_write` → 项目父行 → Task → 配置；依次复核原令牌、原执行、原轮次、`allow_owner_ai`、冻结选择（`revalidate_shared_embedding`）、可见性戳和来源。复核失败直接抛出，此时还没有做关键词检索（有用例断言 `retrieved == []`）。`WizardResumeChanged` / `entity_source_changed` 不属于 `CollaborationSendBlocked`，原样抛出。不复用 `_require_unchanged_shared_selection` 的理由成立：该函数按 HTTP 成员轮次复核，并且拒绝作者本人。
4. 裁决 18：只在作者视图（`!collaborator`）显示「去模型配置」（`/settings/ai`，路由存在）以及置灰的重建按钮和 Tooltip；文档「重建索引」只在 `canUseOwnerAI` 分支置灰。协作者分支没有改动。
5. 裁决 19：`retry_blocked_reason` 是唯一判定。`validate_retry` 和 `to_out` 都调用它；列表、详情和 SSE 快照（`stream_evidence` / engine 中的 `out_json` / `out_json_live`）都经过 `to_out`。该字段只根据本人任务自己的输入计算，`binding_invalid` 不读取作者配置或授权，也就不会泄露。管理端使用独立白名单 schema、没有重试入口，因此不加这个字段是合理的。前端已删除按标题识别的逻辑。`request` 为非对象时返回 409，HTTP 用例的 `broken_request` 覆盖了这一点。

**发现并补测**：第 17 条的失败关闭反例只覆盖了编辑者（直接关授权、撤权），没有覆盖作者任务（无 grant）。现新增 `test_author_embedding_prepare_failure_with_changed_frozen_config_fails_closed[outline|entity|wizard]`：在构造失败期间直接修改作者冻结的向量配置，预期零向量、零文本、未做关键词检索、结果为空，向导内容不变且无未决凭据。规范 `backend/retrieval.md` 补充了作者可见的两条行为变化。生产代码没有改动。

负对照（备份 `/tmp/e6f_chk_neg_GixoFG`，还原后执行 `shasum -a 256 -c` 结果为 OK）：去掉 `prepare_shared_embedding` 的锁内复核后，新增的 3 例全部失败。

回归：SQLite 按文件跑、最多 3 路并行，以下全部通过：task_shared_embedding_e6f 23、task_retry_blocked_e6f 11、collaboration_retrieval_core 26、cache_preflight 51、embedding_snapshot_send_e6 3、entity_entry_e4g3b 69、collaboration_outline_ai 41、wizard_entry_e4g3c 95、stream_wizard_e5c3 60、wizard_resume_identity 15、outline_entry_e4g3 62、tasks 32、kb_sync_retrieval_e6d 44、kb 33、shared_http_knowledge_e6 3、entity_ai_execution_identity 52、kb_index_guarded_e6b 41、kb_editor_entries_e6c 27、stream_wizard_successor 14、wizard 30、kb_legacy_document_cleanup 18、bound_memory_index_e6 17、route_inventory 1。PG 按顺序跑，以下全部通过：collaboration_retrieval 3、kb_sync_retrieval_e6d 4、cache_preflight 2、wizard_entry_e4g3c 5、stream_wizard_e5c3 5、collaboration_outline_ai 1、entity_ai_execution 6、wizard_resume_identity 2。`ruff check .` 通过。前端：`tsc -b` 无错误，lint 通过，全量 vitest 216 个文件、1506 例全部通过。没有做真实浏览器验证。

需用户知悉（不阻塞）：
- 作者向导排队期间，如果向量配置变为本地不可用（如密钥轮换后无法解密），文本 guard 的额外绑定复核会让任务失败关闭，而不是退回关键词；受理时就已不可用的配置则照常走关键词。两者行为不同，原因是任务受理后不再重新选择配置，已写入 spec。
- 按裁决 18，作者文档「重建索引」在配置不可用时置灰，因此作者暂时无法用关键词重新分块失败的文档（修好配置后即可恢复）。

## 进度：2026-11-14 E4i-a（E5a 收口：Agent 能力判定与授权失效错误；只改后端，不开放协作者入口，无新迁移）

实现：
- **新增 `app/services/agent/access.py`**：
  - `lock_agent_write(db, user_id, project_id, *, grant, visibility_write=True, allow_collaborator=True, on_denied="changed")`：先 `authz.require_authenticated(write=True)`（认证门禁 + actor User 屏障，与原 `_lock_owned_project` 相同的首批语句，保证 PG RR 快照在其后冻结），再 `project_access.resolve_access(for_update=True, visibility_write=…)`（发送写门禁 → 作品父行锁 → 锁后 `populate_existing` 重读成员）。放行只有两种：作者本人且 `grant is None`；accepted **editor**、`can_use_ai`、`(invitation_id, revision) == grant`。管理员人工 `projects.edit` 不并入（管理员兼 viewer / 已撤权成员 / 无 grant / 角色往返后的旧 grant / 作者带 grant 均拒绝）。
  - 两种失败模式：`on_denied="changed"`（锁后复核）抛 `AgentAuthorizationChangedError`（协作者失去读取来源的 404 也归为授权失效；作者的 404 / 删除中 409 / 401 保持原错误）；`on_denied="not_found"`（HTTP 入口）维持原 404。
  - `task_grant(task)`：取轮次 Task 的 `_collaboration_grant`，键存在但值破损时失败关闭。
- **接线**：`tools/base.lock_project`（新增 `grant`、`visibility_write` 参数）→ `lock_agent_write`；`ToolCtx.grant`（作者 None）；runner `_process_calls` 的 `ToolCtx` 取 `job.task_execution_identity.grant_identity`；`execute_change` 先读轮次 Task 取 grant，再 `lock_agent_write(grant=…)`（替换 `lock_author_visibility_write(manage=False)`），`ToolCtx(visibility_locked=True, grant=grant)`；`owned_change(lock)` / `_turn(lock)` / `_lock_owned_project` 改调 `lock_agent_write(allow_collaborator=False, on_denied="not_found")`——入口仍只放行作者（E4i-d 翻转开关并传原 grant）；`_lock_owned_project` 仍不取发送写门禁（原行为）。
- **确认路径跳过复核的排查与修复**：原 `execute_change` 在 `lock_author_visibility_write(manage=False)` + 历史/可见性检查后构造 `ToolCtx(visibility_locked=True)`，`Tool._lock_write` 见到该标志即直接返回——既不重取锁、也不复核能力；runner 自动执行也经 `execute_change`，同样跳过。另 `Tool.preview` 把普通 `AppError`（含 403/404）转成 `{"error"}`，`execute_change` 据此记 `failed` 并由 `decide_turn` 继续 / runner 继续。现 `_lock_write` 每次都调用 `lock_project`（`visibility_locked` 时 `visibility_write=False`，只省去重取发送门禁与 `scope.verify`）；能力失效抛 `AgentAuthorizationChangedError`（`VisibilityPolicyError` 子类），`error_result` / `Tool.execute` / `execute_change` / `_execute_admitted_task` / `decide_turn` / runner 均原样上抛，终止本轮。`start_task` 的 `execute_admitted` 不调 `_lock_write`，但同事务内 `execute_change` 顶层与 `tool.preview` 已复核，中央受理再按 `ai.use` 复核。
- **撤销（裁决 6、8）**：`_undo_changes` 锁内要求作者或原轮次 editor（`_undo_grant`：批内各行轮次 Task 的 grant 须存在且唯一一致，否则 `AgentAuthorizationChangedError`）；接 `transactions.audit_scope`，`effects.undo` 经 `TransactionSession(audit_mode="execute")` 调用，根提交改 `coordinator.commit`；失败回滚后由协调器以独立会话（有 engine 用 `engine.ctx.sessionmaker`，否则 `visibility_sessionmaker(db)`）按 operation_id 补记。不新增审计动作。核实：`effects.undo` 内部调用的 `scene_service.replace_chapter_contents/replace_legacy_content/initialize_chapter/refresh_projection`、`setting_field_service.claim_fields_revision`、`setting_category_service.claim_categories_revision` 都不开 `AuditOperation`，故当前真实撤销不产生管理审计；协调器为防御性接入（测试以模拟的审计服务验证）。

测试：
- 新增 `tests/test_agent_access_e4ia.py`（SQLite 26 + PG 6）：管理员来源 × {viewer, 已撤权, 无 grant, 角色往返旧 grant} × {预览, 执行} × {未加锁, visibility_locked} 均抛 `AgentAuthorizationChangedError` 且作品不变；原 grant 的管理员兼编辑者与作者可写、作者带 grant 拒绝；入口仍作者专用（编辑者 `_lock_owned_project` / `owned_change(lock)` 404，同一函数复核模式放行原轮次 editor）；确认路径工具内复核（`execute_change` 顶层通过后同事务降级，工具预览内复核读到并终止，提案仍 proposed，调用序列 `[visibility_write=True, False]`）；授权失效 confirm-all（模拟 E4i-d 入口、真实能力函数）`after_entry` 409 `permission_denied`、两项均 proposed、未续跑、未执行（`committed` 变体在投影核对处即 409）；撤销链路嵌套审计成功 / 失败各恰一条。PG：管理员来源三类、确认路径复核、撤销审计两例。
- 调整：`test_agent_audit_integration.py` 去掉 `lock_author_visibility_write` monkeypatch（只保留 `owned_change` / `owned_conversation` / 投影 / `maybe_resume` / `get_tool` 的入口模拟），确认与工具写入走真实能力函数，8 例通过；`test_agent_audit_coordinator.py::test_tool_propagates_explicit_preview_and_execute_scopes` 中纯管理员改为管理员兼 accepted editor 并传 grant（原用例正是「管理员人工来源经 Agent 写入」的形态）；`test_agent_project_style_privacy.py` 两组编辑者/管理员兼编辑者用例传 grant；`test_agent_authenticated_actor.py` 的门控点由 `lock_author_visibility_write` 改为 `agent_access.lock_agent_write`。
- PG 首跑发现回归并已修复：`lock_agent_write` 最初直接 `resolve_access`，其先 `load_record` 读作品，使 RR 快照早于认证门禁冻结；`test_agent_authenticated_actor.py::test_agent_conversation_writes_revalidate_at_write_gate_postgresql[create|update|delete|send]` 在门禁处并发撤销令牌后得到 409（40001）而非 401。改为先 `require_authenticated(write=True)`（与原 `_lock_owned_project` 相同）后全部通过；规范已写明首批语句要求。

负对照（备份 `/tmp/e4ia_neg_3byxYy`，每次还原后工作区 `shasum -a 256 -c` 三文件均 OK）：
1. 管理员来源被接受（非作者分支改为 `can_edit_content` 放行）→ 17 失败（管理员来源 16 例全挂 + 管理员兼编辑者 after_entry 终止用例）。
2. 授权失效后继续执行（复核失败改抛普通 `ForbiddenError`，可被工具层转 `{"error"}` / 记 failed）→ 21 失败（含确认路径复核、两种 actor 的 after_entry 终止用例）。
3. 确认路径跳过复核（恢复 `visibility_locked` 时 `_lock_write` 直接返回）→ 10 失败（全部 locked=True 用例 + `test_confirm_path_rechecks_inside_tool`）。
4. 撤销不接审计协调器（`effects.undo` 直接用 db、根提交改回 `db.commit()`）→ `test_undo_coordinates_nested_audit[failure]` 失败（失败审计随回滚丢失）。

回归：
- SQLite（按文件，≤3 路并行，最终代码复跑）：全部 `test_agent_*.py` 非 PG 文件 35 个（access_e4ia 26、action_identity 12、api 11、audit_coordinator 19、audit_integration 8、authenticated_actor 18+1 xfailed、auto_external_fencing 58、change_identity_migration 8、context 7、context_actor_version 9、context_execution_identity 18、context_model_binding 5、conversation_final_snapshot 5、derived_chapter_admission 8、derived_task_admission 11、effects 29、effects_claim_identity 6、external_deletion_guard 39、mcp_send_lease 24、mcp_skills 21+6 xfailed、model_admission 3、model_value_privacy 3、preview_guard_privacy 4、project_style_privacy 35、providers 59、recovery_claim_identity 30、retry_execution_fencing 9、runtime 20、stale_change_identity 3、summary_task_token_identity 7、task_actor_binding 9、tools 54、turn_identity_runner 17、unknown_outcome 14、visibility 8）；哨兵 progression_agent_effects 12、progression_agent_gemini 1、scene_agent 14、setting_category_agent 68、setting_field_agent 34、task_agent_evidence 11、skill_inputs 15、project_lifecycle 12、skill_generation 8、audit_authorization_no_autoflush 10（1 skipped，PG 用例）——全部通过。
- PG（`AINOVEL_COLLAB_POSTGRES_TEST_URL`，按文件顺序，未与 SQLite 并行）：access_e4ia 6、audit_coordinator 3（含预览/执行事实、嵌套失败根提交前不可见、真实 40001 安全失败）、authenticated_actor 8、audit_authorization_no_autoflush 11、project_style_privacy_postgresql 4、task_agent_evidence_postgresql 6、turn_identity 2、external_deletion_guard 9、auto_external_fencing 3、change_identity 2、context_actor_version 3、context_execution_identity 2、summary_task_token_identity 1——全部通过，无 skip。
- `ruff check .` 通过。

research §13 本片相关项核实：
- 「未运行任何测试」：本片已在 SQLite 与 PG 实跑上述用例。
- `test_task_retry_admission_api.py:95` 是否含 `agent.turn`：只用 `outline.continue`，不含 `agent.turn`；E4i-b 不需翻转该用例。
- §5.3「`_undo_changes` 未接 `audit_scope`、`effects.undo` 不经 service 不触发 `AuditOperation`」：前者已修；后者核实属实（见上「撤销」），本片不新增审计动作。
- §5.3「提交确认丢失 PG 未在最新代码重跑」：`test_agent_audit_coordinator.py` 的 PG 3 例已在最新代码重跑通过；SQLite 的提交前/后注入用例（同文件）也通过。
- 其余 §13 项（`manual=True` 分支副作用、`start_task` 预览 `ai_config_id`、提及路由 actor、`replace_chapter_text` 与单章 claim 互斥）属 E4i-c/d，未在本片核实。

偏差：
- 撤销把 `effects.undo` 包进 `TransactionSession`（研究建议只接 `audit_scope`）：否则协调器无法接到撤销链路内的审计作用域，接入只剩形式；`effects.undo` 内的 `commit()` 降为 flush 不改变语义（其本身不提交）。
- `lock_agent_write` 放在新模块 `agent/access.py`，未放 `setting_ai_visibility.py`：它依赖 Agent 专用错误与 grant 解析，放在 agent 包避免反向依赖。
- `_lock_owned_project` 保持不取发送写门禁（原行为）；撤销前 `owned_change(lock)` / `_turn(lock)` 已在同事务取得门禁，`effects.undo` 再取为重入，锁序未变。

遗留（交给后续切片）：
- 协作者路径的 User 屏障顺序：`lock_agent_write` 先锁 actor，再由 `resolve_access` 锁作者 User。作者 ID 小于协作者时不是严格升序，与「多个 User 按 ID 升序」的约定不一致，存在与其他同时锁两者的事务（如作者改成员）死锁的理论可能（PG 会以 40P01 中止一方，现映射为安全 500）。作者路径不受影响。E4i-d 开放入口前需处理：可由调用方传入锁前读到的作者 ID，按升序一次锁定后在锁内核对。
- 降级已提交后再确认：协作者的个人投影随之变化，`_require_change_action` 先以 409 `agent_change_changed` 拒绝，不会走到能力复核的 `permission_denied`。两者都失败关闭，但错误码不一致；E4i-d 可考虑把能力复核前移到投影核对之前。
- runner `_process_calls` 里读取作品用的 `project_service.get_owned_project`、读工具、`_check_summary_execution` 等仍限作者（E4i-b/c）。

## 复核：2026-11-14 E4i-a（check agent）

结论：实现符合裁决 6/8/9 与硬约束（管理员 `projects.edit` 不并入、确认路径不跳过复核、授权失效终止整轮）。本次复核没有改动应用代码，只补了测试并更新了规范。

逐项核对：
1. 能力函数 `lock_agent_write` 只放行两类人：作者本人且 `grant is None`；或 accepted editor，具备 `can_use_ai`，且 `(invitation_id, revision)` 与 grant 一致。以下情况都会拒绝：管理员兼 viewer、管理员兼已撤权、纯管理员（无 grant）、角色往返后的旧 grant、作者伪造 grant。复核另外补了非管理员协作者的 viewer、已撤权、旧 grant、持他人 grant 四种情况。
2. 复核不可跳过：`_lock_write` 每次都调用 `lock_project`，`visibility_locked` 只省掉门禁和 `scope.verify`。runner 预览、自动执行、确认（`execute_change` 顶层＋工具 preview/execute）都会复核。`start_task` 的 `execute_admitted` 依赖同一事务内的顶层复核和 preview 复核。外部（MCP）确认先经过 `execute_change` 顶层复核，回程走作者专用的 `_lock_owned_project`。
3. 终止整轮：`error_result`、`Tool.execute`、`execute_change`、`_execute_admitted_task`、`decide_turn`、runner 读写工具和自动执行都会把 `VisibilityPolicyError` 原样上抛。逐项确认后的状态是一致的：前面已提交的项保留 effects，可以撤销；当前项的保存点和根事务回滚，失败审计由协调器补记；提案保持 proposed，不会续跑。
4. 调整过的旧测试：`audit_coordinator` 改为管理员兼 editor＋grant，符合裁决 6；纯管理员被拒由新测试覆盖。`authenticated_actor` 的拦截点仍在认证屏障之前，断言 401 和无副作用不变。断言没有被削弱。
5. 撤销：作者路径的提交和回滚语义与原实现等价。原来是 `db.commit()` 或回滚后上抛；现在是 `coordinator.commit`（无未决审计时只执行 commit）。失败时多一次空协调，不产生审计或写入。加锁顺序：入口 `owned_change/_turn(lock)` 取门禁 → `_lock_owned_project` 重入 → `effects.undo` 门禁重入，顺序未变。`TransactionSession.rollback` 为 no-op，异常仍由外层回滚，属于失败关闭。
6. 加锁顺序：作者路径与原来一致（先 `require_authenticated(write=True)`，再由 `resolve_access` 加锁）。协作者路径先锁 actor、后锁作者，不是升序。但两把 User 锁都在全局 gate 之后取得：所有锁多个 User 的路径都先取 gate，不取 gate 的路径（发送 lease、作品创建、登录）只锁单个 User，因此不会成环。本片**不修**，理由如下。
   - 真实的首次加锁点是入口和 runner 的 `_lock_execution`；工具内传作者 ID 无效。
   - 若改成先取 gate、再读作品、再升序加锁，会在 PG RR 下让 `test_agent_conversation_writes_revalidate_at_write_gate_postgresql` 的令牌撤销从 401 变成 40001。
   - 发现一个**更真实的风险**，留给 E4i-b/d：runner 路径 `_lock_execution` 已持有发送写门禁后，工具才锁作者 User。这可能与作者的发送 lease 在 PostgreSQL 上形成死锁：作者先 FOR SHARE 锁住自己的 User 行，再等待共享门禁。已写入 `agent.md`。
7. 错误码：降级已提交后，确认返回 409 `agent_change_changed`。只泄露“提案已变化”，不含私有数据，属于失败关闭，没有安全问题。仅错误码与 `permission_denied` 不一致，E4i-d 可以把能力复核前移。
8. 入口开关：HTTP 层实测，10 个写路由（会话创建/改/删、发送、确认/拒绝、整轮确认/拒绝、撤销、整轮撤销）对 editor 和管理员兼 editor 都返回 404，且无副作用。

补充测试（`tests/test_agent_access_e4ia.py` 新增 7 例，SQLite 33 passed、6 skipped（PG 用例））：
- `test_http_agent_write_entries_still_404_for_collaborators[editor|admin]`
- `test_plain_collaborator_without_original_editor_grant_is_denied[viewer|revoked|stale_grant|foreign_grant]`
- `test_undo_grant_fails_closed_on_mixed_or_broken_grants`

负对照（备份在 `/tmp/e4ia_check_neg_lLTrQo`，还原后用 `shasum -a 256 -c` 核对两文件均 OK）：
1. `owned_change` 入口改用 `require_project(content.edit/read)` 放行 → HTTP 用例 2 例均失败。
2. 去掉 role=editor 与 grant 一致性检查 → stale_grant、foreign_grant 失败。viewer 和已撤权仍被 `can_use_ai` 或 404 拦住，属于纵深防御。
3. `_undo_grant` 允许混合 grant → 撤销 grant 用例失败。

回归：
- SQLite（按文件、3 路并行）：audit_integration 8、audit_coordinator 19（另有 3 skip）、api 11、runtime 20、authenticated_actor 18（另有 8 skip、1 xfail）、project_style_privacy 35、turn_identity_runner 17、effects 29、tools 54、visibility 8、access_e4ia 33，全部通过。
- PG（按文件顺序，未与 SQLite 并行）：access_e4ia 6、audit_coordinator 3、authenticated_actor 8、project_style_privacy_postgresql 4，全部通过。
- `ruff check .` 通过。

留给后续切片：
- E4i-d：入口翻转时，reject/reject-all 在 `confirm=False` 下不经过 `execute_change` 复核，入口必须自行按 `ai.use`＋grant 判定（裁决 4：viewer 不能确认或拒绝）。`_undo_changes` 需要翻转 `allow_collaborator`，并决定协作者撤销失败时返回 404 还是 409 `permission_denied`。错误码一致性见第 7 条。
- E4i-b：处理第 6 条的 runner 锁序。runner 自动外部调用（`_external_call`）不经过 `lock_agent_write`，须按裁决 2 对 editor 关闭 MCP。
- 既有行为（非本片引入）：工具内 `UnauthorizedError` 会被 `error_result` 转成普通工具错误，提案记为 failed。之后的 `_lock_execution` 或提交会以 401 失败关闭。

## 进度：E4i-b（2026-11-14，受理与 handler：模型来源、grant、配置归属；只改后端，协作者 HTTP 入口仍关闭）

实现：
- **硬门槛：runner 锁序（裁决 16）**：`execution_identity.require_write_execution` 新增可选 `project_owner_id`（仅协作执行传入；须带 grant、≠ actor）。传入时先 `require_authenticated(actor, write=True, targets=(owner,))`（gate → actor / 作者 User 升序），再取发送写门禁、作品父行，锁后核对 `project.user_id == owner`，然后 Task。作者执行不传，行为不变。runner `_lock_execution` 与 `_checkpoint` 的提交后复核传入 `_collaboration_owner_id(job)`：作者取自领取时冻结的 `model_binding`，绑定缺失或与 grant / actor / 作品不一致即 `_StaleAgentExecution`。之后工具里 `lock_agent_write` 再锁作者 User 只是重入。`build_job` 与编辑者摘要保存也走同一顺序。
  - `send_message` 的会话锁改为 `owned_conversation(lock=True, visibility_write=True)`。原因：AgentHandler 声明就绪后，中央受理改按 `require_project(ai.use, visibility_write=True)` 重入，门禁必须先于作品父行，否则会在作品锁之后才取门禁。`validate_and_claim` 自身也以 `visibility_write=True` 加锁。
- **AgentHandler（`task_engine/handlers/agent.py`）**：声明 `collaboration_execution_ready = True`。
  - 新增辅助函数：`_task_grant`（键破损时失败关闭）、`_lock_turn_conversation`（作者走原 `owned_conversation`；协作者走 `lock_agent_write(grant, allow_collaborator=True)`，会话只按真实 actor 查，不属于本人仍 404）、`_check_turn_binding`（绑定须与锁内作者、原 grant 一致；`source` 为 actor 或 owner；作者轮次只能是 actor；`config_owner_id` 与来源对应）、`_bound_config`（作者沿用 `resolve_config`；协作者按 `config_owner_id` 与 `instance_key` 读取）。
  - `validate_and_claim` / `validate_retry` / `revalidate_resume` / `build_job` 去掉“带 grant 即拒”和“`source` 必须为 actor”的限制，作品作者改用锁内取得的值。重试与续跑只沿用原轮次 grant（锁内比对成员当前轮次，重邀即拒，裁决 7）；`_recover_retry_source` 要求原轮次 grant 一致。`build_job` 对协作轮次在锁内以 `for_task` 核对冻结绑定与执行身份能否构成受控 guard。
  - `bind_queued_turn`：允许 grant，但必须与 `_project_model.grant_identity` 一致。作者旧形状（无 `_project_model`）仍兼容。
  - 模型选择（本人优先、授权开启且本人无配置才回退作者、本人配置无效不回退）仍由中央 `preflight_project_model` 完成，handler 只核对冻结结果。
- **runner（`agent/runner.py`）**：
  - `verify_job_resources` 按绑定的 `config_owner_id` 加锁并读取配置，同时核对绑定的 actor、作品、grant 与配置 ID。
  - 新增 `job_project`：作者用 `get_owned_project`；协作者用 `require_project(ai.use, grant_identity=原 grant)`，失败抛 `AgentAuthorizationChangedError`。替换 `_process_calls` 与 `_turn` 中原先只认作者的两处读取。
  - **MCP 对编辑者关闭（裁决 2/15）**：协作轮次不调用 `build_tool_map`（断点记 `{}`），不向模型提供外部工具规格，`_process_calls` 不解析残留映射；`_external_call` 对协作轮次直接记“工具不存在”，不建提案、不发网（包括绕过 `lock_agent_write` 的自动调用）。
- **发送守卫与摘要（`agent/context.py`，裁决 13）**：
  - `_SummaryExecution` 增加 `project_owner_id`、`grant_identity`、`summary_source`。`_summary_execution` 与 `_check_summary_execution` 泛化：作品作者、Task grant、配置归属（`config_owner_id`）、会话 `summary_source` 都纳入锁内比对。
  - 协作轮次的每次主对话与摘要发送，由 `_collaboration_summary_guard` 以**当前已证明的执行身份**新建 `collaboration_ai_guard.for_task` guard（authorize / verify / preflight），并在同一 lease 事务内再做会话、消息、风格核对。撤权、降为 viewer、作者模型授权关闭后零模型请求。
  - `_finish_summary`：作者沿用 `lock_author_visibility_write`；协作者走 `require_write_execution(project_owner_id=)`，保存时写 `summary_source = "collaborator:<邀请UUID>:<轮次>"`（作者写入为 NULL）。
  - `build_messages`：原规则是“非作者会话有摘要即不可验证”，改为 `_summary_source_verified`：作者会话要求来源为空；协作者会话只承认本授权轮次的标记，无标记或来自其他轮次一律 `AgentHistoryUnverifiable`。
- **迁移 0053**（父 0052）：`agent_conversations.summary_source`，可空 String(80)，原位加列。降级直接删列，属于失败关闭：旧代码不承认非作者摘要，再升级后标记为 NULL，同样不承认。Agent 表不导出，`APP_SCHEMA` 不变。
- **撤销（裁决 14，`agent/service.py::_undo_changes`）**：改为 `lock_agent_write(grant, allow_collaborator=True, on_denied="changed")`。协作者因撤权、降级或旧轮次被拒时返回 409 `permission_denied`；会话或提案不属于本人仍由入口 `owned_change` / `_turn` 返回 404；作者的生命周期错误不变。协作者撤销不再派生作者全书记忆向量任务（原先依赖异常被吞掉）。
- 规范：`backend/agent.md` 的 User 锁序条目标注已修复，新增“协作轮次（E4i-b）”条目。

测试：
- 新增 `tests/test_agent_handler_e4ib.py`（SQLite 25 例）：
  - 模型来源：编辑者本人模型；授权开启且本人无配置时回退作者模型（日志 `model_source=owner`，actor 为编辑者）；本人配置无效不回退；未授权且无配置被拒；作者轮次不变。
  - 发送前撤权：revoke、viewer、授权回退下 revoke、作者模型授权关闭，均零模型请求且任务失败。
  - 重试：同一轮次可重试，沿用原 grant 与作者配置；重邀后重试被拒，零请求、无新任务。
  - 续跑：同一轮次 handler 接受；重邀后 handler 404、HTTP 拒绝。
  - 锁序：SQLite 顺序断言（编辑者 actor 与作者 User 先于门禁锁定，工具复核只是重入；作者只锁本人）；`require_write_execution` 作者不一致时拒绝。
  - MCP：编辑者轮次不调用 `build_tool_map` / `tool_specs` / `resolve`；`_external_call` 拒绝协作 job。
  - 摘要：标记写入且同轮次可复用；无标记或他轮次标记不可验证，零发送；作者会话带协作标记不可验证；撤权或降级后摘要零发送。
  - 撤销：revoke、viewer、stale_grant 返回 409 `permission_denied`；原 grant 可撤销；他人会话 404。
  - 0053 迁移往返。
- 新增 `tests/test_agent_lock_order_e4ib_postgresql.py`（PG 2 例）：
  - 两连接锁序：编辑者真实 `_lock_execution` 加工具复核，对作者真实 lease 顺序（`require_authenticated_for_send` → 共享门禁），用 Event 与 `pg_blocking_pids` 编排，无固定等待，双方都成功。
  - PG（RR）下编辑者轮次端到端授权回退。
- 调整 `tests/test_agent_audit_integration.py`：模拟的协作 job 补上冻结 `model_binding`。生产中协作轮次总带绑定；缺失时失败关闭，属于新语义。断言未放宽。

负对照（每次还原后对工作区文件执行 `shasum -a 256 -c` 均为 OK）：
1. 锁序（`/tmp/e4ib_neg_lock_*`）：`_lock_execution` 不传 `project_owner_id` → PG 用例失败，作者 lease 得到 `VisibilityBusyError`（编辑者在作者 User 行上等待，作者轮询门禁超时）；SQLite 顺序用例 `[editor]` 失败。
2. 授权回退（`/tmp/e4ib_neg_fallback_*`）：`_bound_config` 一律按 actor 调用 `resolve_config` → 回退用例与重试用例失败（2 例），本人模型用例仍通过。
3. 撤销 409（`/tmp/e4ib_neg_undo_XstU`）：恢复 `allow_collaborator=False, on_denied="not_found"` → 4 例失败。

回归（最终代码）：
- SQLite（按文件，最多 3 路并行），全部通过：
  - Agent 全部非 PG 文件 36 个：access_e4ia 33（6 skip）、action_identity 12、api 11、audit_coordinator 19（3 skip）、audit_integration 8、authenticated_actor 18（8 skip、1 xfail）、auto_external_fencing 58、change_identity_migration 8、context 7、context_actor_version 9、context_execution_identity 18、context_model_binding 5、conversation_final_snapshot 5、derived_chapter_admission 8、derived_task_admission 11、effects 29、effects_claim_identity 6、external_deletion_guard 39、handler_e4ib 24（另有 0053 迁移 1 例单独跑过）、mcp_send_lease 24、mcp_skills 21（6 xfail）、model_admission 3、model_value_privacy 3、preview_guard_privacy 4、project_style_privacy 35、providers 59、recovery_claim_identity 30、retry_execution_fencing 9、runtime 20、stale_change_identity 3、summary_task_token_identity 7、task_actor_binding 9、tools 54、turn_identity_runner 17、unknown_outcome 14、visibility 8。
  - 协作与任务文件：collaboration_content_review 22、editor_proposal 30、log_failure_privacy 5、log_private_views 17、paused_cleanup_e5a 25、plan_delete_guard 16、stream_early_subscribe_e5e 84、task_revocation_e4 22、route_inventory 1；task_abort_identity 22、receipt_deletion_guard 83、sse_scope 18、agent_evidence 11、retry_admission_api 5。
  - `test_migrations.py::test_upgrade_and_downgrade` 1。
- PG（按文件顺序执行，未与 SQLite 并行，无 skip），全部通过：lock_order_e4ib 2、access_e4ia 6、audit_coordinator 3、authenticated_actor 8、auto_external_fencing 3、change_identity 2、context_actor_version 3、context_execution_identity 2、external_deletion_guard 9、project_style_privacy 4、summary_task_token_identity 1、turn_identity 2、task_agent_evidence 6。
- `ruff check .` 通过。未跑全量（按精简回归策略）。

偏差：
- 新增迁移 0053，任务描述中没有要求。来源标记若写进 `summary` 文本，会经 `ConversationOut.summary` 泄露，也可能被旧摘要伪造，所以改用独立列。若另一会话同时在建 0053，需要协调版本号。
- `for_task` guard 没有在 `build_job` 中一次性构建后复用：Agent 断点每步推进，构建时的 guard 在首次断点后就会因 `matches_running` 失败。现在 `build_job` 只做形状核对，每次发送按当前已证明身份新建 guard（与作者路径 `_summary_guard` 的做法一致）。
- `require_write_execution` 的作者预锁是**显式传参**，只接入 Agent 路径，没有对所有 grant 执行体自动生效，以免影响其他 handler。
- `send_message` 作者入口的锁序有变化：会话锁同时取发送写门禁，这是声明就绪后必须的。
- 重邀后，旧轮次的 HTTP 重试由原模型预检以 400「模型绑定无效或已失效」拒绝，而不是 404/409，结果同样是失败关闭。

待确认 / 遗留：
- **其他 handler 可能有同类锁序反转（本片之前就存在，未修）**：引擎 `_event_task` → `require_write_execution`（actor User → 发送写门禁）之后，outline/entity/wizard 等的 `verify_event` 调 `guard.require_task_write`，此时才锁作者 User（`targets=(owner,)`）。理论上会与作者发送 lease 交叉等待；PG 下表现为作者 lease 在门禁轮询 2 秒后得到 `ai_visibility_busy`。建议单独立项：让 `_event_task` 对 grant 执行体也传入 `project_owner_id`，并补双连接回归。
- 工具（读、写、正文、`start_task`）对编辑者仍走只认作者的 service（E4i-c）。编辑者轮次调用这些工具会拿到工具错误，不会崩溃。
- `agent.turn` 尚未纳入撤权后的排队 / 待审核受限收尾（E4i-e）。编辑者被撤权后，排队中的轮次会停在退避状态，待审核轮次停在 paused。
- 通用 `/tasks/{id}/retry` 与 `/resume` 已对协作者的 `agent.turn` 生效（handler 已就绪）。但在 E4i-d 开放前，HTTP 无法产生这类任务。

## 复核：E4i-b（2026-11-14，check agent）

结论：锁序硬门槛已满足，模型来源、grant、配置归属、MCP 关闭、撤销 409 与入口关闭均符合裁决。发现并修复 2 处问题（均已补测试与负对照）。

核对要点：
- 锁序：协作者路径 `_lock_execution`、断点提交后复核、`build_job`、编辑者 `_finish_summary` 都经 `require_write_execution(project_owner_id=)`，顺序为 gate → actor/作者 User 升序 → 发送写门禁 → 作品 → Task → 配置（PG 配置锁是 advisory，不锁 User 行）。重试和续跑走 `_lock_user_task_project` → `resolve_access(visibility_write=True)`，User 按升序锁定。作者路径不传参数，行为不变。`send_message` 新取的门禁位于作品父行之前。PG 双连接用例只用 Event 和 `pg_blocking_pids` 编排，没有 sleep；按旧顺序时作者 lease 会以 busy 失败，所以用例确实证明了修复。
- 低风险观察（未改）：`lock_agent_write` 先单独锁 actor，`resolve_access` 再锁 `(actor, owner)`。owner.id < actor.id 时，User 实际上按降序加锁。这些路径都持有排他的 DB gate，不持 gate 的发送 lease 只锁单个 User，因此不会形成环。建议 E4i-d 开放协作 `send_message` 时，评估在 gate 后先预取作者 ID 统一升序（受 RR 首语句约束）。
- 模型来源和守卫：编辑者本人模型、授权回退、本人配置无效不回退、`verify_job_resources` 锁内按 `config_owner_id` 核对，均成立。每次主发送和摘要发送都按 `execution_state.current` 新建 `for_task` guard，`job.provider` 只经 `build_messages` 和 `guard_agent_provider` 两处重新包装后使用，没有绕过守卫的发送路径。
- 摘要标记：只有 `_finish_summary` 写入该标记，`ConversationOut` 不含该字段，Agent 表不导出，因此无法伪造，对外也不可见；其他轮次和作者会话一律不承认该标记。
- 撤销：HTTP 入口仍先经 `owned_change` / `_turn`（`allow_collaborator=False`），协作者在入口处得到 404。`_undo_changes` 内部拒绝时返回 409 `permission_denied`。协作者撤销跳过作者 `enqueue_memory`，这是正确的：该函数只认作者，派生任务不能归给作者，也不能花作者的费用。
- MCP：映射、规格、resolve、`_external_call` 四处都对协作轮次关闭。

修复：
1. **编辑者同轮压缩后主发送必然失败**（`agent/context.py`）：压缩成功后只用 `set_committed_value` 更新了内存中的 `summary` 和 `summarized_until`，没有更新 `summary_source`。随后 `guard_agent_provider` 用同一个会话对象重建期望值，期望来源为 None，而库中已有协作标记，`_check_summary_execution` 因此拒绝，任务 failed。作者两边都是 None，不受影响。修复：同步设置 `summary_source`。新增 `test_editor_turn_with_compaction_sends_main_round_after_marked_summary`（SQLite）和 `test_postgresql_editor_turn_with_compaction_keeps_marked_summary`（PG，授权回退作者模型）。负对照：删去修复后两例都失败；还原后用 `shasum -a 256 -c` 校验为 OK（`/tmp/e4ib_neg_ctx_*`）。原有摘要用例直接调用 `build_messages`，覆盖不到这条路径。
2. **0053 降级有损**（`alembic/versions/0053_agent_summary_source.py`）：原实现直接删列，再升级后编辑者摘要会永久无法验证，这与 0052、0029 等迁移"有新语义数据时拒绝有损降级"的约定不一致。现改为先取写锁（SQLite 写 `alembic_version`，PG `ACCESS EXCLUSIVE`），扫描到非空标记则拒绝降级。迁移用例补充了"有标记拒绝、清空后可降级"。负对照：恢复直接删列后用例失败，还原后校验为 OK（`/tmp/e4ib_neg_mig_*`）。

遗留项判断：引擎 `_event_task` 对 outline/entity/wizard 等 grant 执行体，先由 `require_write_execution` 取发送写门禁，再在 `verify_event → guard.require_task_write` 中锁作者 User。这一点属实，但**不构成永久死锁**：门禁的独占和共享获取都是 `pg_try_advisory_xact_lock*` 轮询，上限 2 秒。作者 lease 轮询超时后返回 `ai_visibility_busy` 并回滚，释放 User 共享锁，编辑者随即继续。实际影响是作者偶发 409 和最多 2 秒的延迟。Agent 自身的 `verify_event` 不锁作者 User 行，不受影响。建议单独立项：从 `current.input["_project_model"]["project_owner_id"]` 取作者传入 `require_write_execution`，并补双连接回归。这会改动所有协作 handler 的事件提交路径，不属于"显然安全"的小改，本次未动。

规范：`backend/agent.md` 补充了内存来源同步和降级拒绝；`database-guidelines.md` 新增 0053 条目。

回归（最终代码）：
- SQLite（按文件，最多 3 路并行）：handler_e4ib 26、access_e4ia 33（6 skip）、audit_integration 8、audit_coordinator 19（3 skip）、context_execution_identity 18、context 7、summary_task_token_identity 7、project_style_privacy 35、context_model_binding 5、context_actor_version 9、runtime 20、migrations 27（1 skip）。
- PG（按文件顺序执行，未与 SQLite 并行）：lock_order_e4ib 3、context_execution_identity_postgresql 2、summary_task_token_identity_postgresql 1、access_e4ia 的 PG 用例 6、audit_coordinator 的 PG 用例 3。
- `ruff check .` 通过。

## 进度：E4i-c（2026-11-14，工具迁移与检索；只改后端，协作者 HTTP 入口仍关闭，无新迁移）

实现：
- **工具改走协作者分支**（`agent/tools/base.py`、`read.py`、`write.py`）：新增 `collaborative(ctx, flag)`，`ToolCtx.grant` 非空时给 service 传 `manual=True`（story 为 `manage=True`），作者轮次不传参数，默认 helper 及其测试不变。
  - 读：字段定义 / 条目目录 / 条目字段、角色列表与详情（`get_collaborative`）、任职 / 成员关系、组织、关系、职业、世界观、大纲树、章节列表（`list_manual_chapters` + `project_states(manual)`）、正文（`get_manual_chapter(content.read)`）、章节分析（`get_analysis(manual)`）、伏笔、时间线。读工具不单独加锁重验（裁决 9），结果写入前仍由 `_lock_execution` 复核。
  - 写：CRUD（角色 / 世界观 / 大纲走 `manage`，组织 / 关系 / 职业 / 伏笔走 `manual`）、共享字段起始值 / 变化、基础设定 / 变化、成员设置 / 移除 / 删除成员资料、任职设置 / 移除、细纲整体同步、章节标题。`update_project` 原已是 `require_project(content.edit)`。
  - 正文 `replace_chapter_text` 不调 service 的项目检查，只依赖 `_lock_write`；`generating` 与 `ensure_outside_batch` 互斥对编辑者同样生效（编辑者自己的单章占用也使章节处于 generating），无需改动。
  - 协作者分支本身只要求 `content.read/edit`（管理员人工来源也会放行），所以写入能力完全由先于 service 的 `_lock_write` → `lock_agent_write`（同一事务锁后，原轮次 accepted editor + `ai.use`）决定，确认路径（`visibility_locked`）同样复核。逐个核对 `manual=True` 的副作用：`update_baseline` 的 `actor_user_id` 只用于系列冲突错误按 actor 投影（更安全）；`sync_plans` 的 bypass / 门禁判断在 Agent 已持门禁时只是重入；`lock_manual_project_visibility` 的 SQLite busy_timeout 调整在已持门禁时无副作用；审计动作名与人工路由一致（管理员兼编辑者按裁决 6 记管理审计）。
- **检索（裁决 12）**：
  - `handlers/agent.py::_freeze_shared_embedding`：协作轮次在受理事务 `resolve_shared_embedding(for_update=True)` 冻结 `_shared_embedding`（未授权 / 作者未配置 → keyword；作者配置本地不可用的 400 → 不冻结 = 只用关键词）；作者轮次不冻结（行为不变）。重试沿用原冻结并锁内 `revalidate_shared_embedding`；`build_job` 校验 Task 行与领取输入中的冻结一致，向量模式下核对能构成 `for_task` guard。
  - `agent/access.py::frozen_shared_embedding / text_guard_extras`：任务行冻结了选择后，`collaboration_ai_guard` 要求同一执行的文本 guard 必须带上它（否则拒绝），故 `context._collaboration_summary_guard` 与 `build_job` 的文本 `for_task` 都以 `extra_bindings` 带上冻结选择。
  - `tools/read.py::_collaborator_embedder`：runner 给 `ToolCtx` 传当前已证明执行身份的取值函数与冻结文本绑定；向量模式以 `for_task(execution, selection, extra_bindings=(text_binding,))` 先短事务 `require_task_write` + `load_bound_shared_embedding_config`，再 `prepare_shared_embedding`（普通构造故障锁内复核未变 → 关键词，否则失败关闭）；每次查询嵌入在发送 lease 内重验。provider 普通错误由 `embed_query` 降级关键词，协作者只看到 `public_search_warnings` 的固定文案；`VisibilityPolicyError` 上抛终止轮次。keyword / 未冻结 / 缺执行身份（确认路径）一律关键词，零作者向量调用，协作者本人向量配置永不参与。
- **start_task（裁决 5）**：白名单不变。预览 `ai_config_id` 只在配置属于 actor 时给出，作者回退时为 null 并加费用提示（预览会进入模型上下文与本人面板）。`execute_admitted` 额外核对派生 Task 的 `_collaboration_grant` 等于本轮 grant，不一致抛 `AgentAuthorizationChangedError` 整体回滚（不能把已插入任务当普通失败提交）。派生任务归编辑者、模型按本人优先 / 授权回退由中央受理决定。
- **裁决 17**（`agent/service.py`）：`restored_memory_ids` 取撤销效果中 `before` 非空的 `story_memories` 行；协作者撤销提交后经 `kb_index_service.enqueue_collaborator_memory` 只为这些条目建作者向量任务（归编辑者 + 原邀请轮次；未授权 / 作者无向量 / 已有向量不建；失败只记日志）。正向 Agent 工具只会删除或改章号记忆（`on_chapters_deleted` / `on_chapter_renumbered`），不产生需要新向量的文本，所以正向写入不派生。
- 规范：`backend/agent.md` 新增「协作轮次的工具与检索（E4i-c）」条目。

测试：
- 新增 `tests/test_agent_tools_e4ic.py`（SQLite 26）：
  - 18 个读工具对原轮次编辑者成功，其中 8 个原严格作者读取以 grant=None 调用仍返回「不存在」（证明默认 helper 未改）。
  - 16 个写工具 × {未加锁, visibility_locked}，预览与执行均成功，库内结果核对。
  - 非原轮次编辑者 6 种来源（管理员兼 viewer、管理员撤权、管理员角色往返旧 grant、降级 viewer、撤权、重邀后旧轮次）× 2 种锁态 × 4 个写工具 × {预览, 执行}：一律 `AgentAuthorizationChangedError`，作品不变。
  - 检索（真实轮次）：授权编辑者用作者向量（冻结 vector、查询嵌入仅 `owner-embed`、hybrid）；未授权编辑者冻结 keyword、零嵌入调用、带固定提示；作者向量配置损坏时不冻结、零调用；提供方普通错误降级关键词且不回显错误原文；发送门禁前撤权 / 关闭作者授权 → 轮次失败、零查询嵌入、不写检索结果；作者轮次不冻结、仍用本人向量。
  - start_task：作者回退时预览 `ai_config_id` 为 null 且有费用提示；模拟 E4i-d 确认入口走真实确认链后，派生任务归编辑者、绑定本轮 grant、`_project_model.source=owner` 且配置为作者配置。
  - 裁决 17：撤销恢复记忆后只建一条编辑者的 `kb.index` 记忆任务（`_manual_memory_ref_ids == [该条]`、原 grant、vector 选择），不建作者任务；未授权编辑者不建任务；`restored_memory_ids` 单元。
- 新增 `tests/test_agent_tools_e4ic_postgresql.py`（PG 2）：编辑者 `create_character` 与 `update_chapter_meta`（locked）写入成功，降级 viewer 后旧 grant 两种锁态终止、作品不变；授权编辑者检索冻结 vector 用作者向量（hybrid），作者关闭授权后新轮次冻结 keyword、零嵌入调用。

负对照（每次从 /tmp 备份还原后 `shasum -a 256 -c` 均 OK）：
1. 管理员来源并入（`lock_agent_write` 对 `can_edit_content` 直接放行，`/tmp/e4ic_neg_admin_eCPn`）→ 8 失败（admin_viewer / admin_revoked / admin_stale_grant / old_round × 2 锁态）。
2. 未授权编辑者检索使用作者向量（协作分支改为作者默认 `resolve_embedder`，`/tmp/e4ic_neg_vector_fvSk`）→ 4 失败（未授权关键词、作者配置损坏、撤权 / 关闭授权两例）。
3. 附加（`/tmp/e4ic_neg_preview_KzG7`）：预览回填作者配置 ID、协作者撤销不补建记忆向量 → 对应 2 例失败。

回归（最终代码）：
- SQLite（按文件，最多 3 路并行），全部通过：Agent 全部非 PG 文件 38 个（新增 tools_e4ic 26；access_e4ia 33/6 skip、action_identity 12、api 11、audit_coordinator 19/3 skip、audit_integration 8、authenticated_actor 18/8 skip/1 xfail、auto_external_fencing 58、change_identity_migration 8、context 7、context_actor_version 9、context_execution_identity 18、context_model_binding 5、conversation_final_snapshot 5、derived_chapter_admission 8、derived_task_admission 11、effects 29、effects_claim_identity 6、external_deletion_guard 39、handler_e4ib 26、mcp_send_lease 24、mcp_skills 21/6 xfail、model_admission 3、model_value_privacy 3、preview_guard_privacy 4、project_style_privacy 35、providers 59、recovery_claim_identity 30、retry_execution_fencing 9、runtime 20、stale_change_identity 3、summary_task_token_identity 7、task_actor_binding 9、tools 54、turn_identity_runner 17、unknown_outcome 14、visibility 8）；哨兵 collaboration_memory_write_e6 20、progression_agent_effects 12、scene_agent 14、setting_category_agent 68/1 skip、setting_field_agent 34、skill_inputs 15、task_agent_evidence 11。
- PG（按文件顺序，未与 SQLite 并行，无 skip）：tools_e4ic_postgresql 2、lock_order_e4ib 3、access_e4ia 的 PG 用例 6、audit_coordinator 的 PG 用例 3、context_execution_identity 2、summary_task_token_identity 1、turn_identity 2、project_style_privacy 4。
- `ruff check .` 通过。未跑全量（精简回归策略）。

偏差：
- 检索未直接复用 `kb_service.search_access`：它基于 `for_http`（HTTP 认证会话），且会对传入 session `rollback`，不适合 runner 的根事务；改按大纲 / 实体任务的同口径（受理冻结 + `for_task` + `prepare_shared_embedding`），只复用 `public_search_warnings` 与 `KEYWORD_ONLY_*` 文案。
- 冻结为 vector 后整轮依赖作者模型授权（`task_requires_owner_ai` 的既有语义，与大纲 / 实体一致）：作者关闭授权时，即使编辑者用本人文本模型，该轮也会失败关闭（不是只降级检索）。
- 裁决 17 只在撤销路径落地；正向 Agent 写入不会产生需要新向量的记忆文本（见上），所以未在 runner 自动执行 / 确认路径加派生。若将来新增会改写记忆正文的工具，需要在提交后按同一 helper 派生。
- 记忆撤销测试只把 `effects.undo` / `preflight_effect_versions` 换成桩（效果还原本身由 effects 测试覆盖），锁、能力判定、提交与派生入队均为真实路径。
- start_task 派生任务的端到端确认使用了测试内模拟的 E4i-d 入口（同 `test_agent_audit_integration`）；真实入口仍对协作者 404。

待确认 / 遗留：
- 作者向量配置本地不可用时，协作轮次照常受理、只用关键词（与向导一致，大纲 / 实体是 400）。是否需要与裁决 20 ①统一为拒绝，请确认。
- E4i-d：确认 / 拒绝入口翻转时仍需按 E4i-a 遗留处理 reject 路径的锁内 `ai.use` + grant 判定；本片对此无改动。
- E4i-e：冻结 vector 的 paused 轮次在作者关闭授权后会由受限收尾判失败（依赖 `task_requires_owner_ai`），待 E4i-e 纳入 `agent.turn` 时一并验证。

## 复核：E4i-c（2026-11-14，check agent）

逐项结论：
1. **协作者分支与写锁**：所有写工具（含预览、自动执行、确认路径）只经 `Tool.preview` / `Tool.execute`，二者在调用 `operation` 前都执行 `_lock_write` → `lock_agent_write`（同一事务；`visibility_locked` 只省发送写门禁、不省能力复核）；`execute_change` 先 `lock_agent_write(grant)` 再 `tool.preview`，`start_task` 的 `execute_admitted` 位于其后。未发现绕过 `_lock_write` 直达 `manual=True` 写入的路径（`run` 对非 read 直接拒绝；`write_text`、`_shared_field_target` 不经 service 项目检查，同样只依赖 `_lock_write`）。service 二次 `require_project(content.edit, visibility_write=True)` 为同序重入；SQLite 忙时其 `db.rollback()` 落在 `TransactionSession` 上为空操作。副作用：`update_chapter(manual)` 只改标题，不写版本；审计仍由 `AuditOperation` 规则决定（管理员兼编辑者记管理审计，裁决 6）；无通知副作用。
2. **读工具隐私**：**发现并修复**——协作轮次 `list_chapters` / `read_chapter` 直接 `ChapterBrief.model_validate(row)`，把作者私人章节风格 `style_id`（及失效的大纲 / 细纲关联）交给编辑者和其模型；人工章节接口用 `manual_chapter_views` 隐藏。新增 `read.chapter_briefs`：协作轮次走 `manual_chapter_views(full=False)`，作者轮次不变。其余读工具：概览经 `project_for_model`（风格 / 阅读进度按 actor）、分析经 `get_analysis(manual)`（任务引用按 actor 过滤）、任务 / 风格 / Skill 按本人；8 个原严格作者读取在 grant=None 下仍为「不存在」。
3. **检索冻结**：模式与裁决一致（授权+可用 → vector；未授权 / 未配置 → keyword；本地不可用 → 不冻结、运行时关键词并给固定提示）；未授权零作者向量调用，本人向量配置永不参与；每次查询嵌入经 `prepare_provider` 的 `guard_provider` lease（for_task）重验；普通错误由 `embed_query` 降级且 `public_search_warnings` 只给固定文案；`VisibilityPolicyError` 上抛。**加固**：`_collaborator_embedder` 锁内 `load_bound_shared_embedding_config` 抛出的非策略类 `AppError`（授权 / 配置变化、受理后本地不可用）原先会被 `Tool.run` 当普通工具错误回给模型并继续，现转为 `CollaborationSendBlocked` 终止轮次（E6 裁决 11 / 20②）。
   - 偏差判断（冻结 vector 后整轮依赖作者模型授权）：与大纲 / 实体一致，不违反裁决 1（裁决 1 管受理，不管运行中收紧）。且实测作者对成员的任何 PATCH（含 `allow_owner_ai` 开 / 关、改角色）都会递增 `revision` → 原 grant 失配，编辑者进行中的轮次无论检索模式、无论用谁的文本模型都会终止（探针：未授权 keyword 轮次中作者**开启**授权 → `failed 协作授权已失效`）。所以该偏差在授权开关上没有额外影响；额外影响只剩「作者改 / 损坏向量配置会终止冻结 vector 的编辑者本人模型轮次」，符合裁决 20②。建议维持。
4. **start_task**：白名单 10 类未变；派生任务 `user_id` = 编辑者，`_collaboration_grant` 与本轮 grant 不符抛 `AgentAuthorizationChangedError`，经 `_execute_admitted_task` 上抛整体回滚；预览 `ai_config_id` 仅本人配置时给出（`request` 取自工具参数，引用校验只允许本人配置，不含作者配置 ID）。
5. **记忆撤销（裁决 17）**：只在 `grant is not None` 时以原 grant、编辑者身份为恢复条目建单条 `kb.index`，不建作者全书任务；授权 / 无向量 / 已有向量时不建。核对正向写：Agent 工具可达的记忆写只有 `on_chapters_deleted`（经 `sync_plans` 删章）与 `on_chapter_renumbered`（只改章号与检索单元标志），不产生需新向量的文本，结论成立。
6. **作者路径**：`collaborative()` 对 grant=None 返回空参数；检索作者分支保持 `resolve_embedder`；`chapter_briefs` 作者分支逐字等价原输出。

实现者问题（作者向量本地不可用时协作轮次是拒绝还是只用关键词）：**建议维持受理 + 仅关键词**。理由：Agent 的主体是对话，检索只是可选工具；裁决 1 允许编辑者用本人模型开 Agent，拒绝会让编辑者因作者侧、自己无法修复的配置问题完全不能用 Agent；作者本人轮次在同情形也是降级关键词（`resolve_embedder`）；裁决 20① 已明确不要求统一，向导即为同类先例；编辑者只看到固定提示，不暴露配置。

修复文件：`backend/app/services/agent/tools/read.py`（`chapter_briefs`、`_collaborator_embedder` 锁内失败关闭）。
新增测试（`tests/test_agent_tools_e4ic.py`，+2）：`test_editor_chapter_reads_hide_owner_private_style_id`、`test_frozen_vector_selection_failure_inside_lock_fails_closed`。
负对照（`/tmp/e4ic_check_neg_xmrm` 备份，还原后 `shasum -a 256 -c` OK）：去掉协作投影 / 去掉锁内失败转换 → 两例均失败。

回归（最终代码）：SQLite（按文件，≤3 路并行）tools_e4ic 28、tools 54、project_style_privacy 35、runtime 20、preview_guard_privacy 4、access_e4ia 33/6 skip、handler_e4ib 26、audit_integration 8、audit_coordinator 19/3 skip；PG（顺序、未与 SQLite 并行、无 skip）tools_e4ic_postgresql 2、access_e4ia PG 6、audit_coordinator PG 3。`ruff check .` 通过。

遗留（供后续切片）：
- E4i-e：resume（paused → queued）对冻结选择的锁内重验尚未覆盖，随 `agent.turn` 受限收尾一并验证。
- 成员任意 PATCH 即终止进行中轮次属既有 grant 语义，前端（E4i-f）需在作者修改授权时提示「会中止该成员进行中的 AI 任务」。

## 进度：E4i-b2（2026-11-14，引擎事件 / 终态提交锁序，裁决 18；只改后端，无迁移，协作者 HTTP 入口不变）

实现：
- `task_engine/execution_identity.py` 新增 `frozen_project_owner_id(identity)`：作者执行（无 grant）返回 None；协作执行只从领取快照 `input_json` 中服务端写入的 `_project_model` / `_shared_embedding` 取 `project_owner_id`（严格正整数），两者都有时必须一致，作者不能等于 actor；缺绑定、类型破损或不一致抛 `task_execution_changed`（失败关闭）。不读当前数据库：避免在 RR 首语句前读作品，作者一致性仍由锁后核对兜底。
- `task_engine/engine.py`：
  - `_event_task`：普通分支改为 `require_write_execution(db, current, project_owner_id=owner_id)`（E4i-b 已支持：gate → actor/作者 User 升序 → 发送写门禁 → 作品 → Task，锁后核对 `project.user_id`）。单章 done/paused 分支的 `require_authenticated` 同样传 `targets=(owner,)`，并在 `lock_active` 后核对作品作者。`_flush` 经 `_event_task`，一并覆盖。
  - `complete_state`：仅当 `status in (succeeded, paused)`、handler `execution_identity_aware`、非系统清理（即随后会调 `verify_finalization`）时，`require_authenticated` 传作者 target。失败/取消/中断终态不调用 handler，锁集合保持原样（首版对所有终态预锁时，3 个用合成 grant 任务（无模型绑定）的撤权用例错误文案变为「任务执行已停止」，据此收窄）。
- 审计其他 engine 取门禁的路径：领取 `_claim_and_run`、`_fail_reset_queued_chapter`、`_fail_collaboration_paused` 已是 gate → `lock_users(actor, owner 升序)` → 门禁；`stream_lease._task_frame_lease` 是单 User FOR SHARE + 共享门禁 + 配置 advisory（PG 不锁 User 行），不构成反转；`_complete_from_*_receipt` 不取认证门禁。无需修改。
- 作者任务：`frozen_project_owner_id` 返回 None，`_event_task` / `complete_state` 与原调用逐字等价。
- 规范：`backend/agent.md`（User 锁序条目追加 E4i-b2）、`backend/error-handling.md`（协作者执行 E4 追加锁序条目）。

测试：
- 新增 `tests/test_engine_event_lock_order_e4ib2_postgresql.py`（PG 2 例，参数 event / final）：真实 outline.continue 协作任务在第二实例运行，在 handler `verify_event` / `verify_finalization` 入口（引擎已持门禁、handler 尚未锁作者）挂起；作者经第一实例走真实发送 lease 顺序（`require_authenticated_for_send` → 共享门禁）。只用 Event 与 `pg_blocking_pids` 编排，无 sleep。断言作者在作者 User 行上被引擎事务阻塞、放行后作者 lease 成功、任务 succeeded 且草稿/费用来源正确。
- 新增 `tests/test_engine_event_lock_order_e4ib2.py`（SQLite 3 例）：按 session 记录 `lock_users` 与发送写门禁（含引擎模块级导入的门禁），在 handler 事件/终态核验处切段：编辑者任务门禁前最后一次 User 锁为 `(owner, editor)` 升序，handler 内 User 锁 ⊆ 已锁集合；作者任务门禁前只锁本人。另有 `frozen_project_owner_id` 单元用例（作者 None、双绑定一致、缺失/不一致/等于 actor/bool/字符串/null 失败关闭）。

负对照（每次还原后 `shasum -a 256 -c` 均 OK）：
1. `/tmp/e4ib2_neg_engine_E0jlJs`：`_event_task` 与 `complete_state` 均改 `owner_id = None` → SQLite `[editor]` 失败；PG `[event]`、`[final]` 均失败，作者得到 `VisibilityBusyError`。
2. `/tmp/e4ib2_neg_final_oFqnUe`：只去掉 `complete_state` 预锁 → PG `[final]` 失败、`[event]` 通过（证明两阶段各自独立覆盖）。
3. `/tmp/e4ib2_neg_final2_l77xRY`（收窄后最终代码）：同 1，SQLite `[editor]`、PG 两例均失败。
4. 基线 `/tmp/e4ib2_baseline_*`：去掉预锁时 `test_collaboration_task_revocation_e4.py` 22 例通过，确认首版 3 例失败由本片引起，已收窄修复。

回归（最终代码）：
- SQLite（按文件，最多 3 路并行），全部通过：engine_event_lock_order_e4ib2 3、agent_handler_e4ib 26、collaboration_outline_ai 41、outline_entry_e4g3 62、entity_entry_e4g3b 69、entity_ai_execution_identity 52、entity_tasks 6、wizard_entry_e4g3c 95、wizard_collaboration_atomicity 8、stream_wizard_e5c3 60、wizard_execution_identity 20、kb_index_guarded_e6b 41、kb_editor_entries_e6c 27、kb_index_admission_e6a 26、bound_memory_index_e6 17、analysis_entry_e4g 55、analysis_execution 128、chapter_execution 35、chapter_entry_e4f 41、chapter_receipt_identity 10、batch_entry_e4g2 59、task_execution_identity 21、task_execution_fencing 11、task_event_guard 6、task_execution_send_guard 23、task_abort_identity 22、task_receipt_deletion_guard 83、task_sse_engine_http 1、tasks 32、task_revocation_e4 22。
- PG（按文件顺序，未与 SQLite 并行，无 skip），全部通过：engine_event_lock_order_e4ib2 2、agent_lock_order_e4ib 3、outline_entry_e4g3 4、outline_ai 1、entity_entry_e4g3b 4、entity_ai_execution 6、wizard_entry_e4g3c 5、kb_index_guarded_e6b 3、kb_editor_entries_e6c 4、bound_memory_index_e6 2、analysis_entry_e4g 5、analysis_execution 21、chapter_execution 11、batch_entry_e4g2 6、task_execution_fencing 1、task_execution_identity 7、task_revocation_e4 2。
- `ruff check .` 通过。未跑全量（精简回归策略）。

偏差 / 待确认：
- 作者 ID 取自领取快照而非锁前读库；协作任务若无任何服务端模型/向量绑定，事件提交现在直接失败关闭（生产受理总会写绑定；仅合成测试数据会这样）。
- Agent 的事件提交现在也预锁作者 User（Agent `verify_event` 本身不锁作者）：多持一把 User 锁到事件事务结束，作者 lease 可能在 User 行上排队到该短事务提交，不再出现 busy；属预期的统一顺序。
- 只用 outline 作为 PG 代表 handler；entity/wizard/kb/analysis/chapter/batch 共用同一引擎入口，以其原有 SQLite/PG 文件回归，未逐个补交叉等待用例。

## 复核：E4i-b2（2026-11-14，check agent）

结论：实现符合裁决 16/18，未发现需要改动的生产代码；补充一个 PG 交叉等待 handler 类型。

1. 引擎取门禁路径逐一核对：领取 `_claim_and_run`（gate → `lock_users(actor, owner)` → 门禁）、`_fail_reset_queued_chapter`、`_fail_collaboration_paused` 已是升序预锁；`_event_task` 普通 / 单章 done·paused 分支与 `_flush`（经 `_event_task`）现均预锁；`complete_state` 仅在 succeeded/paused 且会调 `verify_finalization` 时预锁。failed/cancelled/interrupted 终态在门禁后只调 `release_uncertain_attempt` / `_require_resume_receipt` / `discard_pending`，均不锁 User，收窄成立。`on_abort` 在 `complete_state` 之外由 handler 自行开事务，不属本片。重试/续跑经 `create_task` 重新受理，执行期仍走同一 `_event_task`。作者任务 `frozen_project_owner_id` 返回 None，调用与原逐字等价。
2. 快照取作者：生产中 `Project.user_id` 不存在任何改写路径（无作品转让功能），即使出现，`require_write_execution` / 单章分支的锁后核对与 handler 守卫的绑定核对都会失败关闭，只会多锁旧作者行、不会静默写入。**关键问题核对**：所有带 `_collaboration_grant` 的 Task 写入点——`task_service.create_task`（模型受理必写 `_project_model`，其 `project_owner_id` 与 `source` 无关，编辑者用本人模型 + 仅关键词检索的 Agent/向导/实体/大纲/章节/批量亦然）、`kb_index_service` 受理（协作者强制 `require_vector`，必写 `_shared_embedding`）、分析派生记忆子任务（写 `_shared_embedding`）、分析自动派生（两份绑定齐全）——均至少带一份绑定；无 grant 的 `analysis_service:920` / `kb_index_service:864` 是作者路径。故不存在“合法协作任务缺绑定被误判失败”或“跳过预锁而 handler 仍锁作者”的情况。
3. handler 内 `require_write_execution(db, execution)`（不传作者）只用于构建前短屏障并立即回滚，或在终态事务内重入（wizard `verify_finalization`），本身不锁作者 User；之后的 `guard.require_task_write` 锁作者已被引擎预锁覆盖。
4. Agent 事件提交多锁作者 User：作者 lease 只在该短事务期间于 User 行排队，与 handler 守卫 `require_task_write` 一直采用的顺序一致，符合裁决 16；反向（引擎等作者 lease）原本即在门禁上等待，未新增无界等待来源。接受。
5. PG 用例无 sleep（Event + `pg_blocking_pids` 轮询）。按建议把 `test_engine_event_lock_order_e4ib2_postgresql.py` 扩为 kind × stage（新增 `character.generate`），共 4 例。

负对照：`/tmp/e4ib2_check_neg_oEYVjO`（`_event_task` 与 `complete_state` 的 owner 置 None）→ PG 4 例全部失败；还原后 `shasum -a 256 -c` OK。

回归（最终代码）：SQLite（按文件，≤3 并行）e4ib2 3、agent_handler_e4ib 26、task_revocation_e4 22、outline_entry_e4g3 62、entity_entry_e4g3b 69、wizard_entry_e4g3c 95、analysis_entry_e4g 55、chapter_entry_e4f 41、batch_entry_e4g2 59、kb_editor_entries_e6c 27，全部通过。PG（顺序，无 skip）e4ib2 4、agent_lock_order_e4ib 3、task_revocation_e4 2、outline_entry_e4g3 4、entity_entry_e4g3b 4、wizard_entry_e4g3c 5，全部通过。`ruff check .` 通过。

遗留建议：handler `on_abort` 路径（非引擎）若将来在门禁后锁作者 User，应同样预锁；当前未发现此类调用。

## 进度：E4i-d（2026-11-14，Agent HTTP 入口对协作者开放；只改后端，新增迁移 0054，前端留给 E4i-f）

实现：
- **入口能力（`agent/access.py`）**：新增 `lock_agent_entry(lock=…)`（私人作用域：作者或 accepted 成员含 viewer；仅管理员人工来源、已撤权、陌生人 404）、
  `check_agent_write`（与 `lock_agent_write` 同一判定：作者且 grant 为 None，或 accepted editor + `ai.use` + 原 `(invitation_id, revision)`；管理员 `projects.edit` 不并入）、
  `require_conversation_write`（会话级写入：当前轮次写能力 + 会话属于当前邀请轮次）、`deny_changed`。`lock_agent_write` 改为复用 `check_agent_write`，语义不变。
- **`agent/service.py` 各入口**：
  - 读：`list_conversations` / `get_conversation`（`owned_conversation(lock=False)`）/ 生成日志对作者与 accepted 成员（含 viewer）开放本人会话；会话恒按创建者过滤。
    `_prepare_committed_changes` 的投影证明统一 `owner_only=False`。`get_conversation` 对本人已不可见的活动旧轮次（viewer / 旧 grant）不附带 `active_task`。
  - 新建会话：editor（当前轮次）写入 `collaboration_invitation_id`；viewer 403 `project_readonly`（与其它 AI 入口一致）。
  - 改名 / 删除 / 发送：`owned_conversation(lock=True)` 做会话级写判定，失败 409 `permission_denied`（裁决 14）；发送另在锁外初检（模型预检前即 409）；
    受理提交后的复核按本轮 Task 的 grant（`check_agent_write`）。协作者会话 `mcp_plugin_ids` 非空 → 403 `collaborator_mcp_unavailable`（裁决 2）。
  - 确认 / **拒绝** / 撤销 / 整轮确认 / 整轮拒绝 / 整轮撤销：按提案所属轮次 Task 的 grant（`_require_change_write`、`_turn(lock=True)`），锁外初检 + 入口锁内重验，
    且**先于**提案投影核对（E4i-a 遗留：降级后错误码由 `agent_change_changed` 统一为 `permission_denied`）。拒绝不经 `execute_change`，由此自行判定（裁决 15）。
    `_turn` 改按本人真实归属读取轮次（不经活动成员可见性谓词），本人旧轮次授权失效给 409 而非 404。`maybe_resume` 按原轮次 grant 复核（回滚前先取 grant）。
    整轮撤销无可撤项时以轮次 grant 复核（否则空批次被当成作者轮次）。
  - `execute_change`：带 grant 的 external 提案直接记失败（不发网、不建未知屏障）。
- **MCP（第 4 项）**：`mcp_tools._send_lease` 保留作品作者条件，另拒绝带 `_collaboration_grant` 的任务；编辑者无法产生 external 提案，故不会触发
  `has_unsettled_external_change` 阻挡作者删除作品。
- **派生任务 / 模型来源（第 5 项）**：`execute_admitted` 经真实入口端到端：派生任务归编辑者、绑定当前邀请轮次；授权回退时 `_project_model.source=owner`、
  配置为作者（费用来源作者），未授权编辑者用本人配置。检索按 E4i-c 冻结口径（裁决 12），本片未改。
- **迁移 0054**（父 0053）：`agent_conversations.collaboration_invitation_id` 可空 String(36)，原位加列；存在非空标记时拒绝降级（先取写锁再扫描）。不进对外输出与单书备份。
- **第 8 项锁序扫描**：各 handler `on_abort`（analysis/entities/outline/wizard/agent/book_import 为空操作；chapter/batch 经 `release_chapter_claim`、kb 经
  `kb_index_service.on_abort`）只取作品父行锁，不取发送写门禁也不锁 User，无反转。handler 内未传作者的 `require_write_execution`（wizard/outline/entities/
  chapter/batch/kb/retrieval/book_import 等 20 处）改为由 `require_write_execution` 对协作执行默认按 `frozen_project_owner_id` 预锁作者 User（快照缺绑定则退回原锁集合，
  正确性仍由锁后核对与 handler 守卫失败关闭），使其后 `guard.require_task_write` 的作者 User 锁统一为重入。
- 规范：`backend/agent.md` 新增「协作者 HTTP 入口（E4i-d）」并改写 HTTP 入口条目；`database-guidelines.md` 新增 0054。

测试：
- 新增 `tests/test_agent_entries_e4id.py`（SQLite 16）：
  - `test_agent_entry_role_matrix` × 9 身份（作者、授权编辑者、未授权编辑者、viewer、管理员兼 viewer、管理员兼编辑者、已撤权、撤权后重邀、陌生人）。
    设置阶段经真实 HTTP：建会话 → 发送（FakeLLM 3 个 `update_project` 提案）→ 确认第一项；失败会话；空会话；再施加降级/撤权/重邀。
    可写身份：拒绝、撤销、整轮确认（真实续跑成功）、整轮撤销、通用重试 → 整轮拒绝 → 续跑、发送、改名、MCP 拒绝、日志、取消、删除；断言作品回到原值、
    所有轮次归真实触发者、协作轮次绑定当前邀请轮次与本人优先 / 授权回退模型（真实 api_key 只出现期望的一把）、会话轮次标记，管理员兼编辑者有管理审计、其余无。
    只读身份：列表 / 详情 / 日志 200；改名 / 删除 / 发送 / 确认 / 拒绝 / 整轮确认 / 整轮拒绝 / 撤销 / 整轮撤销 409 `permission_denied`；取消 404；重试 viewer 403、重邀 400；
    新建会话 viewer 403、重邀者 201 并可在新轮次发送。不可见身份全部 404。每类都核对提案 / 任务 / 会话 / 作品快照不变、零模型调用、管理审计数不变。
  - 管理员兼编辑者确认写入记 `project.update` success 一条（裁决 6）；派生 `start_task` 两种费用来源；会话互相私有（作者 / 两名编辑者）；协作者订阅 404、作者 200（裁决 10）；
    `require_write_execution` 默认预锁（frozen → `(owner, editor)` 先于发送门禁；missing → 只锁本人）；0054 往返。
- 新增 `tests/test_agent_entries_e4id_postgresql.py`（PG 4）：编辑者发送 → 工具提案 → 确认（续跑成功）→ 撤销端到端；拒绝与撤权并发：撤权 / 降级先提交（Event 停在入口写锁前）
  → 404 / 409 且提案 proposed；拒绝先持锁（第二应用实例撤权，`pg_blocking_pids` 观测被阻塞）→ 拒绝已提交、撤权随后完成。
- 调整（入口开放后的预期翻转，断言未放宽）：`test_agent_access_e4ia.py`（入口锁用例改为“原 grant 放行、无 grant 409、他人提案 404”；删除被矩阵取代的
  “HTTP 写入口仍 404”；去掉入口模拟，`committed` 降级也断言 `permission_denied`）；`test_agent_audit_integration.py`、`test_agent_tools_e4ic.py` 去掉入口 / 投影 monkeypatch
  改走真实入口；`test_agent_authenticated_actor.py` 拒绝路径的拦截点改为 `lock_agent_entry(lock=True)`；`test_collaboration_log_private_views.py` viewer 本人会话详情 404→200、
  发送 404→409 `permission_denied`，撤权后详情 404。

负对照（每次从 /tmp 备份还原后 `shasum -a 256 -c` 均 OK）：
1. 拒绝不做 grant 判定（`/tmp/e4id_neg_reject_5ABB`：`_require_change_write` 与 `_turn` 不检查）→ 矩阵 viewer / admin_viewer / reinvited 3 例失败，PG `concurrent_revocation[viewer]` 失败。
2. viewer 可写会话（`/tmp/e4id_neg_viewer_send_AbAe`：`require_conversation_write` 去掉写能力判定）→ viewer / admin_viewer 失败。
3. 仅发送路径跳过会话写判定（`/tmp/e4id_neg_send_only_hByp`）→ viewer / admin_viewer 发送得到中央 403；**重邀者向旧会话发送得到 202**——证明轮次标记是必要的（中央受理不拦旧会话）。
4. 去掉 `require_write_execution` 默认预锁（`/tmp/e4id_neg_prelock_1TQm`）→ `prelocks...[frozen]` 失败。

回归（最终代码）：
- SQLite（按文件，≤3 并行）全部通过：Agent 全部非 PG 文件 38 个（entries_e4id 16、access_e4ia 31/6 skip、action_identity 12、api 11、audit_coordinator 19/3 skip、audit_integration 8、
  authenticated_actor 18/8 skip/1 xfail、auto_external_fencing 58、change_identity_migration 8、context 7、context_actor_version 9、context_execution_identity 18、context_model_binding 5、
  conversation_final_snapshot 5、derived_chapter_admission 8、derived_task_admission 11、effects 29、effects_claim_identity 6、external_deletion_guard 39、handler_e4ib 26、mcp_send_lease 24、
  mcp_skills 21/6 xfail、model_admission 3、model_value_privacy 3、preview_guard_privacy 4、project_style_privacy 35、providers 59、recovery_claim_identity 30、retry_execution_fencing 9、
  runtime 20、stale_change_identity 3、summary_task_token_identity 7、task_actor_binding 9、tools 54、tools_e4ic 28、turn_identity_runner 17、unknown_outcome 14、visibility 8）；
  协作 / 任务：analysis_entry_e4g 55、batch_entry_e4g2 59、chapter_entry_e4f 41、entity_entry_e4g3b 69、log_private_views 17、outline_entry_e4g3 62、task_revocation_e4 22、
  wizard_entry_e4g3c 95、engine_event_lock_order_e4ib2 3、kb_editor_entries_e6c 27、kb_index_guarded_e6b 41、migrations 27/1 skip、project_transfer 37、task_execution_fencing 11、
  task_execution_identity 21、task_execution_send_guard 23、task_sse_scope 18。
- PG（按文件顺序、未与 SQLite 并行、无 skip）：entries_e4id 4、lock_order_e4ib 3、tools_e4ic 2、engine_event_lock_order_e4ib2 4、access_e4ia PG 6、authenticated_actor PG 8、
  audit_coordinator PG 3、analysis_entry_e4g 5、batch_entry_e4g2 6、chapter_entry_e4f 7、entity_entry_e4g3b 4、outline_entry_e4g3 4、task_revocation_e4 2、wizard_entry_e4g3c 5、
  kb_editor_entries_e6c 4、kb_index_guarded_e6b 3、task_execution_fencing 1、task_execution_identity 7。
- `ruff check .` 通过。未跑全量（精简回归策略）。

偏差：
- 新增迁移 0054（任务未要求）：裁决 7 要求重邀后旧会话只读，而会话本身没有轮次信息；`created_at` 与 `responded_at` 比较在 SQLite（秒精度 CURRENT_TIMESTAMP）下不可靠，
  从轮次 Task 推导在任务被裁剪后会丢证据，故加独立列（与 0053 同口径的受保护降级）。负对照 3 证明没有它重邀者能向旧会话发送。
- E4i-a/b 复核建议的“门禁后预取作者、User 统一升序”**未在 Agent 入口实施**：首版这样做后 `test_agent_conversation_writes_revalidate_at_write_gate_postgresql` 必然由 401 变 40001
  （门禁 UPDATE 冻结 RR 快照后再读作品）。保留原序（actor 先、作者后，均在全局门禁之后）；论证：所有多 User 加锁方先取全局门禁，不取门禁的发送 lease 只锁单个 User 且随后等待的
  发送写门禁本路径在 User 锁之后才取，不成环。真正有反转风险的“发送写门禁后锁作者”已由 `require_write_execution` 默认预锁覆盖。
- `require_write_execution` 默认预锁是全局行为变化（原先只在 Agent / 引擎显式传参）；快照缺绑定时退回原锁集合而不是失败关闭，以免改变合成数据用例的失败语义。
- 拒绝先持锁的 PG 用例中，拒绝提交后的续跑复核 / 最终投影可能已看到撤权，响应可能是 200 或 404/409；用例以数据库事实（rejected、撤权完成）为准。
- 通用 `/tasks` 重试：viewer 403 `project_readonly`、重邀旧轮次 400「模型绑定无效或已失效」（E4i-b 既有口径），未统一为 409 `permission_denied`。

待确认（附建议）：
1. viewer / 旧轮次会话能否删除本人会话（私人数据清理）？现按裁决 4“只读”一律 409。建议维持；若需清理，另开“仅删除”例外并保留未知外部结果屏障。
2. 前端（E4i-f）需要判断会话是否可写：`ConversationOut` 未暴露轮次标记。建议 E4i-f 用 `access.can_use_ai` + 409 `permission_denied` 提示，不把邀请 UUID 输出给前端；
   若需逐会话只读标记，后端加布尔 `writable`（服务端计算，不含 UUID）。
3. 通用重试错误码不统一（403 / 400 / 409）。建议维持（均失败关闭、零副作用），在 E4i-f 前端统一按“不可重试”展示。

## 复核：E4i-d（2026-11-14，check agent）

结论：入口矩阵、拒绝锁内判定、0054、默认预锁、MCP 封闭、隐私和派生任务费用来源均符合裁决。发现 1 处纵深缺口，已修复。

1. **入口矩阵**：所有入口都已逐一核对，包括创建、列表、详情、改名、删除、发送、确认、拒绝、整轮确认、整轮拒绝、撤销、整轮撤销、日志、MCP 设置，以及通用 retry、cancel、resume、finish 和 stream。
   - 私人作用域统一由 `lock_agent_entry` 或 `require_personal_project` 判定：作者或 accepted 成员可进入，仅有管理员人工来源、已撤权用户和陌生人一律 404。
   - 写能力只认 `ai.use` 加原 grant，不并入 `projects.edit`。
   - 改名、删除和发送都走 `owned_conversation(lock=True)`。日志只读。取消和重试经 `task_visible_to_actor` 与 handler 的 grant 判定。
   - 未发现漏判的入口。
   - 通用 `DELETE /tasks/{id}` 允许 viewer 删除本人已结束的轮次，这是既有行为。删除后 `turn_task_id` 被置空，`_change_grant` 返回 None，协作者对该轮次的确认和撤销随之失败关闭，不会扩权。
2. **拒绝与整轮拒绝**：
   - 判定顺序：锁外初检 → `conversation_lock` → `_change_entry(lock=True)`（门禁 → User → 发送门禁 → 父行）→ 按提案所属轮次 Task 的 grant 执行 `_require_change_write` → 提案投影核对 → 改状态。判定在锁内、改动之前完成。
   - `recover_external` 提交后会再次重验。
   - 整轮拒绝先执行 `_turn(lock=True)`，再对逐项调用的 `decide_change` 各自重验。
   - 竞态语义：撤权先提交时拒绝返回 404/409，提案保持 proposed；拒绝先持锁时，撤权被阻塞到拒绝提交之后。PG 用例已覆盖这两种顺序。
3. **0054**：
   - 写入：协作者创建会话时写入当前 `invitation_id`，作者为 NULL。
   - 判定：改名、删除和发送检查会话标记。提案和轮次操作按轮次 grant 判定；轮次 grant 在创建时与会话标记一致，两者等价。
   - 不可变：标记和 `user_id` 都没有更新路径，作品也没有作者转移功能，所以会话不会在作者和协作者之间转换归属。
   - 降级：先取写锁再扫描，存在标记时拒绝降级。
   - 迁移链：`alembic heads` 只有 0054 一个 head，链路为 0052 → 0053 → 0054。
   - 不外泄：`ConversationOut`、`ConversationDetail` 以及投影快照都不含该字段；agent 三张表登记在 `NOT_EXPORTED`。
   - `exporter.APP_SCHEMA` 仍为 0052，沿用 0053 时的做法：Agent 表不在单书备份中，该值仅供参考。
   - **缺口（已修）**：轮次标记只在 HTTP 入口判定。实施负对照 3 已证明中央受理不会拦截旧会话，而 `AgentHandler.validate_and_claim` 的注释写的是“handler 内再次持锁校验，避免其他创建入口绕过约束”。现在 handler 锁内也核对 `collaboration_invitation_id == grant[0]`（作者为 NULL），不符时返回 409 `permission_denied`。
4. **默认预锁**：
   - 冻结的作者 ID 只取自服务端写入的绑定，锁后会核对 `project.user_id`，因此锁错用户也只是多锁一行，随后失败关闭。
   - 快照缺少绑定时退回原锁集合，与 E4i-d 之前完全一致，不新增风险，正确性仍由 handler 守卫失败关闭保证。
   - 预锁消除了“发送门禁之后才锁作者”与作者发送 lease 之间的环。
   - 40001 风险：作者 User 行若在快照后被不取门禁的事务修改（例如登录写 `last_login_at`），预锁会得到 40001。该窗口与 actor 本人一侧同型、很短，而且多数 handler 原本就会随后锁作者，接受这一风险。
   - **关于不重排 Agent 入口锁序**，论证成立：
     - 持多个 User 锁的事务（`require_authenticated(write)`、引擎领取或收尾、bootstrap）都先取全局门禁，彼此不会并存。
     - 不持门禁的只有单 User 锁：配额检查和发送 lease（FOR SHARE）。
     - 发送 lease 随后等待的共享门禁，本路径在两个 User 锁之后才取，因此不会成环。
5. **MCP 封闭**：四层都已到位：入口返回 403 `collaborator_mcp_unavailable`；grant 任务的 runner 的 `tool_map` 为空；`execute_change` 对带 grant 的 external 提案直接记 failed，不发网、不建屏障；`_send_lease` 拒绝带 `_collaboration_grant` 的任务并保留作者条件。编辑者无法产生会阻挡作者删除作品的未决外部结果。
6. **隐私**：
   - 会话、提案、轮次和日志都按 `user_id == actor` 过滤，作者与其他成员的会话一律 404。
   - 详情对已不可见的活动旧轮次不附 `active_task`。
   - 日志中的 `ai_config_id`、`_project_model` 和 `_collaboration_grant` 已脱敏，见 `test_collaboration_log_private_views`。
   - 协作者订阅 `agent.turn` 仍为 404。
7. **派生任务**：`test_derived_start_task_is_owned_by_editor_with_fee_source` 经真实入口验证了两种模式：授权回退时 `source=owner`、配置归作者；未授权时 `source=actor`、使用本人配置。
8. **调整的测试**：五个文件逐段核对，都是开放后的预期翻转（404 → 200/409），或者去掉入口模拟改走真实入口。断言没有放宽，`log_private_views` 还新增了详情中不含作者私聊的断言。

修复：
- `app/services/task_engine/handlers/agent.py::validate_and_claim`：锁内核对会话轮次标记。
- 测试：
  - `test_agent_entries_e4id.py` 新增 `test_central_admission_rejects_conversation_from_other_round[missing|old_round]`，绕过入口直接走中央受理，断言 409、不建任务、不写消息、零模型调用。
  - 三个直接构造协作者会话的 fixture 补写当前 `invitation_id`，与真实入口一致：`test_agent_handler_e4ib.py`、`test_agent_lock_order_e4ib_postgresql.py`、`test_agent_tools_e4ic.py` 两处。
- 规范 `backend/agent.md`：在“协作者 HTTP 入口”下补充 handler 侧的核对与 fixture 约定。
- 负对照：从 `/tmp/e4id_chk_neg_handler_ALdm` 备份恢复前，去掉 handler 核对后新增的 2 个用例均失败；恢复后 `shasum -a 256 -c` 结果为 OK。

回归（最终代码）：
- SQLite（按文件，最多 3 个并行）：全部 38 个 Agent 非 PG 文件均通过。
  - 其中 `entries_e4id` 18、`tools_e4ic` 28、`handler_e4ib` 26、`access_e4ia` 31 + 6 skip、`authenticated_actor` 18 + 8 skip + 1 xfail、`audit_integration` 8。
  - 另跑 `log_private_views` 17、`task_revocation_e4` 22、`migrations` 27 + 1 skip、`setting_category_agent` 68 + 1 skip、`chapter_entry_e4f` 41、`task_agent_evidence` 11、`project_transfer_privacy` 5，均通过。
- PG（按文件顺序执行，不与 SQLite 并行，无 skip）：`entries_e4id` 4、`lock_order_e4ib` 3、`tools_e4ic` 2、`project_style_privacy` 4、`external_deletion_guard` 9、`access_e4ia` 37、`engine_event_lock_order_e4ib2` 4、`task_agent_evidence` 6，均通过。
- `ruff check .` 通过。

对实施方 3 个问题的意见：
1. viewer 或旧轮次用户删除本人会话：**建议维持 409**，与裁决 4“只读”一致。删除会级联删除提案和撤销证据。如果产品需要清理，可另开“仅删除”例外，前提是保留未知外部结果屏障并且不在活动轮次期间执行。此外，已撤权用户的会话对所有人都不可见、长期留存，建议交给产品决定是否在撤权时提供清理。
2. 前端如何判断会话可写：**建议由后端计算 `writable` 布尔值**，不输出 UUID。只用 `can_use_ai` 无法区分重邀者的旧轮次会话：此时 `can_use_ai` 为真，但旧会话只读，前端会显示一个必然返回 409 的输入框。409 `permission_denied` 仍保留为兜底。注意加字段时要同步 `get_conversation` 投影快照 `proofs["conversation"]["snapshot"]` 的比对口径，建议放在 E4i-f 的后端部分实现。
3. 通用重试错误码：**建议维持**。viewer 返回 403 `project_readonly`，与其他 AI 入口一致；重邀者返回 400，是模型绑定 grant 失效的既有口径。三者都失败关闭、零副作用，而通用 `/tasks` 入口统一改码会波及所有任务类型。E4i-f 前端将 400、403、409 统一展示为“不可重试，请在当前会话重新发起”。

## 进度：E4i-e（2026-11-14，引擎受限收尾与订阅；只改后端，无迁移，协作者订阅仍 404）

实现：
- **裁决 11：`agent.turn` 纳入受限收尾**（`task_engine/engine.py`）：`agent.turn` 加入 `_COLLABORATION_QUEUED_CLEANUP_KINDS`（paused 白名单随之包含，候选 SQL 自动纳入）。
  `_fail_collaboration_queued` 对 `agent.turn` 在终态 CAS 同事务调用 `agent.service.void_revoked_turn_changes`。queued 走领取锁内复核（gate → User → 作品 → Task），
  paused 走 `_fail_collaboration_paused`（gate → actor/作者 User 升序 → 发送写门禁 → 作品 → Task）。
- **提案处置**（`agent/service.py::void_revoked_turn_changes`，常量 `COLLABORATION_CHANGE_VOIDED`=「协作授权已失效，提案已作废」）：只处理本轮
  （`turn_task_id` + `turn_task_key` 精确、同作品）`proposed` 且 `result` 无外部内部标记的行，条件 UPDATE 为 `failed`、`error` 与 `result.error` 为固定文案
  （start_task 来源证明随之清除）；`executing` / 带外部标记 / 其他状态不动——外部未知屏障（`has_unsettled_external_change`、`require_no_unknown_external`）保留。
  不写消息、不改作品、不发送。作废状态经 `ChangeOut.status/error` 在本人会话可见。
- **运行中断点**（`handlers/agent.py::on_abort` → `_void_revoked_turn`）：协作轮次因撤权复查取消或断点复核终止时，`on_abort` 先无锁预筛
  （`TaskEngine._paused_grant_lost`，授权有效即不取写锁），失效时按 gate → actor/作者 User 升序 → 发送写门禁 → 作品 `lock_active`（删除中不越过）→ Task
  锁内 `_collaboration_claim_failure` 复核后作废并提交；异常只记日志不阻断收尾。作者轮次直接返回；授权有效时的本人取消 / 普通失败不作废（保留同轮次重试复用提案的语义）。
- **续跑冻结向量重验**（E4i-c 遗留，`build_job`）：在 `require_write_execution` 后同一锁内，向量模式先按 ID 升序取全部配置选择门禁（与 `SendGuard._verify_bindings`
  同序），再 `revalidate_project_model` + `revalidate_shared_embedding`。首次运行与 paused → queued → running 的每次领取都重验；作者向量配置改动 → 400
  「模型配置已失效，请重新选择」，runner 不启动。授权变化先由领取锁内受限收尾。
- **裁决 10**：未开放。现有 `test_agent_entries_e4id.py::test_turn_subscription_stays_404_for_collaborators` 与 `test_task_sse_scope.py` 通过；新测试在作废后
  再断言协作者订阅 404。
- 规范：`backend/agent.md` 新增「协作轮次的撤权受限收尾（E4i-e，裁决 11）」；`backend/error-handling.md` E5-a 条目改为拆书/知识库不在白名单并指向 agent.md。

测试：
- 新增 `tests/test_agent_revocation_e4ie.py`（SQLite 28）：
  - 4 种撤权（revoke / viewer / model_off / reinvite）× {待确认 paused、已无待确认的 paused、queued、运行中断点（有通知 / 无通知各一）}，共 20 例：任务 failed（固定文案），
    本轮 proposed 提案作废且会话可见（revoke 为 404）、不能再确认、已 executed 效果保留、撤权后零模型调用；待确认用例另断言会话不再被占用、协作者订阅 404。
  - 不误伤：作者待确认轮次保持 paused 并可确认续跑成功；编辑者授权有效时本人取消的轮次，提案保持 proposed 且事后撤权不回头作废（复查收尾数 0）。
  - 外部屏障：轮次内 executing 外部行、带未知标记的 proposed 外部行、作者会话的未知外部结果均原样保留，`has_unsettled_external_change` 前后都为真。
  - 作废函数单元：错误 UUID 不动、重复调用幂等。
  - 续跑向量重验：{暂停期间改作者向量配置、续跑入队后改配置、撤权、关闭作者模型授权} 4 例零作者向量 / 零模型调用 / 无新生成记录；正向对照（未变化时续跑成功）。
- 新增 `tests/test_agent_revocation_e4ie_postgresql.py`（PG 3）：续跑持锁（revalidate_resume 后、提交前）时第二实例撤权被阻塞（`pg_blocking_pids`），放行后续跑 queued、
  撤权提交、领取受限收尾 failed、零模型调用；撤权先于续跑取锁 → 确认 404/409、轮次 paused → 复查收尾 failed；续跑入队后改作者向量配置 → 构建锁内失败关闭。
- 调整（预期翻转，断言未放宽）：
  - `test_collaboration_paused_cleanup_e5a.py`：白名单外类型示例由 `agent.turn` 改为 `book.import`。
  - `test_agent_entries_e4id.py`：只读 / 已撤权身份在降级后显式复查（原先后台复查与快照竞争），t1 断言 failed，详情中两项待确认提案为 failed + 作废文案。
  - `test_agent_handler_e4ib.py::test_editor_resume_rejected_after_reinvite`：重邀后旧轮次由 paused 改为受限收尾 failed（仍从未续跑）。
  - `test_agent_access_e4ia.py::test_authorization_loss_terminates_turn_instead_of_failing_change[committed]`：已提交降级后复查，提案 failed、轮次 failed、仍未执行；
    `after_entry`（同事务降级随确认回滚）维持 proposed / paused。

负对照（每次还原后 `shasum -a 256 -c` 均 OK）：
1. `/tmp/e4ie_neg_void_Ucnl`：去掉引擎与 `on_abort` 的作废调用 → 13 例失败（待确认 4、运行中 8、外部屏障 1）。
2. `/tmp/e4ie_neg_resume_W1DJ`（SQLite）、`/tmp/e4ie_neg_resume_pg_J46c`（PG）：去掉 `build_job` 的冻结向量重验 → 两个改配置用例与 PG 用例失败（runner 启动后在发送守卫处
   才失败，错误为「权限或模型授权已变化，已停止发送」）。

回归（最终代码）：
- SQLite（按文件，≤3 并行）：revocation_e4ie 28、task_revocation_e4 22、task_sse_scope 18、entries_e4id 18、tools_e4ic 28、handler_e4ib 26、engine_event_lock_order_e4ib2 3、
  audit_coordinator 19/3 skip、paused_cleanup_e5a 25、access_e4ia 31/6 skip、log_private_views 17、external_deletion_guard 39、agent_api 11、task_surface_e5a 38、
  audit_integration 8、admin_task_cancellation 30、stream_early_subscribe_e5e 84、project_style_privacy 35、content_review 22、recycle_bin_tasks 13，全部通过。
- PG（按文件顺序，未与 SQLite 并行，无 skip）：revocation_e4ie_postgresql 3、external_deletion_guard_postgresql 9、entries_e4id_postgresql 4、paused_task_postgresql 9、
  lock_order_e4ib 3、task_revocation_e4_postgresql 2、access_e4ia PG 6、audit_coordinator PG 3，全部通过。
- `ruff check .` 通过。未跑全量（精简回归策略）。

偏差：
- 续跑重验放在 `build_job`（领取锁内）而非 `revalidate_resume`：放在后者会让“确认已提交、续跑被拒”的轮次滞留 paused（授权仍有效，复查不收尾），会话被占用；
  放在构建锁内则任务以失败终态结束、会话释放，且同时覆盖首次运行。文本模型的既有 `revalidate_resume` 检查未改。
- 运行中轮次的作废与终态不在同一事务（`on_abort` 在终态 CAS 之前单独提交）：失效不可逆，先后不改变结论；作废失败只记日志，提案因轮次已非 paused 仍不可确认。
- queued 收尾只作废本任务绑定的提案；排队中的重试轮次的源轮次（已结束）提案仍为 proposed（与作者既有失败轮次同形，均不可确认），未回头处理。
- 运行中用例里“有通知”路径固定协作文案；“无通知”路径由断点复核终止，错误文案取决于终止点（策略文案或执行身份变化），用例只断言 failed。

待确认（附建议）：
1. 是否需要给作废提案在会话里追加一条系统消息（如“协作授权已失效，本轮已停止”）？现在只靠提案 `status/error` 与任务失败文案可见。**建议不加**：消息会进入后续模型历史，
   且旧会话对撤权者不可见、对降级 / 重邀者只读，前端（E4i-f）按提案状态展示即可。
2. 已结束（failed / cancelled / interrupted）的协作轮次中残留的 proposed 提案（排队重试的源轮次、或撤权前已因其他原因结束的轮次）是否也要事后作废？**建议维持不作废**：
   它们已因轮次非 paused 无法确认；事后批量改写会把授权有效时的普通失败也改成“授权失效”文案，造成误导。前端可按“轮次已结束”统一置灰。
3. 续跑因作者向量配置变化失败后，编辑者只能在会话重新发起（重试同轮次会沿用旧冻结选择再次失败）。**建议维持**（E6 裁决 20②：不重选配置），E4i-f 前端对该失败给出“作者向量配置已变化，请重新发起”提示。

## 复核：E4i-e（2026-11-15，check 代理）

结论：作废范围、三条状态路径的锁序与同事务性、续跑向量重验、协作者订阅 404 均符合裁决 10/11/12。发现并修复 2 个问题（均补测试与负对照）。

核对要点：
- **作废范围**：`void_revoked_turn_changes` 只按 `turn_task_id + turn_task_key + project_id` 精确匹配 `proposed`，带任一外部内部标记（5 个键，与
  `has_unsettled_external_change` / `require_no_unknown_external` 同集合）或非 dict `result` 的行跳过；`executing`、其他轮次、作者轮次不动。无标记的
  proposed external 改为 `failed` + `{"error": str}`，在两种屏障判定中都是合法终态，结论不变。条件 UPDATE 带 `change_key/status/turn` 守卫，幂等。
- **锁序**：确认入口 `_change_entry(lock=True)` 为 gate → User → 发送写门禁 → 作品 → 提案行，与作废（… → Task → 提案行 FOR UPDATE）同向，无 ABBA。
  确认在作品锁内读取 Task 状态，终态 CAS 也取作品锁，所以“轮次非 paused 则不能确认”在锁内成立；失败轮次上残留的 proposed 永远不能确认。
- **on_abort 预筛 TOCTOU**：因撤权而终止时，撤权已先提交，预筛必然看到；预筛与终态之间才提交的撤权只会出现在本人取消 / 普通失败 / 关停路径，
  其结果等同于既有的“失败轮次保留提案”，不能确认，重试需原 grant（撤权 / 重邀 / 降级都改变 revision），不可利用。
- **单独提交的崩溃窗口**：作废已提交而终态未提交 → 重启 `recover()` 记 interrupted 并再调用 `on_abort`（幂等）；终态已提交而作废失败 → 残留 proposed
  不可确认。两种情况都不能利用；UI 至多短暂看到运行中轮次下的“已作废”提案。偏差可接受，但原注释“下次失效收尾会再试”不成立
  （failed 不是复查候选），已更正。
- **续跑重验**：build_job 取门禁的集合 `{binding.config_owner_id, selection.project_owner_id}` 是 SendGuard `{actor, owner}` 的子集，同为升序；
  配置写入为“认证 User → 配置门禁（写）”，build_job 为“gate/User → 发送写门禁 → 作品 → 配置门禁（读）”，不成环。普通 READ COMMITTED 会话，
  无 40001 风险。放在 build_job 而非 revalidate_resume 的偏差同意。
- **e4id 的显式复查**：通知触发的后台复查与 `before` 快照存在竞态，显式复查只是让测试结果确定，不掩盖缺陷。新增
  `test_paused_turn_finished_by_change_notification_alone` 证明不做显式复查时仅靠通知即可收尾。其余 3 个调整均为语义翻转，
  保护性断言（409 `permission_denied`、`executed == []`、作品值不变、NotFoundError）都保留，没有放宽。

修复：
1. **断点已提交、paused 终态提交前撤权 → 轮次永久 running**（`engine.py` 终态异常分支，`handlers/agent.py::finalize_revoked_pause`）：
   paused 的 `verify_finalization` 按原 grant 抛错后，agent.turn 没有回退分支，任务带 `execution_key` 停在 running，提案仍为 proposed，会话被占用直到重启。
   待审核复查只看 paused，不会收尾。现在走与 on_abort 相同的锁内复核 + 作废（`_void_revoked_turn` 改为返回失效原因），再按原 UUID/轮次/执行键 CAS
   为 failed + 协作文案；授权仍有效的其他核验失败保持原屏障。
   新增测试 `test_revocation_between_checkpoint_and_paused_commit_fails_turn`（4 种撤权 × 有 / 无通知 = 8 例）和 PG 用例
   `test_postgresql_revocation_before_paused_commit_fails_running_turn`（跨实例撤权）。
   负对照 `/tmp/e4ie_review_neg_pause_Sx46`：还原后用例停在 running 而失败；恢复后 `shasum -a 256 -c` OK。
2. **SQLite 可见性锁的取消被掩盖**（`ai_dispatch._lock_sqlite`）：撤权复查取消落在 no-op UPDATE 上时，连接已作废，`finally` 恢复 `busy_timeout`
   抛出 PendingRollbackError，替换了 CancelledError。任务因此被记为「任务执行失败：服务器内部错误」，`running…[*-notified]` 约 1/8 偶发失败
   （复核时复现 2 次）。现在仅当原异常为取消时吞掉恢复失败，其余异常行为不变。
   新增测试 `test_cancel_during_sqlite_visibility_lock_is_not_masked`；修复后 running 用例连续 42 次通过。
   负对照 `/tmp/e4ie_review_neg_lock_tx33`：还原后得到 PendingRollbackError 而失败；恢复后 `shasum -a 256 -c` OK。
   同模式的 `chapter_service` / `project_access` / `setting_ai_visibility` / `series_service` 位于 HTTP 请求路径，本次未改，留作后续统一处理。
- 规范：`backend/agent.md` E4i-e 条目补充以上两点及回归清单。

回归（最终代码）：
- SQLite（按文件，≤3 并行）：revocation_e4ie 39、entries_e4id 18、handler_e4ib 26、access_e4ia 31/6 skip、paused_cleanup_e5a 25、task_revocation_e4 22、
  task_sse_scope 18、sqlite_cancel_rollback 1、ai_visibility_dispatch 19/1 skip、agent_visibility 8、collaboration_story_dispatch 9、
  engine_event_lock_order_e4ib2 3、tools_e4ic 28、admin_task_cancellation 30、task_surface_e5a 38、agent_api 11、external_deletion_guard 39，全部通过。
- PG（按文件顺序，未与 SQLite 并行）：revocation_e4ie_postgresql 4、entries_e4id_postgresql 4、paused_task_postgresql 9、lock_order_e4ib 3、
  task_revocation_e4_postgresql 2、engine_event_lock_order_e4ib2_postgresql 4、external_deletion_guard_postgresql 9、task_sse_scope_postgresql 7、
  access_e4ia（`-k postgres`）6，全部通过。
- `ruff check .` 通过。

对实施方 3 个问题的意见：
1. 作废时追加系统消息：**同意不加**。系统消息会进入后续模型历史，旧会话对撤权者不可见、对降级 / 重邀者只读。E4i-f 按 `ChangeOut.status/error`
   与任务失败文案展示即可。
2. 事后作废已结束轮次的残留 proposed：**同意不作废**。这些行在锁内已无法确认（Task 非 paused），批量改写会把授权有效时的普通失败误标为“授权失效”。
   前端按“轮次已结束”统一置灰。本次修复后，“断点后撤权”的轮次已同事务作废，不再属于残留。
3. 作者向量配置变化后重试持续失败：**同意维持**（裁决 20②，不重选配置）。E4i-f 对 400「模型配置已失效，请重新选择」的 agent.turn 失败
   隐藏“重试”，提示“作者向量配置已变化，请在会话中重新发起”。

## 进度：E4i-f（2026-11-15，裁决 23 后端 writable + 协作者 Agent 前端；无迁移）

实现（后端，裁决 23）：
- `ConversationOut.writable: bool`（默认 false）：列表 / 详情 / 新建 / 改名均由服务端计算，不输出邀请 UUID。
  `agent/access.py` 新增 `conversation_writable(entry, uid, row)`（包装 `require_conversation_write`，同一判定）与
  `conversation_writable_fact(...)`（核验快照用的纯事实版本）。
- `get_conversation` 改用 `_conversation_entry(lock=False)` 取 entry，writable 放入 `proofs["conversation"]["snapshot"]`；
  `personal_projection._final_visibility` 的 auth CTE 增加 `member_role` / `member_invitation_id`，核验时按同一 SQL 事实重算 writable 再比对，
  读取与核验之间授权变化 → 404 失败关闭。409 `permission_denied` 仍为写入口兜底。`update_conversation` 改为显式 `_conversation_entry(lock=True)` +
  `require_conversation_write`（语义不变）。规范 `backend/agent.md` 补「会话可写性（E4i-f）」条目。

实现（前端）：
- `utils/projectAccess.ts`：新增 `canUseAgent`（服务端 `can_use_ai` 且来源为作者或 accepted 协作者，管理员 `projects.edit` 不并入）、
  `canViewAgent`（可写者，或协作者含 viewer / 管理员兼 viewer 只读本人会话）、`authorizationVersion`；`stores/project.ts` 的 `ProjectCapability`、
  `hooks/useProjectAccess.ts` 的 `canUseAgentNow` / `canViewAgentNow` 随之扩展。`canUseOwnerAI` 保留给灵感 / 知识库等仍仅作者入口。
- `ProjectLayout`：按钮与 `AgentDock` 改用 `canViewAgent`；Dock 键 `${pid}:${authorizationVersion ?? 'owner'}:${scopeVersion}`（作品 + 授权轮次 + 能力代次），
  授权变化整体重挂，不沿用旧可写状态。
- `useAgentConversation`：读取作用域 `canViewAgent`、写作用域 `canUseAgent`；`writable = detail.writable && !lost && canUseAgent`，所有写动作先判。
  进度来源 `agentProgressMode`：作者 SSE；协作者不订阅（裁决 10），未结束轮次（含 paused）`startAgentTurnPolling` 页面可见时 3 秒轮询详情，
  轮次结束时与作者订阅结束相同地失效资源并刷新列表。中途撤权：任何读 / 写遇 409 `permission_denied` 或 404 → 停止订阅与轮询、刷新作品权限、
  权威重读一次（仍可读则以服务端 writable 切为只读；仍 404 则清空详情并提示「会话已不可访问」、刷新列表）。
  协作者通用重试遇 400/403/409 → 隐藏重试，统一「不可重试，请在当前会话重新发起」（裁决 24）；agent.turn 失败含「模型配置已失效，请重新选择」
  → 隐藏重试，提示「作者向量配置已变化，请在会话中重新发起」（裁决 27）。作者行为不变。
- `AgentConversationView`：writable=false 显示「此会话仅供查看」+ 原因（查看者 / 旧授权轮次），隐藏输入区、自动执行、停止与重试；
  MCP 设置对协作者隐藏（裁决 2）；模型来源为作者授权时提示「外部服务费用由作者账户承担」。
- `AgentTimeline` / `AgentChangeCard`：`canWrite=false` 时确认 / 拒绝 / 撤销 / 撤销整轮禁用；作废提案（裁决 25）标签「已作废」并显示原文案；
  所属轮次已结束的残留 proposed（裁决 26）置灰为「轮次已结束」、不再提供确认 / 拒绝 / 全部确认。
- `AgentPanel`：查看者不能新建（空态说明）；`writable=false` 的会话列表标「（只读）」、改名 / 删除禁用。
- `MemberPanel`：修改 / 撤销已加入的作品成员时提示「会立即中止其进行中的 AI 任务（包括智能体对话）」（E4i-c 复核带入）。
- `utils/agent.ts` 新增纯函数 `isAgentTurnOpen` / `agentChangeState` / `agentRetryState` / `agentReadOnlyNotice` 与固定文案；`api/agent.ts` 新增 `isAgentAccessLost`。

测试：
- 后端新增 `tests/test_agent_writable_e4if.py`（SQLite 11）：8 身份矩阵（作者、授权编辑者、未授权编辑者、仅改模型授权、viewer、管理员兼 viewer、
  管理员兼编辑者、撤权后重邀）× 列表 / 详情 / 改名（writable=false 时 409 兜底），响应不含邀请 UUID；重邀者新会话 true、旧会话 false；
  同一会话 editor→viewer→editor 往返与撤权后 404；读取时 writable 被篡改为 true（模拟两次读取间降级）→ 详情投影核验 404；事实函数表。
- 前端新增 `utils/agentCollaborator.test.ts`、`components/agent/useAgentConversation.test.ts`（进度来源 × 身份、可见轮询 / 回前台 / 清理、失权停止）、
  `components/agent/AgentPanel.test.ts`；扩展 `agentUI.test.ts`（作者 / 可写编辑者 / 查看者 / 旧轮次 / 重试提示 / 作废与轮次结束）、
  `projectAccess.test.ts`（智能体能力 8 身份、不按角色名推断）、`useProjectAccess.test.ts`、`collaborationUI.test.ts`（成员变更提示）。
- 调整（预期翻转）：`collaborationWorkspace.test.ts` viewer / editor / 管理员兼 viewer 现显示「项目智能体」（裁决 1、4），纯管理员仍不显示；
  `agentUI.test.ts` 封存用例的 mock 改为可变状态并补齐 `active_task.task_key`（原 mock 缺键，新的轮次结束判定需同一实例）。

负对照：`/tmp/e4if_neg_round_46gw`：`conversation_writable` 与事实函数都忽略邀请轮次 → `test_writable_across_actor_matrix[reinvited]`、
`test_fact_function_matches_entry_rule` 失败；还原后 `shasum -a 256 -c` OK，复跑 11 passed。

回归（最终代码）：
- 后端 SQLite（按文件，≤3 并行）：writable_e4if 11、entries_e4id 18（并行批次中 `test_derived_start_task_is_owned_by_editor_with_fee_source` 偶发 1 次
  succeeded≠paused，单独复跑 3 次与整文件复跑均通过）、revocation_e4ie 39、log_private_views 17、agent_api 11、project_style_privacy 35、access_e4ia 31/6 skip。
- 后端 PG（顺序、未与 SQLite 并行、无 skip）：entries_e4id_postgresql 4、revocation_e4ie_postgresql 4、project_style_privacy_postgresql 4、access_e4ia `-k postgres` 6。
- `ruff check .` 通过。
- 前端：`npm run lint` 通过、`npm run build` 通过、全量 vitest 219 文件 / 1545 通过（上次 1506）。未做浏览器验证（由主会话另行进行）。

## 复核：E4i-f（2026-11-15，check 代理）

结论：后端 `writable` 与 409 写检查是同一判定，四处输出一致，邀请 UUID 未泄露，投影快照比对正确。前端能力派生、只读控件、轮询与 SSE 分流基本符合裁决 1–4、10、23–27。发现并修复 4 个前端问题，并定位、修复了偶发失败的测试（测试顺序竞态，不是产品缺陷）。

核对要点：
- **writable 口径**：列表（`lock=False`）、详情（`lock=False` + 投影核验）、新建 / 改名（`lock=True`）都调用 `conversation_writable` → `require_conversation_write`，与写入口的 409 是同一函数。事实函数 `conversation_writable_fact` 与它等价：
  - `access.can_use_ai = owner or editor`；
  - grant 取当前成员，revision 恒等；
  - 只比 invitation_id。
  
  其他成员字段（如 `allow_owner_ai`、revision）的 PATCH 不改变 writable，不会新增 404 抖动。只有角色或邀请轮次在两次读取间真实变化时才 404，这是失败关闭。8 身份矩阵中详情全部 200，证明稳定状态下两侧一致。`ConversationOut`、`ConversationDetail` 均不含 `collaboration_invitation_id`、`summary_source`，测试断言响应中不出现任何轮次的 UUID。
- **既有抖动面（非本切片引入，记录）**：运行中轮次的提案状态变化（自动执行 proposed→executed）若落在“读取—核验”的毫秒窗口内，会触发 404「修改提案不存在」。作者 5 秒核对本来就有此窗口，协作者 3 秒轮询使其略为常见。前端经恢复控制器重读一次即可恢复，不清空会话。
- **能力**：`canViewAgent`、`canUseAgent` 只取服务端 `can_use_ai`、`is_collaborator`、`role`，管理员的 `projects.edit` 不并入。纯管理员不挂载 Dock（workspace 测试断言）。`canUseOwnerAI` 仍用于灵感和知识库调试等作者专属入口。
- **调整的测试未放宽**：
  - `collaborationWorkspace.test.ts` 中 viewer、editor、管理员兼 viewer 显示「项目智能体」，属于按裁决 1、4 的预期翻转；纯管理员不显示的断言保留。
  - `agentUI.test.ts` 封存用例的原断言（含「新开会话」、无输入框、确认禁用）全部保留，补 `task_key` 是为了让 mock 与真实 API 形状一致。

修复：
1. **失权核对遇暂时失败后永久卡住**（`useAgentConversation`）：409/404 后的权威重读若遇网络等非权限错误，`lost` 一直为真，轮询停止、写入禁用。手动「刷新」成功也清不掉，只能重挂面板。
   - 改为提取纯控制器 `createAgentAccessRecovery`：只进入一次核对；重读成功即恢复；仍 404/409 时清空并保持停止，不自动循环；暂时失败时保持停止，之后任一成功的权威读取（`refreshSafe` → `settle`）恢复。
   - 被更新读取取代的结果改抛 `AgentReadSuperseded`，静默丢弃，不再误显示「作品权限已变化」。
   - 新增 4 个单测。负对照 `/tmp/e4if_review_neg_settle_gc6S`：`settle` 失效 → 2 例失败；还原后 `shasum -a 256 -c` OK。
2. **查看者的封存会话仍显示「新开会话」**：点击后静默无效，说明文字也提示“请新开会话继续”。现在按 `canUseAgent` 隐藏按钮与该句，新增 UI 测试。
3. **会话列表改名 / 删除遇 409 `permission_denied` / 404 后不重读**：列表的 writable 一直停在旧值。现在遇失权即关闭改名框并 `reload()`。
4. **SSE 防御**：订阅改为仅在确认作者（`access.ready && role === 'owner'`）时进行。权限快照未就绪时不再按作者处理。
- 规范：`frontend/component-guidelines.md` 第 9 行不再写“`canUseOwnerAI` 控制项目智能体”，并在「项目智能体面板」中补「协作者模式（E4i-f）」条目。

偶发失败 `test_derived_start_task_is_owned_by_editor_with_fee_source`：
- **复现**：3 路并行循环，失败 3/15。
- **定位**：在临时插桩副本中（已还原，`shasum -a 256 -c` OK），失败时第 2 轮的 `fake_llm.tool_calls` 依次为：

  ```
  [轮1 首调, 轮1 续跑(作者 key、工具结果消息), 轮2 首调(编辑者 key), …]
  ```

  原因：第 1 轮确认 `start_task` 后轮次自动续跑，`engine.start()` 后与第 2 轮的准备并发执行，先弹出了第 2 轮压入的 `start_task` 脚本（`tool_script` 共享）。第 2 轮因此拿到默认文本，直接 succeeded。这是测试顺序竞态，不是产品缺陷。
- **修复**：每轮结束前等本轮续跑 succeeded、派生任务进入终态，并断言 `tool_script` 已耗尽。并行 3 路循环共 21 次全部通过。

回归（最终代码）：
- 前端：`npm run lint` 通过，`npm run build` 通过；受影响 vitest 10 个文件 113 通过；全量 219 个文件 1550 通过（上次 1545，+5）。
- 后端 SQLite（按文件，≤3 并行）：writable_e4if 11、entries_e4id 18、revocation_e4ie 39、agent_api 11、access_e4ia 31（另 6 skip）、collaboration_log_private_views 17。
- 后端 PG（顺序执行，未与 SQLite 并行）：entries_e4id_postgresql 4、revocation_e4ie_postgresql 4、access_e4ia `-k postgres` 6。
- `ruff check .` 通过。未做浏览器验证。

对实施方 3 个问题的意见：
1. 偶发测试：**已定位并修复**，见上，属于测试竞态。建议以后凡是在同一测试内多轮复用 `fake_llm.tool_script` 的用例，都在进入下一轮前等上一轮续跑结束。
2. 重试提示仅对协作者：**建议维持**。
   - 作者轮次不冻结作者向量，「作者向量配置已变化」的文案对作者不成立；作者的配置失效重试会在受理时得到明确 400，原行为不变。
   - 注意 `模型配置已失效，请重新选择` 还会在编辑者本人或作者的**文本**模型配置被改 / 删时出现（`_bound_config`、`revalidate_project_model`）。这些情况下隐藏重试同样正确（重试沿用冻结绑定，必然再次失败），但「作者向量配置已变化」的措辞偏窄。建议用户确认是否改为「模型配置已变化（可能是本人或作者的配置），请在会话中重新发起」。文案是裁决 27 指定的，本次未改。
3. 禁用还是隐藏：**建议维持现状**。
   - 只读会话：输入区、自动执行、停止、重试隐藏；提案上的确认 / 拒绝 / 撤销保持禁用态，配合顶部只读说明，让用户看到“此处原本可操作但当前无权”。
   - 已结束轮次：确认 / 拒绝隐藏，改为「轮次已结束」说明并整卡置灰，因为这类操作永远不会再可用。
   
   两者语义不同，按“暂时无权→禁用、永久不可→隐藏”区分合理。

## 进度：E4i-g（2026-11-15，§9.2 PostgreSQL 并发 9 项 + E7 端到端；只补测试，无产品代码改动，无迁移，不开放新入口）

### 覆盖映射（research e4i-agent-inventory §9.2）

| # | 场景 | 已有用例（引用） | 缺口 → 本片新增（`tests/test_agent_e4ig_postgresql.py`） |
|---|---|---|---|
| 1 | 发消息受理 vs 撤权（两种顺序） | `test_agent_authenticated_actor.py::test_agent_conversation_writes_revalidate_at_write_gate_postgresql[send]`（仅作者原令牌撤销、单一顺序） | 编辑者成员撤权：`test_pg_send_after_committed_revocation_is_denied[revoke/viewer]`（撤权先提交 → 404/409，零任务零消息零请求）；`test_pg_send_holding_lock_blocks_revocation`（受理先持锁 → 撤权被 `pg_blocking_pids` 观测阻塞，受理先提交，领取锁内受限收尾 failed、零请求） |
| 2 | 模型真实 HTTP 发送 lease vs 撤权/降级/`allow_owner_ai` 关闭/令牌撤销（响应头前后） | `test_collaboration_ai_guard_postgresql.py::test_revoke_waits_for_real_send_headers_then_old_binding_fails`（通用 guard）；`test_agent_context_actor_version_postgresql.py`（作者 Agent 令牌）；`test_agent_lock_order_e4ib_postgresql.py`（编辑者锁序，假发送） | 编辑者 Agent 真实 SDK HTTP（本机服务器、`create_provider`）：`test_pg_editor_real_http_send_lease[main × {revoke,viewer,model_off,token} × {before_send,during_send}]` 8 例：发送前提交 → 0 次 HTTP；发送中 → 变更被 lease 阻塞到响应头、已发 1 次不撤回、输出不进消息/任务输出/hub、轮次 failed |
| 3 | 后台写工具 vs 撤权；管理员兼编辑者降 viewer 写工具失败且审计 | `test_agent_tools_e4ic_postgresql.py::test_postgresql_editor_tool_write_and_downgraded_old_grant`（顺序降级）；`test_agent_access_e4ia.py::test_postgresql_admin_source_never_authorizes_agent_write`（直接调工具） | 自动执行真实轮次：`test_pg_auto_write_tool_vs_revocation[{editor,admin} × {revocation_first,tool_first}]` 4 例：撤权先提交 → 工具不执行、零后续请求、**零管理审计**；工具先持锁 → 撤权被阻塞、合法结果保留、之后零请求，管理员兼编辑者恰好 1 条 `project.update` success |
| 4 | HTTP 确认（含 `execute_admitted` 派生任务）vs 撤权；确认请求取消 | `test_agent_access_e4ia.py::test_postgresql_confirm_path_rechecks_inside_tool`（同事务降级）；`test_agent_revocation_e4ie_postgresql.py` 前两例（确认→续跑两顺序）；`test_agent_entries_e4id_postgresql.py`（拒绝两顺序） | `test_pg_confirm_start_task_after_committed_revocation[revoke/viewer]`（零派生任务、章节不占用）；`test_pg_confirm_start_task_holding_lock_then_revocation`（派生任务先提交且绑定原轮次，撤权后领取锁内受限收尾 failed、零请求、章节占用释放，轮次亦受限收尾）；`test_pg_cancelled_confirm_request_rolls_back`（工具已写未提交时客户端取消 → 整体回滚、锁释放、再次确认照常） |
| 5 | 撤销 vs 撤权；撤销 vs 人工编辑（effects 版本预检） | `test_agent_access_e4ia.py::test_postgresql_undo_coordinates_nested_audit`（审计协调）；`test_agent_entries_e4id_postgresql.py::test_postgresql_editor_send_tool_confirm_undo`（顺序正例） | `test_pg_undo_after_committed_revocation_is_denied[revoke/viewer]`；`test_pg_undo_holding_lock_blocks_revocation`；`test_pg_undo_vs_author_manual_edit[manual_first/undo_first]`（人工先 → 409 `undo_conflict` 人工值保留；撤销先持锁 → 人工编辑被阻塞后在其上提交，二者都不丢） |
| 6 | 手动 MCP 确认 vs 撤权（裁决 15：编辑者 MCP 关闭） | 作者未知结果屏障：`test_agent_authenticated_actor.py::test_external_execution_claim_postgresql`、`test_agent_external_deletion_guard_postgresql.py`（9）、`test_task_agent_evidence_postgresql.py`（6）；编辑者关闭：SQLite `test_agent_entries_e4id.py` 矩阵 403、`test_agent_handler_e4ib.py::test_external_call_refuses_collaboration_job` | `test_pg_editor_mcp_closed_and_residual_external_never_blocks_author_delete`：编辑者本人有可用插件，会话挂载 403、新建不能挂载；人为植入残留外部提案 + 残留挂载，确认持锁期间作者删除作品被阻塞、放行后确认只记 `CHANGED` 失败、零连接零调用、无未决外部证据，删除 204 |
| 7 | 摘要发送 vs 撤权 | `test_agent_context_actor_version_postgresql.py::test_revoked_actor_after_headers_does_not_persist_summary`、`test_agent_context_execution_identity_postgresql.py`、`test_agent_summary_task_token_identity_postgresql.py`（均作者）；`test_agent_lock_order_e4ib_postgresql.py::test_postgresql_editor_turn_with_compaction_keeps_marked_summary`（编辑者正例） | 并入第 2 组：`[summary × {revoke,token} × {before_send,during_send}]` 4 例：摘要未保存、`summary_source` 为空、轮次 failed |
| 8 | RR 快照 40001 在编辑者路径映射为安全 409 | `test_agent_audit_coordinator.py::test_postgresql_real_serialization_conflict_keeps_safe_failure`（协调器层）；`test_agent_turn_identity_postgresql.py::test_repeatable_read_rejects_task_replaced_after_snapshot`（作者） | `test_pg_editor_confirm_serialization_conflict_is_safe_409`：编辑者确认快照冻结后另一真实连接原值写回同一提案行 → 条件 UPDATE 真实 40001 → 409 `CONCURRENT_CHANGE`、不含 SQL、提案仍待确认、重试成功 |
| 9 | paused 轮次撤权后受限收尾 vs 本人确认 | `test_agent_revocation_e4ie_postgresql.py` 4 例（确认/续跑先持锁、撤权先提交、续跑冻结向量、断点后撤权） | 收尾先持锁：`test_pg_restricted_finish_holding_lock_blocks_own_confirm`（第二实例受限收尾持锁作废提案，确认在入口写锁上被阻塞，放行后 409 `permission_denied`、两项提案作废、轮次 failed） |

E7 端到端（`tests/test_agent_e2e_e4ig.py`，SQLite 6 例；PG 对照 `test_pg_editor_request_ledger_and_config_source[fallback/own_ungranted]` 2 例复用同一断言）：
- `test_editor_turn_request_ledger_and_config_source[fallback/own_granted/own_ungranted]`：真实 HTTP 入口一条轮次 = 会话压缩摘要 + 工具轮（`search_knowledge` + `start_task`）+ 确认后派生 `chapter.generate` + 续跑轮。精确账目：文本请求 4（摘要 1、工具轮 2、派生章节 1），向量请求授权时 2（工具检索查询 + 派生章节上下文检索，均为作者向量 `owner-embed`）、未授权 0；密钥只出现本轮实际来源（回退 = 作者，本人优先 = 编辑者，非回退模式作者密钥零次）。内部：轮次与派生 Task 的 `_project_model`（source / config_owner_id / config_id / actor_id）与 `_collaboration_grant`；4 条生成记录（3 条 agent.turn + 1 条 generate）`actor_user_id=编辑者`、`model_source`、`ai_config_id`、`model_config_owner_id` 与 Task 绑定一致（回退时内部保留作者配置 ID）。编辑者可见：会话日志 `ai_config_id` 回退时为 null、本人模型时为本人配置；会话详情、会话列表、轮次与派生任务的 `/tasks/{id}` 中不出现作者配置 ID、`_project_model`、`_collaboration_grant` 或作者密钥。
- `test_invalid_own_config_never_falls_back_to_owner`：本人配置密文损坏 + 作者已授权 → 400，零请求、不建任务。
- `test_revocation_mid_turn_sends_nothing_more[revoke/model_off]`：模型流中途撤权 / 关闭作者授权 → 轮次 failed，文本/工具/向量计数均不再增加，不产生派生任务。
- 已有可引用：模型来源与回退（`test_agent_handler_e4ib.py`）、检索冻结与零作者向量（`test_agent_tools_e4ic.py`）、派生任务费用来源（`test_agent_entries_e4id.py::test_derived_start_task_is_owned_by_editor_with_fee_source`）、日志脱敏（`test_collaboration_log_private_views.py`，种子行）。本片补的是“真实轮次 → 记录 → 投影”的整链一致性与精确请求次数。

### 新增文件与用例数
- `backend/tests/test_agent_e4ig_postgresql.py`：33 例（并发 31 + E7 PG 对照 2），PG 真实运行、无 skip。
- `backend/tests/test_agent_e2e_e4ig.py`：6 例（SQLite），导出 `check_request_ledger` 供 PG 复用。

### 修复
- 无产品缺陷暴露，未改生产代码。测试编写中修正的均为测试自身问题：压缩预算过小使工具轮超预算（改为 8k 预算 + 一条超长旧消息，恰好一次摘要）；派生章节的上下文检索也会产生 1 次作者向量请求（计入账目）；40001 诱发改用原值写回的原生 SQL（ORM `onupdate` 会改 `updated_at`，使最终投影核验先以 404 失败关闭，而非 40001）；受限收尾需由第二实例执行（同进程共享 AuthorizationRuntime，阻塞发生在进程内锁而非 PostgreSQL，`pg_blocking_pids` 观测不到）。

### 负对照（脚本 `/tmp/e4ig_neg/neg.py`；每次备份到 `/tmp/e4ig_neg_<名>_*`，还原后 `shasum -a 256 -c` 全部 OK）
| 名称 | 打掉的守卫 | 结果 |
|---|---|---|
| N1 | `require_conversation_write` 直接返回 | 1a `[viewer]` 失败（`[revoke]` 由入口私人作用域 404 独立拦截，预期不翻转） |
| N2 | 受限收尾白名单去掉 `agent.turn`、`chapter.generate` | 1b、4b 均失败 |
| N3 | `_collaboration_summary_guard` 直接返回未守卫 provider | 第 2/7 组 12 例全部失败（发送前出现真实 HTTP；发送中变更不再被阻塞） |
| N4 / N4d | 工具 `check_agent_write` + 执行体 `active_grant_required` | 第 3 组 `tool_first` 2 例失败；`revocation_first` 2 例**不翻转**：调试确认轮次在引擎事件提交围栏（E4i-b2，`任务输出授权已变化，已停止`）即终止，早于 runner/工具；多层防御，未再拆引擎围栏（N4b 连同 `_lock_execution` 一起去掉会使 runner 内部错误，属无效负控，已弃用） |
| N5 | `check_agent_write` 直接返回 | 4a、5a `[viewer]` 失败（`[revoke]` 由入口 404 独立拦截） |
| N6 | `preflight_effect_versions` 直接返回 | **不翻转**：`effects.undo` 内行级 after 值比对（`数据已变化，无法撤销`，`undo_conflict`）独立拦截；5c `undo_first` 为锁序观察（断言撤销期间人工编辑被 `pg_blocking_pids` 阻塞） |
| N8 | `execute_change` 协作外部分支 | **不翻转**：协作轮次冻结空工具映射 / `_send_lease` 拒绝带 grant 任务（E4i-b/d 的另外两层）独立拦截，零连接零调用 |
| N9 | `is_serialization_error` 恒 False | 第 8 组失败（变为非 409） |
| N10 | `void_revoked_turn_changes` 直接返回 | 第 9 组失败（提案仍 proposed） |
| N11 | 日志投影 `project_log_payload` 跳过 | E7 账目 3 例失败（回退模式暴露作者配置 ID） |
- 未做破坏性负控的：4c（取消回滚是事务原子性，无可单独去掉的守卫）、5b/5c `undo_first` 与各“持锁先提交”用例的阻塞断言（`not changing.done()` + `pg_blocking_pids` 本身即证明锁存在）。

### 回归（最终代码）
- PG（顺序执行，未与 SQLite 并行，`-rs` 无 skip）：`test_agent_e4ig_postgresql.py` 33（另连跑两次 31/31 稳定）、`test_agent_entries_e4id_postgresql.py` 4、`test_agent_revocation_e4ie_postgresql.py` 4、`test_agent_lock_order_e4ib_postgresql.py` 3、`test_agent_tools_e4ic_postgresql.py` 2、`test_agent_access_e4ia.py -k postgres` 6，全部通过。
- SQLite（按文件，≤3 并行）：`test_agent_e2e_e4ig.py` 6、`test_agent_api.py` 11、`test_agent_handler_e4ib.py` 26、`test_agent_entries_e4id.py` 18、`test_agent_revocation_e4ie.py` 39、`test_agent_writable_e4if.py` 11，全部通过。
- `uv run ruff check .` 通过。全量留给 Gate E，未跑。
- 命令：`cd backend && export AINOVEL_COLLAB_POSTGRES_TEST_URL="$(cat /tmp/collab_runner/pg_url)" && /opt/local/bin/gtimeout 900 uv run pytest -q <文件> -p no:logging -rs`。

### 未验证项
- 真实 HTTP 只覆盖 OpenAI 兼容 SDK（Anthropic / Gemini 的 lease 行为由通用 `test_ai_visibility_dispatch.py` 覆盖，未在 Agent 编辑者路径逐一重跑）。
- 40001 只在编辑者 HTTP 确认路径诱发；runner 后台路径（`run_agent` → `CONCURRENT_CHANGE`）与撤销路径未单独诱发真实 40001。
- 第 6 组的残留外部提案与挂载是人为植入（生产中编辑者轮次无法产生），用于证明多层关闭；未覆盖作者与编辑者同一会话混合外部提案（会话私有，不可能）。
- 浏览器验证不在本片；全量回归留 Gate E。

### 待决问题（附推荐）
1. research §9.2 第 3 项写“管理员兼编辑者降 viewer 后写工具失败且审计只有一条失败”。实际为**零条**审计：授权失效在进入工具 service 的 `AuditOperation` 之前即终止（引擎事件围栏 / 执行体与 `lock_agent_write` 锁后复核），与人工路由“授权拒绝早于审计作用域时不记审计”一致；工具先持锁成功时恰好 1 条 success。**推荐维持**，把清单表述更正为“不产生成功审计、无重复失败”；如需对授权失效也留管理审计痕迹，需新增审计动作，超出 V1（裁决 6 不新增动作）。
2. 并发修改提案行中**投影可见字段**（如 `updated_at`）时，确认在最终投影核验处以 404「修改提案不存在」失败关闭，而非 409 `CONCURRENT_CHANGE`；只有不改投影事实的并发写才走到 40001 → 409。两者都零副作用、可刷新重试。**推荐维持**（投影核验是隐私边界，宁可 404）；前端已按 404/409 统一触发权威重读（E4i-f）。
3. 编辑者经 Agent `start_task` 派生的 `chapter.generate` 在已授权时其上下文检索也使用作者向量（费用 = 作者），与裁决 12 / E6 一致，但 E4i-f 的“外部服务费用由作者账户承担”提示只出现在模型来源为作者时。**推荐**：本人模型 + 作者授权（own_granted）时也提示“检索向量费用由作者承担”，属前端文案小改，留给 Gate E/后续切片确认。

## 复核：E4i-g（2026-11-15，check 代理）

结论：覆盖映射基本真实，9 项均有真实 PostgreSQL 用例，引用的 14 个已有用例都存在。并发用例统一用 Event 加 `pg_blocking_pids` 证明阻塞，阻塞观察之后都有 `assert not work.done()`，不是碰巧串行。第 2 项是真实 SDK（`create_provider` 对接本机 HTTP 服务器）：发送前提交时零请求；发送中提交时，变更被 lease 阻塞到响应头之后，已发出的 1 次请求不撤回，输出也不落库。第 8 项是另一真实连接诱发的 40001，不是 mock。发现 4 处测试强度问题，均已修复，未改产品代码。

产品代码核对：`backend/app` 下最新 mtime 为 07:43（E4i-f），本片两个测试文件为 13:48。没有晚于 E4i-f 的 app、alembic 改动，与“只补测试”的声明一致。

### 发现与修复（只改测试）
1. **E7 投影出口不全**：原测试只查会话日志、会话详情和列表、两个 `/tasks/{id}`。
   - 派生 `chapter.generate` 写入的 `generate` 记录，编辑者可以经章节级生成记录列表和详情读取，原测试没有覆盖。
   - 已补：章节日志列表和详情（`model_source` 正确，`ai_config_id` 回退时为 null）、`/tasks` 列表、两个 `/tasks/{id}/output`、章节版本列表，全部纳入“无作者配置 ID、`_project_model`、`_collaboration_grant`、作者密钥”的检查。
   - 负控 R1：只去掉章节日志出口的投影，`[fallback]` 失败（`('owner', 1) != ('owner', None)`）。
2. **第 6 项残留外部提案没有真正测到 `execute_change` 的协作分支**：
   - 原用例植入的提案所在任务 checkpoint 的 `mcp_tool_map` 为空，执行在 `_execute_external_impl` 的 `entry is None` 处就会失败。所以实现方的 N8 不翻转，并不是因为“另外两层独立拦截”。
   - 已加固：植入真实 `build_tool_map` 映射表，并用 `mcp_tools.preview` 生成一致的预览，使该提案除“协作轮次”外完全可执行。
   - 负控 R2：只去掉协作分支后用例失败。残留提案进入外部执行，`_send_lease` 因 `_collaboration_grant` 与作者条件拒绝真实发送，但留下未知结果屏障，作者删除作品得到 409 `project_tasks_not_stopped`。
   - 结论：`_send_lease` 只拦得住发网，拦不住屏障；避免作者删除被阻挡的只有该协作分支这一层。
3. **第 5 项归因错误**：`preflight_effect_versions` 只校验效果格式、旧字段、分类和可见性版本，不检测人工编辑。挡住“人工改后撤销”的唯一一层是 `effects.undo` 的行级 after 值比对（经 `_raise_conflicts` 抛出 `undo_conflict`）。
   - 负控 R3a：只去掉 `_raise_conflicts`，`manual_first` 即翻转为 200；R3b 两处都去掉，结果相同。
   - 已更正测试 docstring 与注释。研究清单第 9.2 节第 5 项“effects 版本预检”的措辞也应按此理解。
4. **失败原因断言过弱**：第 2/7 组 12 例、第 3 项 4 例只断言 `status == "failed"`。负控 R5 中，轮次以“任务执行失败：服务器内部错误”失败，原用例照样通过。
   - 已收紧为只接受受控的授权失效文案：`AUTH_STOPPED` = `_POLICY_MESSAGES` 中的 task_output_changed 与 permission_denied；`kind=token` 时为系统受限收尾的“任务执行已停止”。
   - E7 中途撤权用例原用“授权”“权限”子串匹配，同样改为固定集合。
   - R5 复跑：两例 `revocation_first` 因 `'任务执行失败：服务器内部错误' not in AUTH_STOPPED` 失败，说明新断言有效。

### 负控（脚本 `/tmp/e4ig_review_neg/neg.py`，备份目录 `/tmp/e4ig_review_<名>_*`，每次还原后 `shasum -a 256 -c` 均 OK）
| 名称 | 打掉的守卫 | 结果 |
|---|---|---|
| R1 | 章节日志 list/get 跳过 `public_logs` 投影 | E7 `[fallback]` 失败 |
| R2 | `execute_change` 中 `if grant is not None` 协作外部分支（加固后） | 第 6 项失败：删除被未知结果屏障 409 |
| R3a / R3b | `_raise_conflicts` 置空（R3b 另加 `preflight_effect_versions` 置空） | `manual_first` 失败（200） |
| R4a / R4 | `AgentHandler.verify_event`（R4 另加 `check_agent_write` 与 `active_grant_required=False`） | 不翻转：`_event_task → require_write_execution` 的 `task_visible_to_actor` 成员可见性与 `owner_ai_allowed` 仍是独立一层（错误文案为 `task_output_changed`） |
| R5 | 再把 `require_write_execution` 整体置空 | 轮次以内部错误失败。这属于无效负控（与实现方 N4b 同类），但它暴露了第 4 条的弱断言，收紧后两例失败 |

第 3 项撤权先提交的多层拦截属实：引擎事件围栏（verify_event）、`require_write_execution`、执行体 grant 复核、`check_agent_write`。实际终止点是引擎事件提交围栏。

### 测试结果（最终代码）
- PG（顺序执行，未与 SQLite 并发，`-rs` 无 skip）：
  - `test_agent_e4ig_postgresql.py` 共跑 4 次（基线 1 次、修改后连跑 2 次、修行宽后 1 次），每次 33 passed。
  - 哨兵全部通过：`entries_e4id_postgresql` 4、`revocation_e4ie_postgresql` 4、`lock_order_e4ib_postgresql` 3、`tools_e4ic_postgresql` 2、`access_e4ia -k postgres` 6。
- SQLite：
  - `test_agent_e2e_e4ig.py` 3 路并行 × 5 批，共 15 次，每次 6 passed，无偶发失败。
  - 哨兵（≤3 并行）全部通过：`agent_api` 11、`handler_e4ib` 26、`writable_e4if` 11、`entries_e4id` 18、`revocation_e4ie` 39、`collaboration_log_private_views` 17。
- `uv run ruff check .`：All checks passed。全量回归未跑，留给 Gate E。

### 对 3 个待决问题的意见
1. **审计条数**：同意维持零条。实测终止点是引擎事件围栏（`task_output_changed`），根本没有进入工具 service 的 `AuditOperation`。这与规范“授权拒绝早于审计作用域时不记审计”一致，也不违反裁决 6。清单措辞应改为“授权失效时不产生成功审计，也不产生重复失败审计；工具先持锁成功时恰好 1 条 success”。
2. **并发修改可见字段时返回 404 而非 409**：同意维持。投影核验是隐私边界，宁可 404 失败关闭。两条路径都零副作用、可重试，前端（E4i-f 恢复控制器）对 404 和 409 都会触发权威重读。建议 Gate E 在 agent.md 的错误矩阵里补一句“确认期间提案的可见投影被并发改动 → 404，刷新后重试”，避免后人当作缺陷“修成 409”。
3. **own_granted 时向量费用由作者承担但没有提示**：E7 已实测，own_granted 下 2 次向量请求都走作者的 `owner-embed`（工具检索 1 次、派生章节上下文检索 1 次）。这是对作者的真实计费，属费用透明问题，建议升级为 Gate E 的必做项，不作为可选：只要 `allow_owner_ai` 为真且作品共享索引使用作者向量，就提示“知识检索向量费用由作者账户承担”，不论文本模型来自谁。只改前端文案。

### 遗留
- 实现方列的未验证项仍成立：真实 HTTP 只测了 OpenAI 兼容 SDK；runner 后台路径和撤销路径没有单独诱发 40001。
- “持锁先提交”类用例对 HTTP 状态码使用 `in (2xx, 404, 409)`。原因是提交后的响应投影与撤权提交之间存在合法竞争，结果不确定。这些用例同时断言了数据库事实（任务、提案状态、成员状态、正文），可以接受。

## 进度：E4i-g2（2026-11-15，裁决 31 作者向量费用提示；仅前端）

- `utils/kb.ts` 新增 `AGENT_OWNER_MODEL_COST` / `AGENT_OWNER_VECTOR_COST` / `AGENT_OWNER_MODEL_VECTOR_COST` 与纯函数 `agentOwnerCostNotice({collaborator, modelSource, status})`：向量判定只用 `collaboratorKbAccess(status).vector`；模型来源为作者时向量合并为一行、否则沿用原文案；本人模型 + 向量显示“知识库检索将使用作者共享的向量模型，向量费用由作者承担。”；作者、关键词、状态未知 / 读取失败均不显示。
- `AgentConversationView` 复用 `useIndexStatus`（与知识库页同一 `/kb/index-status` 读取，无新接口），`enabled = collaborator && writable && !historySealed`（查看者、只读 / 封存会话、作者不请求），`foreground: true`，`revalidateKey` 与 KnowledgePanel 同式（作品 ID 匹配时的 authorization_version + allow_owner_ai）；旧作品 / 旧轮次响应经 `currentIndexStatus` 键比对丢弃。AgentPanel 为懒加载 chunk，不影响入口 chunk 约束。
- 测试：`utils/kb.test.ts` 新增 5 例（作者不显示、协作者+向量+本人模型、关键词 / 未知不显示、作者模型合并、旧键 / 关闭不生效）；`components/agent/agentUI.test.ts` 新增 1 例渲染（含查看者 / 作者 enabled=false）。受影响 vitest（kb、agent 组件、useIndexStatus.access、agentCollaborator）全部通过；`npm run lint`、`npm run build` 通过。全量留 Gate E。
- 规范：component-guidelines「协作者模式（E4i-f）」补一句。

## 复核：E4i-g2（2026-11-15，check 代理）

- 结论：通过。判定只依赖服务端 index-status（`collaboratorKbAccess(status).vector`）；`useIndexStatus` 在早返回之前无条件调用，无 hooks 顺序问题；`enabled = collaborator && writable && !historySealed`，作者 / 查看者 / 只读 / 封存会话不发请求；读取失败或未知时 `status` 为 undefined、不显示；旧作品 / 旧轮次 / allow_owner_ai 变化经 `currentIndexStatus` 键比对丢弃，授权轮次变化另由面板整体重挂；卸载时读取 effect（alive 标志）与 `startVisiblePolling` 清理完整。文案与裁决 31 一致；作者模型来源时合并为一行无重复；`modelSource` 为 loading/none 时只按向量判定，合理。`accessKey` 与 KnowledgePanel/MemoriesPanel 同式。
- 轮询（待决 1）：`foreground: true` 下仅在 `indexing` 或本人有活动 `kb.index` 时、页面可见才 3 秒轮询，回前台刷新一次，结束即停。同意该做法：Agent 输入区只需显示费用来源，建索引期间检索模式可能变化，可见期轮询代价低；不建议在非建索引时常驻轮询。
- 补测：`agentUI.test.ts` 增加封存会话（writable 仍为 true）`enabled=false` 及可写协作者 `foreground: true`、`revalidateKey` 为字符串的断言（原测试未覆盖 `!historySealed` 分支）。规范补“封存会话不读、foreground 下仅建索引中且可见时轮询”。
- 负控（备份 /tmp/e4ig2nc，恢复后 `shasum -a 256 -c` 两文件 OK）：①去掉 `!historySealed` → 新增断言失败（1 failed）；②`agentOwnerCostNotice` 去掉 `collaborator &&` → kb.test「作者本人不提示向量费用」失败。
- 验证：vitest（kb、agentUI、hooks）5 文件 70 passed；`npm run lint`、`npm run build` 通过；构建产物中 kb 文案只在 `kb-*.js`，入口 `index-*.js` 静态 import 与 index.html 预加载均不含 kb chunk（仅出现在动态导入依赖映射）。
- 观察（不阻塞）：index-status 稳态只在挂载 / 任务版本 / 键变化时重读，作者在同一授权轮次内改坏向量配置后提示可能滞后到下次重读（与知识库页一致，仅文案）；撤权恢复（同轮次）重新启用时会先短暂沿用同键旧状态直到重读返回，同样仅文案影响，未改共享 hook。

## Gate E 后端全量回归（2026-11-15）

只运行测试并记录证据，未修改产品代码与测试。日志：`/tmp/gate_e/`（`ruff.log`、`sqlite/<文件>.log`、`pg/<文件>.log`、各自 `_status.txt`）。日志中已核对不含 PG 连接串。

### 1. Ruff
- `cd backend && uv run ruff check .` → exit 0，`All checks passed!`

### 2. SQLite 全量
- 命令：`backend/tests/test_*.py` 排除 `*_postgresql.py`，共 364 个文件；每文件 `/opt/local/bin/gtimeout 600 uv run pytest <file> -q -p no:cacheprovider -rs`，`xargs -P 3` 并行。
- 耗时：墙钟 2703s（约 45 分钟）；无超时（单文件最长 188s：`test_collaboration_stream_early_subscribe_e5e`）。
- 总数：**6369 passed / 2 failed / 0 error / 40 skipped / 19 xfailed / 0 xpassed**；363 个文件 exit 0，1 个文件 exit 1。
- 失败（串行重跑仍失败 → **稳定失败**，非偶发）：`tests/test_setting_ai_visibility_migration.py::test_0037_preserves_real_0036_rows_and_schema_then_safe_roundtrip[False]` 与 `[True]`。
  - 最小复现：`cd backend && uv run pytest tests/test_setting_ai_visibility_migration.py -q -p no:cacheprovider`（2 failed, 2 passed, 1 skipped）。
  - 断言：`tests/test_setting_ai_visibility_migration.py:110` `AssertionError: agent_conversations`，0037 迁移后的真实表结构 ≠ `_schema_0037_model_contract(当前模型)`。
  - 初步判断：测试冻结契约漂移，非迁移缺陷。本分支新增迁移 0053（`agent_conversations.summary_source`）与 0054（`agent_conversations.collaboration_invitation_id`），模型 `app/models/agent.py` 已加这两列；`tests/test_setting_fields_migration.py::_schema_0037_model_contract` 只剔除了 0038/0039 的增量，未剔除这两列，因此按当前模型生成的"0037 合同"多出两列。修复方向（需开发者决定）：在冻结契约辅助函数中对 `agent_conversations` 去掉 `summary_source`、`collaboration_invitation_id`。
- SQLite 阶段 skip 均为"未设置某隔离 PG 环境变量"的 PG 分支门控（SQLite 阶段未注入任何 PG URL），包括：`AINOVEL_FIELDS_POSTGRES_TEST_URL`（ai_visibility_dispatch、card_ai_visibility、category_cards/transfer、setting_* 等）、`AINOVEL_AGENT_POSTGRES_TEST_URL 或 AINOVEL_COLLAB_POSTGRES_TEST_URL`（agent_access_e4ia、agent_audit_coordinator、agent_authenticated_actor）、`AINOVEL_COLLAB_POSTGRES_TEST_URL`（audit_authorization_no_autoflush、collaboration_access_no_autoflush、collaboration_empty_migration）、`AINOVEL_POSTGRES_TEST_URL`（test_migrations）。
- 已知 ai_visibility_dispatch PG 分支 skip：**仍在**。`tests/test_ai_visibility_dispatch.py` 1 skip，原因"设置 AINOVEL_FIELDS_POSTGRES_TEST_URL 才运行隔离 PostgreSQL 验证"；该测试不认 `AINOVEL_COLLAB_POSTGRES_TEST_URL`，且不在 `*_postgresql.py` 中，PG 阶段也不会覆盖。skip 不计为通过。

### 3. PostgreSQL 全量
- 命令：SQLite 全部结束后开始，`AINOVEL_COLLAB_POSTGRES_TEST_URL` 由 `/tmp/collab_runner/pg_url` 注入（未打印/未落盘），顺序逐个运行 108 个 `backend/tests/*_postgresql.py`，每个 `gtimeout 900 uv run pytest <file> -q -p no:cacheprovider -rs`。
- PG 版本：`PostgreSQL 16.15 on aarch64-unknown-linux-musl, compiled by gcc (Alpine 15.2.0) 15.2.0`。
- 耗时：墙钟 1727s（约 29 分钟），无超时。
- 总数：**549 passed / 1 failed / 0 error / 96 skipped**；107 个文件 exit 0，1 个 exit 1。
- 失败（串行重跑仍失败 → **稳定失败，环境类**）：`tests/test_ai_config_instance_migration_postgresql.py::test_0049_postgresql_full_backup_restores_instance_keys`，`Failed: 真实 PostgreSQL 备份测试需要 PATH 中的 pg_dump 与 pg_restore`（line 211 主动 `pytest.fail`）。本机 `which pg_dump/pg_restore` 均未找到。属基础设施缺失，不是断言失败；需在 PATH 提供 PG16 客户端工具后重跑才能证明该 AC。
- skip：5 个文件整文件 skip，合计 96 条，**全部未执行，不算通过**：
  - `test_rich_document_postgresql.py` 85 skipped —— 需 `AINOVEL_SCENES_POSTGRES_TEST_URL`
  - `test_scene_ai_postgresql.py` 5 skipped —— 需 `AINOVEL_SCENES_POSTGRES_TEST_URL`
  - `test_scenes_postgresql.py` 2 skipped —— 需 `AINOVEL_SCENES_POSTGRES_TEST_URL`
  - `test_series_postgresql.py` 1 skipped —— 需 `AINOVEL_SERIES_POSTGRES_TEST_URL`
  - `test_setting_fields_postgresql.py` 3 skipped —— 需 `AINOVEL_FIELDS_POSTGRES_TEST_URL`
  - 原因均为这些文件只识别各自领域的隔离 PG 变量，不识别 `AINOVEL_COLLAB_POSTGRES_TEST_URL`；按约束本轮只注入了 COLLAB URL。
- 协作相关 PG 文件（`test_collaboration_*_postgresql.py` 等）全部通过、无 skip。

### 结论
Gate E 后端回归**未全绿**：2 处稳定失败（1 处 SQLite 迁移契约测试漂移、1 处 PG 环境缺 pg_dump/pg_restore），另有 PG 96 条 + SQLite 40 条因环境变量门控 skip 未证明。

## Gate E 补充（2026-11-15，主会话）：失败修复与 skip 补跑

- **SQLite 稳定失败已修**：`test_setting_ai_visibility_migration.py::test_0037_preserves_real_0036_rows_and_schema_then_safe_roundtrip[False/True]`。原因是 `tests/test_setting_fields_migration.py::_schema_0037_model_contract` 按当前模型生成 0037 冻结结构，未排除 0053 `summary_source`、0054 `collaboration_invitation_id`；已在该函数对 `agent_conversations` 排除这两列（仅改测试，迁移无误）。3 个迁移测试文件重跑全绿。
- **PG `test_0049_postgresql_full_backup_restores_instance_keys`**：缺 `pg_dump/pg_restore`。Homebrew `libpq` 为 18.6，`pg_restore` 发出 PG16 不认识的 `SET transaction_timeout` 而失败；改用本机 `libpq@16`（16.15）放入 PATH 后 5 passed。
- **skip 补跑**（全部真实执行，0 skip）：测试自带库名/用户守卫且均在随机 schema 内建表并删除。
  - 字段：本机测试容器 `ainovel-fields-test-*`（field_test/fields_verify）。
  - 场景 / 系列 / `AINOVEL_POSTGRES_TEST_URL`：在同一测试容器内新建专用角色与库 `scenes_test/scenes_verify`、`series_test/series_verify`、`ainovel_test/ainovel_test`（随机口令，连接串存 `/tmp/gate_e/pg_extra/.url_*`，权限 600，未打印）。
  - Agent 相关普通文件不设 `AINOVEL_AGENT_POSTGRES_TEST_URL`，回落共用 `collab_test`。
  - 结果：rich_document 85、scenes 2、scene_ai 5、series 1、setting_fields_postgresql 3、ai_config_instance_migration 5、ai_visibility_dispatch 20、setting_ai_visibility_migration 5、setting_fields_migration 14、setting_categories_migration 20、agent_access_e4ia 37、agent_audit_coordinator 22、agent_authenticated_actor 26 + 1 xfailed（既有 strict xfail：自动 MCP 外部调用）、audit_authorization_no_autoflush 11、category_cards 12、card_ai_visibility 19、collaboration_access_no_autoflush 10、category_transfer 15、collaboration_empty_migration 2、migrations 28、setting_ai_context 13、setting_categories 41、setting_category_agent 69、setting_category_query 23，全部通过。日志 `/tmp/gate_e/pg_extra/r2_* r3_* r4_*`。
- **前端**：`npm run lint`、`npx vitest run`（219 文件 / 1556）、`npm run build` 通过。
- **结论**：Gate E 后端 + 前端全量通过；SQLite 阶段 PG 分支与 5 个整文件跳过的 PG 文件均已在真实 PostgreSQL 16.15 补跑，无剩余 skip（仅既有 strict xfail）。
- 未完成：Agent / E6 协作者 UI 浏览器实测（作者、grant 编辑者、非 grant 编辑者、viewer；含裁决 31 费用提示三种情况）。

## 浏览器验证：2026-11-15 E4-i Agent 协作者 + 裁决 31（主会话）

环境：沿用 /tmp/e5g_browser（SQLite 隔离库，启动时自动升级 0052→0054）；假服务 `fake_ai.py` 新增流式工具调用脚本（同一用户消息后首轮发 `search_knowledge` + `update_chapter_meta`，有工具结果后吐正文；`/calls` 计数；原版备份 `fake_ai.py.bak_e4ig`）。作者新增假向量配置并上传 KB 文档 → 作者检索为 vector。账号 e5g_owner / e5g_editor / e5g_viewer；成员权限由作者经 PATCH collaborators 实时修改。

通过：
- **编辑者 + 作者授权 + 无本人模型**：Agent 入口可见；输入区显示合并文案「本会话的模型调用、知识库检索向量及确认启动的任务将使用作者授权的模型，外部服务费用由作者账户承担。」；发送后轮询展示检索完成 → 改名提案「待确认」→ 确认后「已执行」并续跑出字；章节 1 标题实际改写。
- **编辑者 + 本人文本模型 + 作者授权 + 向量**：只显示「知识库检索将使用作者共享的向量模型，向量费用由作者承担。」（裁决 31）。
- **作者本人**：无费用提示，发送后输入清空。
- **作者关闭 allow_owner_ai（页面不刷新）**：进行中待确认的授权轮次即时显示「协作授权已失效，任务已停止」，提案「已作废」；费用提示消失；模型选择切到本人模型。「重试本轮」被拒后按钮消失并提示「不可重试，请在当前会话重新发起」（裁决 24）。用本人模型新一轮正常（检索 + 提案）；editor index-status 为 keyword / owner_embedding_not_allowed，假服务向量计数不增加（零作者向量调用）。
- **降为 viewer（页面不刷新）**：顶部「当前为查看者」；会话标「（只读）」，新建 / 改名 / 删除禁用；输入区与确认 / 拒绝消失，显示「此会话仅供查看」；待确认提案作废，章节 3 标题未写入。知识库页只读（无上传、设置禁用）。
- **恢复编辑者 + 授权**：知识库页即时出现「使用作者共享的向量模型，费用由作者承担」上传区，Agent 输入区与费用提示恢复。
- **纯 viewer 账号**：Agent 入口可见，面板提示「只能查看本人已有的智能体会话，不能新建会话或发送消息」，无输入区 / 新建。
- **作者成员面板**：修改权限弹窗含「修改或撤销该成员的权限后，会立即中止其进行中的 AI 任务（包括智能体对话），已提交的内容会保留。」。

发现（未修）：
1. 编辑者首次在新会话发送后输入框一次未清空（`act` 返回 false 时不清空）；随后 3 次（含新会话首条）均正常清空，未能复现，原因未定位。
2. 授权失效作废的轮次中，工具步骤仍显示「修改章节标题 · 处理中」（提案卡已正确显示「已作废」），仅显示问题。
3. 恢复为同一邀请轮次的编辑者后，被作废轮次上「重试本轮」重新出现（按通用重试规则，后端最终裁决）。
未在浏览器验证：管理员兼协作者路径；编辑者无授权时知识库页「仅关键词」文案（单测覆盖，API 已确认 keyword_reason）。

## 进度：E4i-h（2026-11-15，浏览器验证问题 1、2；仅前端）

### 问题 1：发送后输入框偶发未清空
- **根因分析**：`AgentConversationView` 只在 `agent.send` 返回 true 时清空；旧 `act` 在 `agentApi.send` 已成功（服务端已受理、轮次已建）之后仍有三类 false 出口：① 受理后的 `refresh()` 被并发读取取代（`requestSeq` 变化 → `AgentReadSuperseded`），还会误提示「作品权限已变化，请重新读取对话。」；② 受理后的重读失败（网络 / 5xx，SQLite 下轮次立即开跑时 GET 详情可能撞写锁）；③ 受理后 `current()` 失效（卸载 / 作用域变化）。浏览器记录 scopeVersion 前后未变，排除了 ③ 中的作用域分支（`invalidateRequestGeneration`、权限变化均会推进 scopeVersion），最可能为 ①（协作者无活动轮次时 `startAgentTurnPolling` 仍挂 visibilitychange 刷新，自动化浏览器切页 / 截图可触发；`setting-ai-visibility` 失效也会调 `refreshSafe`）或 ②。未能在浏览器复现唯一根因，按「受理即清空」语义统一修复。
- **改动**：`useAgentConversation.ts` 抽出导出纯函数 `runAgentAction(deps) -> {accepted, ok}`：请求成功即 `accepted=true`；受理后重读被取代时最多再读两次（保证已执行修改的资源失效与列表刷新照常），仍被取代或卸载时静默；其余重读失败照常提示。`act` 改用它，`send` 返回 `accepted`；`change / turn / cancel / retry / update` 经 `run` 仍返回 `ok`（行为不变，仅受理后被取代不再误报）。被拒绝 / 请求失败仍返回 false、保留输入，`withAgentRefresh` 拒绝后重读一次且不重放不变。
- **测试**：新增 `components/agent/useAgentAction.test.ts`（7 例：正常、受理后被取代并重读成功、受理后重读失败、受理后作用域失效、卸载致取代、服务端拒绝不重放、连续取代放弃）。修复前先以旧语义实现同签名函数运行：**5 failed | 2 passed**；修复后 7 passed。

### 问题 2：已作废轮次工具步骤仍「处理中」
- **根因**：`AgentTimeline` 对无结果（`ok === undefined`）的工具步骤一律显示「处理中」，未看轮次是否结束；授权失效作废 / 失败 / 取消 / 中断的轮次里，待确认提案对应工具没有 tool 结果消息，于是残留。作者路径同样存在。
- **改动**：`utils/agent.ts` 新增 `AGENT_TOOL_STOPPED = '已停止'` 与 `agentToolStepState(step, openTurn, live)`：有结果按「完成 / 未完成」；无结果时仅所属轮次实例仍未结束（`isAgentTurnOpen`，按 id + task_key）或正在实时订阅该步骤（`running` 且在 live steps 中）才「处理中」，否则「已停止」，说明文字改为「本轮已结束，此步骤没有返回结果。」。`buildAgentTimeline` 为历史步骤补上 `turn_task_id / turn_task_key`。运行中 / paused 轮次及作者实时步骤显示不变。
- **测试**：`utils/agent.test.ts` 增状态判定与历史身份 2 例；`agentUI.test.ts` 增 SSR 时间线 1 例（failed / cancelled / 无活动轮次 → 「修改章节标题 · 已停止」且提案「已作废」；running / paused / 实时步骤 → 「处理中」）。修复前（临时让 helper 恒返回处理中）**3 failed**，还原后通过。

### 验证（frontend）
- `npx vitest run src/components/agent src/utils/agent.test.ts`：5 文件 / 61 passed。
- `npm run lint`、`npm run build` 通过。未跑全量 vitest（按精简回归策略）。
- 规范：`component-guidelines.md` 协作者模式段补「发送受理语义」与「工具步骤已停止」两句。

### 待决
- 问题 1 唯一根因未在浏览器确认；建议下次浏览器验证时留意发送后是否出现「作品权限已变化」或网络错误 toast。
- 浏览器发现 3（恢复同轮次编辑者后作废轮次重新出现「重试本轮」）未处理，仍按后端最终裁决。

## 复核：E4i-h（2026-11-15，check 代理）

结论：通过，仅补 2 例测试，未改实现代码。

- 语义：`runAgentAction` 只有 `operation` 成功返回才置 `accepted`；早退（busy / lost / 不可写 / 作用域失效）、服务端拒绝、网络失败均为 `accepted=false`，输入保留。`withAgentRefresh` 拒绝后重读一次、不重放，且始终重抛原错误，所以拒绝路径即使重读被取代，`fail` 收到的仍是原 403/404/409，`isAgentAccessLost` → `createAgentAccessRecovery.markLost` 未被绕过；`AgentRetryRejected` 解包 original、历史封存 `setSealedError` 不变。`AgentReadSuperseded` 只在 `requestSeq` 变化或卸载时抛出，不携带权限信号；真正的权限变化走 `isProjectScopeCurrent` 失败（普通 Error）或 404/409 permission_denied，均不被静默。重读重试每次都推进 `requestSeq`，`refresh` 只应用最新序号结果，不会旧覆盖新。`busy` / `busyRef` 仍在 finally 复位；`invalidate` / `onUpdated` / 成功提示仍只在 `settle` 中。change / turn / cancel / retry / update 经 `run` 返回 `ok`，唯一变化是受理后被取代不再误提示。
- 工具步骤：`isAgentTurnOpen` 同时比对 id + task_key 与 `isOpenTask`，重试产生的新任务、同整数 ID 不同 key 的旧轮次均判为已停止；paused 属于 open，待确认步骤仍「处理中」；作者实时步骤由 `running && steps 命中(call_id+id+key)` 保持「处理中」；历史无身份（旧数据）步骤无结果时显示「已停止」，可接受。
- 补测：`useAgentAction.test.ts` 新增「拒绝后重读被取代仍把原 409 permission_denied 交给失败处理」「受理后重读 404 仍算受理并交失败处理」。
- 负控（备份 /tmp/e4ih_check，恢复后 `shasum -a 256 -c` 三文件 OK）：① 去掉受理静默 + 重试：4 failed；② 仅去掉重读重试：2 failed；③ `agentToolStepState` 恒为处理中：3 failed；④ 历史步骤不带轮次身份：3 failed；⑤ AgentTimeline 去掉实时步骤判定：1 failed。
- 验证（frontend）：受影响 5 文件 / 63 passed；`npm run lint`、`npm run build` 通过；全量 `npx vitest run` 220 文件 / 1576 passed。
- 待决意见：连续 3 次被取代后放弃时不调用 `settle`，确认类动作的资源失效会依赖随后的轮询 / 订阅收尾，边界可接受，记录即可。问题 1 唯一根因仍需下次浏览器验证留意；发现 3 维持后端裁决。

## 进度：H 收尾文档（2026-11-15）

本轮只修改文档，没有改动产品代码或测试，也没有运行测试。前端 lint/build 由主会话在提交前重新运行。

### 修改的文件
- `README.md`
  - 新增顶层章节「作品协作」，包含以下小节：邀请与成员管理、角色与权限边界、系列设定协作、模型与费用、智能体协作、知识库与向量索引、备份与数据边界、数据库迁移。
  - 同步更新目录、功能概览（新增「作品协作」行，并修改「系列作品与共享设定」行）、本地开发中的迁移说明（`0044`–`0054`）、「系列作品与共享设定」（原文写的是“共享库仅作者本人管理”）、「普通用户」、「管理员作品管理」（补充管理员兼协作者的说明）、「作品级备份与迁移」（备份不含协作关系，且只导出作者本人的生成记录）、常见问题「知识库只使用关键词检索」，以及当前限制（新增“协作不是实时共写”）。
  - 目录结构没有新增顶层目录，因此不作修改。
- `TODOS.md`：第 11 项改为已实现，三个子项全部勾选，并补充验收记录和未完成项。
- 本文件：勾选 A–H 清单，为未勾选项在行尾注明原因，并追加本节。

### 勾选依据
- D2：`test_cross_user_isolation` 等原有严格断言在 Gate E 全量中通过；风格隐私证据见 `research/phase-e-style-privacy.md`、`phase-e-agent-style-privacy.md`；`test_collaboration_content_access.py::test_global_style_is_visible_and_cross_project_ids_are_rejected`。
- D3：「E4-f 章节协作入口」至「E4-g3c 向导协作入口」、「E6-c 编辑者向量入口开放」、「E4i-d Agent HTTP 入口对协作者开放」，以及「复核结论：2026-11-13 E5-g」第一条（各 AI 入口在后端均按 `ai.use` 开放，viewer 403）。封面见 `research/phase-e-cover-ai.md`。
- D4：`test_collaboration_planning_access.py::test_mentions_only_include_the_collaborators_own_chat`、`test_collaboration_story_access.py::test_single_book_collaborator_does_not_see_series_identity`，以及 `research/phase-f-series-review.md`。
- D5：`test_collaboration_content_access.py::test_chapter_reader_version_and_export_access_matrix`（覆盖三种格式的导出、阅读进度、备份 404）、`::test_editor_cannot_recycle_or_cancel_owners_running_task`、`test_collaboration_invites.py::test_readonly_admin_keeps_existing_write_and_recycle_errors`。
- E1–E7（含 E5a）：「E4（a~d）」至「E4i-g」的各条进度与复核记录（E5a 见「E4i-a」；E6 见「E6-a」至「E6-f」；E7 见「E4i-g」中的 E7 端到端及其复核），以及「Gate E 后端全量回归」和「Gate E 补充」的结论：后端和前端全量通过，PG 无剩余 skip。
- F1–F5：`research/phase-f-series.md`、`research/phase-f-series-review.md`。其中尚未执行的 `test_collaboration_series_read_race_postgresql.py`、`test_collaboration_series_postgresql.py` 已在「Gate E 后端全量回归」第 3 节通过（协作相关 PG 文件全部通过、无 skip）；`test_series_postgresql.py` 已在「Gate E 补充」中补跑通过。
- G1–G4：`research/phase-g-access.md`、`phase-g-invitations.md`、`phase-g-project-ui.md`、`phase-g-integration.md`，以及「E5-g」「E5-g2」「E6-e」「E4i-f」「E4i-g2」的进度与复核记录。浏览器验证见「浏览器验证：2026-11-13 E5」和「浏览器验证：2026-11-15 E4-i」（含知识库费用提示与只读）。
- H1：本轮完成 README 和 TODOS；规范文件已在各子阶段同步（见工作区 `.trellis/spec/` 的改动）。
- H2：「Gate E 后端全量回归」中的 Ruff 和 SQLite 全量，加上「Gate E 补充」修复 1 处稳定失败后的结果，以及前端 lint、Vitest、build；E4i-h 之后，「复核：E4i-h」中的全量 Vitest 220 个文件 / 1576 个用例、lint 和 build 均通过。E4i-h 之后后端没有改动。
- H3：「Gate E 后端全量回归」第 3 节（PostgreSQL 16.15，108 个文件），以及「Gate E 补充」按各领域变量补跑（scenes、series、fields、`AINOVEL_POSTGRES_TEST_URL` 等），无剩余 skip。

### 仍未完成
- A1：最终代码树上的 479 个公开操作尚无逐项闭合记录（`research/phase-a-reconciliation.md` 中 209 个静态分类项仍待核对）。
- D1：依赖 A1，缺少逐项对照。
- G5：系列设定协作者和管理员兼协作者两条路径在最终代码上未经浏览器验证（2026-11-10 的抽查基于早期快照，记录中已注明不能替代最终验证）。作者、viewer、editor 的两轮浏览器验证均早于 E4i-h 的智能体前端修改，修改部分只有单元测试覆盖。
- 复核修正（check 代理）：TODOS 原写「浏览器验证使用的是最终代码」，与上述时间顺序不符，已改为注明 E4i-h 之后未复验；README「系列成员」面板改为界面实际标题「系列设定协作者」。
- H4：部署、旧任务排空、迁移和回滚均未演练。
- 其他已知遗留（不对应单独勾选项）：
  - 剩余 strict xfail 为平台 Key 动作身份、SQLite 用户 ID 复用和作者自动 MCP 外部调用，均不属于协作开放范围。
  - 「浏览器验证：2026-11-15」的发现 3（恢复同一轮次后，已作废轮次的“重试本轮”按钮重新出现）按后端裁决维持现状。
  - E4i-h 问题 1 的根因尚未在浏览器中确认。
  - 协作审计暂无查询界面。
  - prd.md 的验收标准勾选框本轮没有改动。
