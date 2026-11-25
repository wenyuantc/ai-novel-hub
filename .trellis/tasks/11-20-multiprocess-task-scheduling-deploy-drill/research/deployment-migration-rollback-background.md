# Research: 协作相关迁移的部署、旧任务排空与回滚——背景调研

- **Query**: TODOS 第 25 项第二个待办「演练协作相关迁移的部署、旧任务排空与回滚，并记录操作步骤」当初指什么、现有文档写了哪些要求、迁移能否降级、现有可复用设施
- **Scope**: internal（只读调研；未运行 alembic upgrade/downgrade、未启动 Docker、未改产品代码）
- **Date**: 2026-11-20
- **路径约定**: `A/` = `.trellis/tasks/archive/2026-11/11-09-novel-collaborators/`；`R/` = `A/research/`；`SPEC/` = `.trellis/spec/backend/`；`V/` = `backend/alembic/versions/`

标注说明：「已核实」= 直接读到的代码或文档原文；「有测试佐证」= 仓库现存测试断言了该行为；「推断」= 由代码语义推出、没有找到实测记录。

---

## 1. 出处：这句话当初指什么

### 1.1 TODOS 原文

| 位置 | 原文要点 |
|---|---|
| `TODOS.md:134` | 第 11 项状态：已实现；「新增迁移 0044–0054」 |
| `TODOS.md:140` | 第 11 项验收记录末尾：「**尚未完成：** 部署、旧任务排空和回滚未演练。」 |
| `TODOS.md:312` | 第 25 项状态：未开始。「任务调度只支持单进程，多个 Uvicorn worker 或多个实例会把遗留的运行中任务误标为中断；协作功能的部署、旧任务排空和回滚未演练（第 11 项、README「当前限制」）。」 |
| `TODOS.md:314-315` | 两个待办：多 worker/多实例调度；演练协作相关迁移的部署、旧任务排空与回滚并记录操作步骤 |

### 1.2 直接来源：实施计划 H4（唯一未勾的交付门禁）

- `A/implement.md:96`：「- [ ] H4 按设计验证旧任务排空、数据库迁移、服务部署和回滚。旧服务未具备多用户日志隔离时不可直接回退；不以删除协作数据绕过降级保护。（未勾：部署、旧任务排空、迁移与回滚均未演练。）」
- `A/implement.md:2377`：「H4：部署、旧任务排空、迁移和回滚均未演练。」
- `.trellis/workspace/aiNovel/journal-1.md:1505`（Session 45，2026-11-15）：「清单 33/37 勾选（A1、D1、G5 浏览器、H4 部署演练未完成）」。日志中 E4/E5 阶段没有其他部署/排空/回滚记录（只有这一条）。

所以 TODOS 里的那句话就是 H4；H4 里的「按设计」指 `A/design.md` §3.3。

### 1.3 设计要求：`A/design.md` §3.3「降级与部署」（`design.md:84-89`，全文四条）

1. `design.md:86`：任一协作表或协作审计表有数据时**拒绝有损降级**；存在触发者不是作品作者或触发者为 null 的生成日志，也拒绝删除归属字段。报错说明原因，不静默清表。
2. `design.md:87`：迁移覆盖空库、已有库和 SQLite/PostgreSQL；旧作者日志回填、用户删除、项目彻删、外键、约束与降级均有测试。
3. `design.md:88`：旧服务会按作品返回**全部**生成日志，数据库保留新列并不能让旧服务安全读取多用户日志。**回滚时须先停用访问，确认旧服务已具备日志隔离兼容补丁；否则不能部署旧服务。禁止通过只撤前端入口或直接清表声称已安全回滚。**
4. `design.md:89`：**不允许旧版和新版写入进程混跑：先排空旧任务/流，再升级数据库与服务，最后开启入口。**

对任务提出的三个具体问题，设计给出的答案：

- **是否停机升级**：是。`design.md:89` 规定顺序为「排空 → 升级数据库与服务 → 开启入口」，且禁止新旧写入进程混跑。`R/phase-e-ai-config-identity-0049.md:9` 写得更具体：「部署前停止新受理并排空旧执行体与外呼，做整库备份，再迁移并仅启动相应新版本。」
- **旧任务（旧代码创建、缺少新身份字段）如何处理**：三者中是「**要求先排空 + 新代码 fail-closed，不回填、不补签**」。
  - 不回填：`design.md:135`「作者任务不要求协作身份，存量作者任务保持兼容」，但迁移不为旧任务补身份——`R/phase-e-ai-config-identity-0049.md:6`「不改变旧任务输入，也不从当前配置为旧 v1 任务补签」；`V/0047_agent_change_identity.py:84`「旧任务整数 FK 即使当前有 UUID，也无法证明它就是原来创建消息/提案的任务：不回填」。
  - fail-closed：`R/phase-e-chapter-claim-design.md:84`「旧 queued/running 即便已有 Task UUID，也没有章 claim 的历史对应证据……若无法唯一证明……应失败关闭，保留原正文与状态并提供明确人工核对／复位路径」；`R/phase-e-index-core.md:18`「`queued_at IS NULL` 的旧任务及缺失身份在此原语中不认领；排空旧进程后应由明确的新受理流程创建新任务，不能把后补 UUID 伪装成原占用」；`R/phase-e-engine-fencing.md:16`「旧数据 `execution_key` 或文档 claim 为 NULL 不会通过补发统计 UUID 认领。旧无文档 claim 的排队任务可由作者取消并重试」；`R/phase-e-ai-guard.md:46`「旧作者任务要在已排空旧执行器后走可证明的兼容/重新受理路径」。
  - 唯一有专门清理路径的旧任务：0045 之前入队的知识库文档索引任务，见 `design.md:234`（E4-e 裁决）与本文件 §3.4。
- **回滚时数据如何处理**：不靠 `alembic downgrade` 清数据，而是**停机 + 恢复升级前的整库备份 + 启动与该 schema 匹配的旧代码**。`R/phase-e-ai-config-identity-0049.md:9`：「回滚前同样停机、备份并核对依赖；整库恢复时先停止快照之后的全部旧进程，再恢复同一快照并启动与其 schema 匹配的代码。」`README.md:552` 同义（见 §4.1）。`A/implement.md:96` 追加两条禁令：旧服务未具备多用户日志隔离时不可直接回退；不以删除协作数据绕过降级保护。

### 1.4 「兼容单元」约束（中间态不可部署）

- `A/implement.md:5`：「迁移、后端能力、日志隐私和客户端必须作为兼容单元部署；中间阶段仅在隔离环境验证，不能只关闭前端入口后对外运行不完整的授权实现。」
- 同类表述：`A/implement.md:36`（阶段 C 不能单独部署）、`:54`（中间状态不可部署）、`:67`（0045「新迁移、备份排除、旧作者任务兼容及有损降级保护须作为同一单元验证」）。
- 含义：0044–0054 是一次性整体上线（提交 `4ecf119`，2026-11-15），演练的「旧版本」是该提交的父提交 `4501f18`（迁移头 `0043`），不存在停在 0044–0053 中间版本的可部署代码。

### 1.5 research 子文档中反复出现的部署前提（逐条）

| 位置 | 要点 |
|---|---|
| `R/phase-e-chapter-claim-design.md:27` | 恢复前提是「旧执行进程已停止并排空、同一数据库只有一个调度器」；若将来允许多实例，须另有租约／心跳及 fencing |
| `R/phase-e-chapter-claim-design.md:85` | 整库备份保留真实运行列；「隔离恢复时旧进程必须先排空并遵守同一恢复协议，不能直接运行旧服务读取新多用户日志」 |
| `R/phase-e-chapter-claim-design.md:88` | 新增占用迁移有任何非空 claim 或执行／清理事实时拒绝降级；先在隔离库验证空库、旧库、活动占用、异常部分值等，确认失败均不清数据（文中注明「建议，尚未执行」`:90`） |
| `R/phase-e-chapter-claim-design.md:118-119` | 当前产品是单进程调度；兼容旧 UUID 缺失的活动任务需「在旧进程全部停止后定义可审计、可证明的修复流程」 |
| `R/phase-e-chapter-claim-core.md:11-12` | 历史 `generating` 且三列全空可存储但不自动认领；0046 降级前「必须先证明旧执行体排空并安全处置」；整库备份恢复前必须停止旧进程 |
| `R/phase-e-engine-fencing.md:14` | 「任务引擎仍以单进程部署且升级前旧写进程已经排空为前提；没有分布式租约」 |
| `R/phase-e-engine-fencing.md:17` | 进程崩溃或取消确认丢失时可能残留清理标记；必须先排空旧进程并核对原身份后才可做「经审查的人工修复」 |
| `R/phase-e-index-core.md:6,15` | 0045 降级「不能通过清空运行态绕过排空」；「单进程部署与旧版本执行器排空是现有前提，不承诺多实例崩溃自动恢复」 |
| `R/phase-e-agent-change-identity.md:8` | 0047「升级需要停掉仍按旧模式插入 AgentChange 的旧应用实例；旧写入缺少身份会被拒绝」 |
| `R/phase-e-model-preflight-review.md:15` | 「上线迁移/降级须排空旧服务与执行器」；整库快照恢复会保留原实例键，不能单凭库内 UUID 证明回滚前后的全局时序 |
| `R/phase-e-batch-claim.md:3,12,20,21` | 「单进程任务引擎，旧进程必须排空」；启动恢复仅在旧进程排空后使用原持久任务；「真实多实例调度、跨实例 lease……生产部署与迁移均不在本切片的证明范围」 |
| `R/phase-e-ai-guard.md:49` | PG 并发测试未覆盖旧进程恢复或多进程引擎 |
| `R/phase-e-mcp-settings-gate.md:11` | 「两个应用实例的 deployment 默认必须在部署时一致，现有表并未存储全局 deployment 默认」 |
| `R/phase-e-admin-chapter-cancel.md:5` | 孤立 running 的管理员清理须「在单进程及旧进程排空前提下证明原执行体已停止」 |
| `R/phase-e-remaining-integration-map.md:61` | E7 端到端完成后「再与全后端/前端和迁移、旧进程排空一起交付。不要因为一个 Job 的安全测试通过而开放其他 Job。」 |
| `A/design.md:241` | E5 裁决：跨实例撤权发现沿用心跳周期（约 15 秒），不加跨实例唤醒 |
| `A/prd.md:62` | R6：「旧数据升级、单书导入、回滚与降级不得悄悄丢失或暴露协作信息」 |
| `A/prd.md:38` | 「已中断的旧任务不自动恢复」（指作品恢复后的任务，不是版本升级） |

### 1.6 未找到

- 「旧服务日志隔离兼容补丁」（`design.md:88`）：全仓只有这一处提及，**没有找到任何实现或分支**。
- 现成的部署/回滚 runbook、演练记录：未找到（仓库无 `docs/`、`scripts/` 目录）。

---

## 2. 迁移清单（0044 起到最新）

**最新 head = `0065`**（`V/0065_scene_moves.py:6-7`；全链 65 个版本、单线无分支，已按 `down_revision` 核对）。

所有 0044–0065 的 `downgrade()` 都是同一模式：**先检查，有新语义数据则 `raise RuntimeError` 拒绝；无数据才真正删列/删表**。没有任何一个是「静默丢数据的降级」。空库全链可逆有测试佐证：`backend/tests/test_migrations.py:437-463`（`upgrade head` → `downgrade base`，SQLite）。

### 2.1 协作相关 0044–0054

| 文件 | 内容 | `downgrade()` 拒绝条件（原文位置） | 在真实旧库升级后、未产生新数据时能否降级 |
|---|---|---|---|
| `0044_novel_collaborators.py` | 建 `project_collaborators` / `series_collaborators` / `collaboration_audit_events`；`generation_logs.actor_user_id`，旧记录按 `projects.user_id` 回填（`:66-70`） | 三表任一有行（`:106-108`）；或存在 `actor_user_id IS NULL`、作品不存在、`actor_user_id <> 作者` 的生成记录（`:109-113`）。**不加表锁** | 能（有测试佐证：`tests/test_collaboration_migration.py:38-63`） |
| `0045_kb_index_identity.py` | `tasks.execution_key`；`kb_documents` 三列索引占用 | 任一文档占用列非空，或任一 `tasks.execution_key` 非空（`:47-53`）。**不加表锁** | 能 |
| `0046_chapter_claim_identity.py` | `chapters` 三列生成抢占身份 | 任一章节 `status='generating'`（**含历史无 claim 的生成中章节**）或任一 claim 列非空（`:42-46`）。**不加表锁** | 仅当库里没有 `generating` 章节时能 |
| `0047_agent_change_identity.py` | `agent_changes.change_key`（存量逐行回填 UUID）、两表 `turn_task_key` | **`agent_changes` 有任何一行**，或 `agent_messages.turn_task_key` 非空（`:97-100`）。**不加表锁** | **只要旧库有过任何智能体提案就不能**（升级时已给每行回填 key；`R/phase-e-agent-change-identity.md:9`：需「清空相关提案」后才可降级） |
| `0048_generation_log_model_source.py` | `generation_logs.model_source` / `model_config_owner_id`，旧记录保持 NULL | 任一行两列非空（`:46-49`）。**不加表锁** | 能 |
| `0049_ai_config_instance_identity.py` | `ai_configs.instance_key`（存量回填）+ 生成/不可变触发器 | 先锁 9 张表（PG `ACCESS EXCLUSIVE`，`:190-194`；SQLite 写 `alembic_version`）再扫描 `_has_dependencies`（`:87-120`）：**任何任务状态不在 `succeeded/failed/cancelled`（即 queued/running/paused/interrupted 都算）**、`execution_key` 或管理员取消标记非空、任务/提案/消息/日志载荷含 v2 绑定或未决收据键（`_BINDINGS`/`_RECEIPTS`，`:18-23`）、`scene_review_proposals` 有 pending、`scene_review_payload_chunks` 有任何行、`shared:` 非 v1 向量键、文档处于 pending/parsing/indexing 或有占用 | 只有任务全部为三种终态且无上述载荷时能（有测试佐证：`tests/test_ai_config_instance_migration.py:100-113,173-238`） |
| `0050_inspiration_origins.py` | 建 `inspiration_origins` | 表内有任何行（`:36-42`，先锁表） | 能 |
| `0051_analysis_index_receipt.py` | 建 `analysis_index_receipts` + 不可变触发器 | 收据表有行（`:137-138`）；或 `chapter.analyze` / `chapter.batch` / `kb.index` 任务不在三种终态、或有 `execution_key`/管理员取消标记（`:139-144`，先锁 `tasks` 与收据表） | 无未结束的这三类任务时能 |
| `0052_generation_log_instance_identity.py` | `generation_logs.log_instance_key`（新插入必填）/ `model_config_instance_key` + 触发器；旧行 NULL | 任一行两键非空（`:114-118`，先锁表） | 能；但**新版本只要写过一条生成记录就不能**（ORM 插入必带 key） |
| `0053_agent_summary_source.py` | `agent_conversations.summary_source` | 任一非空（`:34-37`，先锁表）；`A/implement.md:1620` 记录原实现是直接删列，后改为拒绝 | 能 |
| `0054_agent_conversation_round.py` | `agent_conversations.collaboration_invitation_id` | 任一非空（`:36-39`，先锁表） | 能 |

综合（推断，由上表条件叠加得出，无实测记录）：新版本一旦对外运行并产生过「任一 AI 生成记录」（0052、0048）或「任一协作邀请/审计，含失败邀请」（0044），`alembic downgrade` 到 0043 就会在对应版本被拒绝。这与 `README.md:552`「如需回退，请恢复升级前的数据库备份」一致。另外即使新版本没产生数据，旧库里若有智能体提案（0047）、`interrupted`/`paused` 任务（0049、0051）或 `generating` 章节（0046），降级同样被拒。

### 2.2 协作之后 0055–0065（非协作功能，但在同一条降级链上）

| 文件 | 内容 | 拒绝条件 |
|---|---|---|
| `0055_pen_names.py` | 笔名与作品署名 | 两表任一有行（`:52-55`） |
| `0056_setting_thumbnails.py` | 设定缩略图资源与绑定 | 两表任一有行（`:93-96`） |
| `0057_novel_templates.py` | 小说模板、图片、建书收据 | 三表任一有行（`:73-75`） |
| `0058_consistency_check.py` | 事实台账、检查记录、问题 | 三表任一有行（`:120-122`） |
| `0059_consistency_impacts.py` | 影响记录 + 台账上一版三列 | 影响表有行或上一版列非空（`:64-68`） |
| `0060_batch_revisions.py` | 批量修订与撤销记录 | `revision_batches` 有行（`:63-64`） |
| `0061_ai_usage.py` | 用量、单价、预算；给超管加权限 | 三表任一有行（`:170-172`）；或非超管角色持有 AI 用量查看权限（`:173-175`） |
| `0062_ai_usage_time_pricing.py` | 分时定价列 | 有分时规则或分时用量（`:33-39`） |
| `0063_ai_usage_cached_input.py` | 缓存命中单价与用量 | 有缓存定价/用量（`:67-75`）；或有多空闲时段（`:78-83`）；通过后会改写 JSON 去掉新键（`:86-90`） |
| `0064_serialization_workbench.py` | 连载排期、定稿、发布 | 四表任一有行（`:83-85`） |
| `0065_scene_moves.py` | 场景跨章移动历史 | `scene_moves` 有行（`:44-45`） |

0055–0065 的 downgrade 全部先取锁再检查（SQLite 写 `alembic_version`、PG `LOCK TABLE … ACCESS EXCLUSIVE`）。要从 head 降到 0043，必须依次通过 0065→0044 共 22 道检查。

---

## 3. 旧任务兼容：缺少新字段的任务在新代码下的遭遇

### 3.1 新代码写入、旧代码没有的字段

| 字段 | 位置 | 旧代码（`4501f18`，head 0043）是否写入 |
|---|---|---|
| `Task.input["_task_actor_token_version"]` | `backend/app/services/task_service.py:60,600`（每个新任务必写） | **否**（已用 `git grep` 核对：父提交 `backend/app` 下 0 处） |
| `Task.input["_agent_actor_token_version"]` / `["_wizard_actor_token_version"]` | `task_service.py:497-498,601-602`；`execution_identity.py:16-18` | 否（0 处） |
| `Task.input["_collaboration_grant"]`（`GRANT_KEY`） | `collaboration_task_access.py:18`；仅协作者任务，由 `bind_task_grant` 写入（`:63-72`，`task_service.py:598`） | 否（旧版无协作者任务） |
| `Task.execution_key`（列，0045） | 领取时写（`engine.py:1294`） | 否 |
| `Task.input["_chapter_claim"]` + `chapters.generation_claim_*`（0046） | 章节任务受理时绑定（`handlers/chapter.py:100-115`） | 否 |
| `kb_documents.index_task_key/queued_at/claim_key`（0045） | 文档索引受理时绑定 | 否 |
| `Task.input["_project_model"]` / `["_shared_embedding"]` | `task_service.py:594-599`；向量选择见 `SPEC/retrieval.md:138-141` | 否 |

### 3.2 排队中（queued）的旧任务：不执行、不失败，留在队列里

`TaskEngine._claim_and_run`（`engine.py:1176-1300`）的各分支：

| 旧任务形态 | 分支 | 结果 |
|---|---|---|
| 任意类型、输入缺 `_task_actor_token_version` | `from_queued_task` 得到 `actor_token_version=None`（`execution_identity.py:109,119`；有测试佐证 `tests/test_task_execution_identity.py:68-74`）→ `engine.py:1275-1280` `_mark_stalled` 后返回 | **保持 `queued`、`execution_key` 为空、零模型调用**。有测试佐证：`tests/test_book_import_actor_identity.py:208-241`（断言 `status == "queued"`、`fake_llm.calls == []`） |
| `agent.turn` / `wizard.*` 缺令牌别名 | `from_queued_task` 抛 `task_execution_changed`（`execution_identity.py:112-115`）→ `engine.py:1281-1284`「历史身份或原认证已失效：不得从当前用户补签」→ stalled | 保持 `queued`（已核实代码；未找到专门测试） |
| `chapter.generate/polish/revise` 无 `_chapter_claim` | `capture_queued_claim` 抛错 → `engine.py:1228-1233`「没有原 claim 的旧排队记录不得猜测为可运行的新章」→ stalled | 保持 `queued`；章节仍是 `generating`。捕获失败有测试佐证：`tests/test_chapter_claim_author_wiring.py:69-76` |
| `kb.index` 文档模式、文档无占用（0045 前入队） | `engine.py:1234-1246` → `_fail_legacy_document_queued` | 锁内可证明无执行体时**记为 failed**，文档置 failed「旧版建索引任务已清理，请重新索引」；不可证明则 stalled。见 §3.4 |
| `queued_at IS NULL`（0026 之前的行） | `from_queued_task` 要求 `queued_at` 为 datetime（`execution_identity.py:90`）→ stalled；文档占用绑定也拒绝（有测试佐证 `tests/test_kb_index_claims.py:96-107`） | 保持 `queued` |
| 带 `_collaboration_grant` 但授权已失效 | `engine.py:1247-1257` → `_fail_collaboration_queued` | 记为 failed（旧版本不会产生此类任务，仅新版本内部场景） |

`_stalled` 的含义（`engine.py:266-270,337-344,673-688`）：本进程内的退避登记，被跳过但**仍占据其串行键队首**（`:685-687`，同 `serial_key` 的后续任务不得越过它）；任何 `notify()` 会清空登记后重新尝试，结果仍是 stalled。`A/implement.md:567` 记录了引入它的原因（消除领取热循环）。

这些停滞任务的出路（已核实代码）：
- 本人取消：`task_service.cancel_task`（`task_service.py:1010-1034`）。
- 本人重试：**被拒**。`create_task(retry_of=…)` 要求原任务输入里有与当前一致的令牌版本，否则 409 `task_actor_unavailable`「原认证身份无法验证」（`task_service.py:511-516`）；且排队状态本身不可重试（`:681-682`）。
- 管理员取消：见 §6.1。

### 3.3 运行中（running）与待审核（paused）的旧任务

- **停旧服务时**：旧引擎 `stop()` 会把运行中任务取消并记为 `interrupted`（旧代码 `engine.py:107-108`；新代码 `engine.py:304-335`），queued 不动。
- **新服务启动时**：`start()` → `initialize_tracking` → `recover()` → `scan_admin_cancellations` → 调度（`engine.py:292-302`；应用层顺序见 `backend/app/main.py:114-126`）。`recover()` 查询**库里所有** `status == "running"` 的任务（`engine.py:440`，不区分进程——这正是第 25 项第一个待办的根因，`engine.py:5-6`、`SPEC/database-guidelines.md:552-554`），按类型分流：
  - 普通任务：`on_abort` 后置为 `interrupted`「服务重启，任务已中断」（`engine.py:544-577`）。
  - `chapter.analyze`、`wizard.*`（无可证收据时）：**保持 `running`**，登记「任务执行事实待核对」（`engine.py:472-478`）。
  - 章节类带 `_chapter_claim` 检查点、或 `chapter.batch` 断点不可证：保持 `running`，「章节完成凭据待核对」（`engine.py:505-540`）。有测试佐证：`tests/test_batch_generate.py:629-646`（旧式无 claim 的运行中批量任务恢复后仍是 `running`，章节仍 `generating`，重试 409）。
  - 有管理员取消意图的章节任务：留给管理员 drain（`engine.py:541-543`）。
- **待审核（paused）的旧任务**：不被调度。`resume_task` 只把状态改回 `queued`（`task_service.py:735-740`），不检查令牌键；随后按 §3.2 第一行在领取处停滞（推断：由两段代码组合得出，未找到直接测试）。`scan_collaboration_paused` 只处理带 `_collaboration_grant` 的非作者任务（`A/implement.md:578`），与旧作者任务无关。
- **`statistics_key` 为空的更老任务**：`initialize_tracking` 会给没有统计记录的任务补 UUID（`task_statistics_service.py:36-46`），但研究文档明确补发的 UUID 不构成原占用证据（`R/phase-e-index-core.md:18`、`R/phase-e-chapter-claim-core.md:11`）。

### 3.4 唯一有专门清理路径的旧任务：0045 前的知识库文档索引任务

- 裁决：`A/design.md:234`（E4-e，2026-11-12）。实现：`A/implement.md:523-539`。
- 证明条件（同一事务、锁内）：Task 仍 `queued`、`execution_key IS NULL`、无管理员取消意图、`queued_at`/UUID 与读取时一致、请求为本作品 document 模式且 `resource_key == kbdoc:{id}`；文档 `pending` 且三列占用全空；该文档无其他未结束 `kb.index` 任务。
- 三个入口共用同一清理函数：引擎领取（记 failed）、用户取消（记 cancelled，`task_service.py:1078-1105`）、作品删除排空。不可证明时仍 409 `task_cleanup_unproven`。
- 清理后重试返回 409 `kb_legacy_task_not_retryable`，作者经 `POST /kb/documents/{id}/reindex` 重新受理。
- 测试：`tests/test_kb_legacy_document_cleanup.py`（现存）；对应 PG 文件已被移除（见 §5.2）。

### 3.5 部分旧形态仍可运行的情况（已核实）

输入**有**令牌版本但**没有** `_shared_embedding` 的向导任务（开发中间版本入队的形态）可运行，但只用关键词、零向量发送：`SPEC/retrieval.md:140-141`，有测试佐证 `tests/test_task_shared_embedding_e6f.py:92-114`。真正由 0043 代码入队的任务没有令牌版本，走不到这里。

### 3.6 `collaboration_task_access.py` 对旧值的处理

- `task_grant_identity`（`:22-35`）：只接受规范 UUID + 正整数 revision，「bool、字符串数字及破损历史值均不能授权」。
- `task_visible_to_actor`（`:107-140`）：要求 `Task.statistics_key IS NOT NULL`（`:138`）；作者（`Project.user_id == actor_id`）不需要 grant。
- `require_write_execution`（`execution_identity.py:210-269`）：`actor_token_version is None` 直接 `task_execution_changed`（`:234-235`）。
- 已在运行、但缺令牌版本的任务（理论情形）：`_run` 以「任务执行身份尚未确认」记 failed 并按原 claim 回退（`engine.py:1416-1423,1506-1508`）；有测试佐证 `tests/test_collaboration_task_revocation_e4.py:322-351`。

---

## 4. 现有部署 / 升级 / 回滚文档

### 4.1 README

| 行 | 内容 |
|---|---|
| `README.md:96` | 「后台任务引擎目前要求单进程运行。」不要加 `--workers`，不要多实例共享同一数据库调度任务 |
| `README.md:98-154` | Docker 部署三步；`:143`「数据库健康检查通过后，应用会先执行 `alembic upgrade head`，再启动后端」；`:154` `/healthz` 不检查数据库 |
| `README.md:215` | 本地启动不自动迁移；列出 0035–0065 各版本对应功能与拒绝降级条件；「**升级前保留数据库备份并排空旧版本的 AI 任务，不混跑新旧服务。**」 |
| `README.md:536-552` | 「作品协作 · 数据库迁移」：0044–0054 逐版本说明表；`:552`「新版本运行后写入的协作关系、审计记录、生成记录身份等数据，会让对应迁移拒绝降级，系统不会删除数据来伪装兼容。升级前请备份数据库并排空旧版本的后台任务，不要让新旧服务混跑。如需回退，请恢复升级前的数据库备份，并使用与之配套的旧版本。」 |
| `README.md:683-687` | 全站任务监控；取消只是「已受理」，不保证立刻停止已发出的外部请求 |
| `README.md:708` | 「升级已有实例时，先应用新增数据库迁移并重启后端」；Docker 启动命令自动迁移 |
| `README.md:791-814` | 常用管理命令；`:803-804` `docker compose up -d --build` 自动迁移；`:812` `down -v` 会删数据卷；`:814`「更新前应备份数据库和原配置。数据库迁移不等于完整回滚机制；需要回退版本时，应先确认代码与数据库结构兼容，必要时恢复配套备份。」 |
| `README.md:854-864` | 管理端整库备份；`:856`「页面不提供整库覆盖恢复」；恢复需原 `ENCRYPTION_KEY` |
| `README.md:866-876` | 下载、校验 SHA-256、GPG 解密；「恢复到隔离环境验证后，再安排目标实例停服」 |
| `README.md:878-909` | PostgreSQL 手工 `pg_dump -Fc` 导出与 `pg_restore --clean --if-exists` 恢复命令（先 `docker compose stop app`） |
| `README.md:911-921` | SQLite 文件恢复 |
| `README.md:942` | 「CI 使用同一份开发依赖锁文件执行 4 进程 SQLite 全量回归；**自动化测试不再验证 PostgreSQL**。」 |
| `README.md:1071-1072` | 当前限制：单进程任务调度；运行中的模型调用不能跨进程恢复 |

README 中**没有**：如何「排空旧版本的后台任务」的具体操作步骤、如何确认已排空、停机升级的操作顺序清单、回滚操作清单。

### 4.2 Dockerfile / Compose / alembic 环境

- `Dockerfile:37`：`CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers"]`（无 `--workers`）。
- `docker-compose.yml:3` `postgres:16-alpine`；`:17-22` 单个 `app` 服务，`restart: unless-stopped`，依赖 `db` 健康；数据库端口不发布到宿主（`README.md:206`）。
- `backend/alembic/env.py`（全文 67 行）：**没有任何加锁**（无 `pg_advisory_lock`、无 `LOCK TABLE`、无 `lock_timeout`）；`do_run_migrations`（`:45-48`）用 `context.begin_transaction()` 包住 `run_migrations()`，未设置 `transaction_per_migration`；连接池 `NullPool`（`:54`）。`alembic.ini` 无相关配置。
- 已安装 Alembic 1.20.1，`transaction_per_migration` 默认 `False`（`.venv/.../alembic/runtime/environment.py:419`），即一次 `upgrade head` 的全部迁移在同一事务内；版本表更新带行数校验（`.venv/.../alembic/runtime/migration.py:728-763`，不匹配时报「Online migration expected to match one row」）。
- 多实例同时启动时并发执行 `alembic upgrade head` 的实际表现：**未找到任何实测或文档记录**。可确定的事实只有上面三条（无显式锁、单事务、版本表行数校验）；PostgreSQL 下 DDL 在事务内、第二个进程会被表锁阻塞后再失败或成功属于推断，需演练确认。
- 部分迁移内部自带写锁：SQLite 下 0047/0048/0049/0052/0062/0063 的 `upgrade()` 先 `UPDATE alembic_version SET version_num=version_num` 取得真实写事务（如 `V/0048…:27-29`），为的是中断后可整体回滚重试（有测试佐证：`tests/test_generation_log_source_migration.py:134-164`、`tests/test_ai_config_instance_migration.py:246-274`、`tests/test_agent_change_identity_migration.py:190-225`）。PostgreSQL 的 `upgrade()` 不显式加锁。

### 4.3 `.trellis/spec/backend/` 规范

| 位置 | 内容 |
|---|---|
| `SPEC/database-guidelines.md:286-288` | 应用：`uv run alembic upgrade head`；Docker 入口同上；「回滚：`uv run alembic downgrade -1` / `base`」（通用说明，未提降级保护） |
| `SPEC/database-guidelines.md:296-298` | `test_upgrade_and_downgrade`：每个新版本都必须保持该测试通过 |
| `SPEC/database-guidelines.md:3,5,7` | 0052/0054/0053 的降级保护（SQLite 先取写锁、PG `ACCESS EXCLUSIVE` 后扫描） |
| `SPEC/database-guidelines.md:9,15,17,27` | 0029/0026/0025/0024 各自的拒绝降级条件 |
| `SPEC/database-guidelines.md:552-554` | 单进程前提；「改为多实例前须把领取改成 `SELECT ... FOR UPDATE SKIP LOCKED` 并引入心跳 / 租约，否则一个实例重启会把其他实例的运行中任务误判为中断」 |
| `SPEC/admin-dashboard.md:103` | 存在日快照或任务统计时拒绝破坏性降级 |
| `SPEC/admin-dashboard.md:105` | 「部署仍要求单进程，不支持新旧版本同时调度；回退期间旧代码可能造成未跟踪区间，不能伪称连续历史。」 |
| `SPEC/admin-task-monitoring.md:7,17` | 仍为单进程数据库队列；「有 pending 意图时拒绝 downgrade。仍不支持新旧调度器并行运行。」 |
| `SPEC/admin-task-monitoring.md:124` | 错误矩阵：有 pending 时降级/销毁恢复依据 → 拒绝 |
| `SPEC/rbac-quotas.md:44` | 启动顺序：授权初始化 → 设置 load → bootstrap → 恢复/启动任务；有自定义角色/成员/配额记录/RBAC 审计时拒绝 downgrade |
| `SPEC/rbac-quotas.md:77` | 「继续沿用单进程 TaskEngine，不宣称多实例租约」 |
| `SPEC/system-settings.md:5` | 「继续沿用任务引擎已有的单进程部署前提，不提供多 worker 广播」 |
| `SPEC/recycle-bin.md:105` | 「上线前先备份，异常先停清理/停写再前滚修复」；有回收站数据时拒绝 downgrade |
| `SPEC/admin-backups.md:5,25,54,56` | 只提供下载及命令行恢复说明，没有 HTTP 整库覆盖恢复；有备份数据时禁止有损 downgrade；重启将遗留活动备份记 interrupted；PG 用 `pg_dump -Fc` |
| `SPEC/quality-guidelines.md:151-167` | PG 隔离测试库环境变量与守卫（部分已过时，见 §5.2） |
| `SPEC/quality-guidelines.md:169-171` | 迁移冻结结构契约（0037） |
| 其余功能规范 | 各自一句「有数据时拒绝有损 downgrade」：`scenes.md:116`、`mention-tracking.md:35`、`setting-fields.md:78`、`setting-categories.md:14`、`pen-names.md:33`、`skills-mcp.md:322`、`prompts-styles.md:216` |

规范中**没有**专门讲「部署/升级/回滚流程」的文件。

---

## 5. 现有迁移测试与 PostgreSQL 测试设施

### 5.1 迁移测试（现存，全部 SQLite、进程内调用 Alembic）

- 形态：`alembic.command.upgrade(cfg, "<rev>")` → 用同步 `create_engine("sqlite:///…")` 灌数据 → `command.upgrade` / `command.downgrade` → 断言。数据库是 `tmp_path` 下的文件。
- 没有共用辅助函数。每个文件各自定义 `_config(path)`（例：`tests/test_chapter_claim_migration.py:24-29`，用内存 `Config()` 设 `script_location` 与 `sqlalchemy.url`）和各自的 `_seed`；`tests/helpers.py`、`tests/schema_helpers.py`、`tests/conftest.py` 里都没有 Alembic 相关辅助。
- 协作迁移对应的现存文件：

| 文件 | 覆盖 |
|---|---|
| `tests/test_migrations.py:437-463` | 空库 `upgrade head` + `downgrade base` |
| `tests/test_collaboration_migration.py` | 0044：旧数据回填、五种拒绝降级（`:69-104`）、约束 |
| `tests/test_collaboration_empty_migration.py` | 0044 空库往返 |
| `tests/test_kb_index_identity_migration.py` | 0045：约束、两种拒绝、清空后可降 |
| `tests/test_chapter_claim_migration.py` | 0046：保留子行、两种拒绝 |
| `tests/test_agent_change_identity_migration.py` | 0047：旧数据、拒绝、空库、回填失败回滚重试 |
| `tests/test_generation_log_source_migration.py` | 0048：旧数据、拒绝、升级/降级中断重试 |
| `tests/test_ai_config_instance_migration.py` | 0049：回填、安全降级、四类拒绝、升级/降级中断重试 |
| `tests/test_inspiration_origin_migration.py` | 0050 |
| `tests/test_analysis_index_receipt_migration.py` | 0051 |
| `tests/test_generation_log_identity_migration.py` | 0052 |

- 这些测试都是**单版本步进**（N-1 → N → N-1）。**未找到**「从 0043 带真实旧数据一次升到 head、再尝试回退」的端到端用例，也未找到「0043 代码创建的任务在 head 代码下如何表现」的跨版本用例（§3 的测试佐证都是在新代码里手工删字段模拟）。

### 5.2 PostgreSQL 测试设施：大部分已从工作区移除

- 提交 `b329323`（2026-11-17，「test: 移除 PostgreSQL 测试并保留 SQLite 套件」）删除了 **123 个 `*_postgresql.py` 测试文件和 `backend/tests/pg_support.py`**，同时改了 CI 与 README。该提交是当前 `HEAD` 的祖先。
- 被删的协作迁移 PG 测试包括：`test_ai_config_instance_migration_postgresql.py`（含真实 `pg_dump`/`pg_restore` 整库快照恢复、跨连接降级竞争）、`test_inspiration_origin_migration_postgresql.py`、`test_kb_index_identity_postgresql.py`、`test_chapter_claim_postgresql.py`、`test_agent_change_identity_postgresql.py`、`test_generation_log_source_postgresql.py`、`test_collaboration_postgresql.py` 等。内容仍可从历史读取：`git show b329323^:backend/tests/<文件名>`。
- 当前工作区引用 PG 测试环境变量的文件只有 3 个：`tests/test_collaboration_audit_query_postgresql.py`、`tests/test_collaboration_stream_agent_postgresql.py`、`tests/test_serialization_concurrency.py`，统一使用 **`AINOVEL_POSTGRES_TEST_URL`**。
- 获取方式（`tests/test_collaboration_audit_query_postgresql.py:17-47`）：文件内自带 `settings_overrides` fixture；未设变量则 `skip`；守卫要求驱动 `postgresql+asyncpg`、主机为本机回环、库名必须是 `ainovel_test`、不带查询参数，否则 `ValueError`；每个测试建随机 schema，用 `tests.schema_helpers.create_isolated_schema` 以 `create_all` 建表（**不走 Alembic**），结束后 `DROP SCHEMA … CASCADE`。
- `SPEC/quality-guidelines.md:153-167` 仍描述 `AINOVEL_COLLAB_POSTGRES_TEST_URL`、`pg_support.collab_test_url` 等守卫表——对应代码已不在工作区，属过时描述。其中仍有效的经验：同一数据库内 advisory lock 不按 schema 隔离，PG 测试须 `-n 0` 串行（`:164`）；`pg_dump`/`pg_restore` 主版本须与服务端一致，macOS 上 PG16 服务端配 18.x 客户端会因 `SET transaction_timeout` 失败，应把 `libpq@16` 放在 PATH 最前（`:167`）。
- 是否依赖本机 Docker PG 16：测试代码只认环境变量指向的本机库，不自己起容器。验收记录里的做法是临时起 `postgres:16-alpine` 容器（见 5.3）。

### 5.3 归档任务如何启动隔离 PostgreSQL 16 与隔离应用实例

| 任务 | 记录位置 | 做法 | 是否给出确切命令 |
|---|---|---|---|
| 11-19 协作审计查询 | `…/11-19-collaboration-audit-query-ui/research/verification.md:5,25` | 应用：系统临时目录独占 SQLite，从空库 `alembic upgrade head`，随机密钥与回环随机端口，`FRONTEND_DIST_DIR` 同源托管生产包。PG：「临时 `postgres:16-alpine` 容器的独立 `ainovel_test` 库（随机 schema，用后自动销毁）」运行 `tests/test_collaboration_audit_query_postgresql.py -n 0 -v` | 只有 pytest 参数；**容器启动命令未记录** |
| 11-19 协作者实时订阅 | `…/11-19-collaborator-realtime-progress-subscription/research/verification.md:5,22` | 同上，另加本地 OpenAI 兼容假模型 | 同上，**容器启动命令未记录** |
| 11-18 AI 用量 | 该任务**没有** `research/` 目录；记录在 `…/11-18-ai-usage-cost-management/implement.md:41,58,69,81,92` | 「隔离 PostgreSQL 16 验证迁移往返……并执行整库备份恢复核对」；PG 用 `-n 0` | **未记录命令** |
| 11-18 连载工作台 | `…/11-18-serialization-workbench/implement.md:45-46` | 隔离 SQLite 与 PG 16 实测从 0063 升级、空库降级再升级；`uv run pytest -n 0 -q tests/test_serialization*.py` | 只有 pytest 命令 |
| 11-20 场景跨章 | `…/11-20-scene-cross-chapter-ai-restructure/verification.md:7` | 「SQLite 与 PostgreSQL 的实际 Alembic 迁移均达到 `0065`。空的 PostgreSQL 迁移库完成了 `0065 → 0064 → 0065`」 | **未记录命令** |

可直接复用、带确切命令的现成脚手架有两套：

1. **隔离应用实例（SQLite）**：`.trellis/tasks/archive/2026-11/11-18-browser-acceptance-backlog/research/harness/`（`serve.py`、`fake_model.py`、`seed.py`、`lib.cjs`）。命令见其 `README.md:17-36`：`(cd frontend && npm run build)` → `python3 $H/serve.py start [--dir <空目录>]` → `backend/.venv/bin/python $H/seed.py --dir <dir>` → `python3 $H/serve.py status|stop --dir <dir>`。`serve.py` 组装 `DATABASE_URL`（`<dir>/qa.db`）、随机 `JWT_SECRET`/`ENCRYPTION_KEY`、`BACKUP_DIR` 等环境变量，先迁移到 head 再启动后端与假模型，全部绑定 `127.0.0.1` 随机端口，不读产品 `.env`（`README.md:38-42`）。它固定迁移到 head、固定当前代码，**不支持指定旧版本代码或中途停在某个迁移版本**。
2. **Docker 真实隔离（含 PG16、整库备份恢复）**：`backend/tests/backup_acceptance.py` + `backend/tests/backup_acceptance/{infra,probe,smtp_capture}.py`。命令见 `backend/tests/backup_acceptance/README.md:5-10`：`docker build -t ainovel-backup-acceptance:local .` → `cd backend && uv run python tests/backup_acceptance.py --scenario all --output <DIR>`。特点（`README.md:12-14,37-41`）：冻结应用 image ID、随机资源名与归属标签、专属 bridge 网络、只有应用端口绑定宿主 `127.0.0.1`、不读真实 `.env`、不碰现有数据库卷、退出时回收自己的容器/卷/网络。`SPEC/admin-backups.md:75` 称其覆盖「PG16/SQLite×明文/GPG，恢复另一目标比对」，**该描述已过时**：`b329323` 改过这组文件，当前 `infra.py:247-251` 的 `start_app` 写死 SQLite 连接串，`backup_acceptance.py` 里只剩 `engine == "sqlite"` 分支（`:334,464,565`），即**现存脚手架只跑 SQLite，不再起 PG16 容器**。它的容器编排部分（网络、标签、回收、镜像冻结）仍可参考。

---

## 6. 管理端能力

### 6.1 全站任务监控与取消（可用于排空）

- 接口（`backend/app/api/v1/admin_tasks.py:23-35`，前缀 `/api/v1/admin/tasks`）：
  - `GET ""`（`tasks.read`）：按用户/作品/类型/**多选状态**/时间筛选，返回 `{items,total,as_of,counts}`；`counts` 是当前筛选下所有页的七种状态计数（`SPEC/admin-task-monitoring.md:23,35`）——可用来判断 queued/running/paused 是否归零。
  - `GET /{task_key}`：单任务详情。
  - `POST /{task_key}/cancel`（`tasks.cancel`）：`{confirm:true, reason}`，返回 202 仅表示**受理**。
- 可取消范围：`can_cancel = status in OPEN_TASK_STATUSES and not pending and task_key is not None`（`admin_task_service.py:135`），即 queued / running / paused 均可（`admin_task_service.py:227-229`）；`task_key`（`statistics_key`）为空的任务不可取消（启动时 `initialize_tracking` 会补，见 §3.3）。
- 限制（已核实）：
  - **只有单任务取消，没有批量取消或「全部取消」接口**。
  - 受理与真实终态分离：独立 drain 收尾，running 任务等待 10 秒未完成仍保持 pending（`SPEC/admin-task-monitoring.md:89-92`）；收尾失败时保留 pending 及资源屏障（`:122`）。
  - 管理员不能重试/继续/接管他人任务，也读不到输入输出（`:7,61`）。
  - 未知 handler 的任务 fail-closed，保留意图不释放（`:97`）。
  - **有 pending 取消意图时迁移拒绝降级**（`:17,124`；0049 与 0051 的 downgrade 也检查 `admin_cancel_operation_id`）。
- 对 §3.2 停滞旧任务的管理员取消：`cancel_task` 对章节任务调用 `chapter_cancel_intent`，旧任务捕获不到 claim 时 `intent=None`，仍写入取消标记（`admin_task_service.py:232-237`；`task_engine/cancellation.py:37-47`）。其后 drain 能否把无 claim 的旧章节任务收尾为 cancelled：**未核实**（`R/e4-reauth-inventory.md:179` 当时也标注「未核实管理员 drain 对带 grant 排队任务的处理细节」）。

### 6.2 「暂停新任务准入」开关：没有运维可用的全局开关

- **没有维护模式**。在 `backend/app` 中搜索 `maintenance`、`维护模式`、`只读模式`、`暂停准入` 等：只命中 `ai_usage_recording.usage_maintenance`（用量记账的后台维护协程，`main.py:131`），与停服无关。
- `settings_confirmed`（`backend/app/core/authorization_runtime.py:25-26`）：是**内部故障状态**，不是开关。只有系统设置提交结果未知时才被置 `False`（`system_settings_service.py:43,198,207`），恢复确认后置回 `True`（`:67`）。为 `False` 时调度器 `_dispatch` 与 `_claim_and_run` 直接返回（`engine.py:655,1183`）——**只暂停领取，不阻止 HTTP 创建新任务**，既有执行体继续（注释 `authorization_runtime.py:25`；`SPEC/rbac-quotas.md:77`、`SPEC/system-settings.md:38`）。没有任何接口可手动触发。
- 系统设置六项（`SPEC/system-settings.md:29`）：`registration_enabled`、`auto_analyze_enabled`、`mcp_enabled`、`task_max_concurrency`、`task_max_per_user`、`open_api_rate_limit_per_minute`。两个并发项**最小为 1**（`backend/app/schemas/system_settings.py:13,27-28`；部署基线 `backend/app/core/config.py:44-45` 也是 `ge=1`），不能设 0 来停调度。
- 用户配额：`limited` 值允许 0（`SPEC/rbac-quotas.md:42`），「limited 0 禁止新准入」（`SPEC/system-settings.md:45`）。它是**逐用户**的，作用在调度领取（`engine.py:689-690,1261-1265`），降低后「只停止不符合新限额的后续调度，不取消运行任务」。没有全局默认项可一次性对所有用户生效。
- `create_app(start_task_engine=…)`（`main.py:105`）只是工厂参数，默认应用 `_build_default_app()`（`main.py:273-276`）恒为 `True`；**没有环境变量可关闭引擎启动**。
- 相关可用开关：`auto_analyze_enabled=false` 可阻止新的自动分析派生任务（已排队/运行的不取消，`SPEC/system-settings.md:46`）；`registration_enabled=false` 只管注册。

### 6.3 管理端整库备份能否作回滚前快照

- 能产生快照：`POST /api/v1/admin/backups/runs`（`backups.run`）受理手动备份；PostgreSQL 用 `pg_dump -Fc`，SQLite 用在线 backup API 并 `quick_check`（`SPEC/admin-backups.md:18,56`）。每次执行记录 `schema_revision`（`backend/app/models/backup.py:75`；`services/backups/service.py:453,496`），可用来确认快照对应的迁移版本。
- 前置条件：加密默认开启且**未设备份密码时不可执行**（`SPEC/admin-backups.md:29`）；需要 `pg_dump` 可用（镜像内已装 PG16 客户端，`Dockerfile:17-25`）。
- 限制：**没有 HTTP 整库覆盖恢复**，只能下载后按 README 命令行恢复（`SPEC/admin-backups.md:5`、`README.md:856,866-909`）；202 只是受理，需轮询 `GET /runs/{id}` 到 `succeeded`；备份不含 `.env` 与 `ENCRYPTION_KEY`（`README.md:864`）；备份走应用自身，**应用停机后无法使用**，此时只能用 `README.md:880-887` 的手工 `pg_dump`。
- 备份与任务引擎无联动：备份时不会暂停任务，快照里会包含当时的 queued/running 任务行（由「整库」范围推出；`R/phase-e-chapter-claim-design.md:85` 也写明「系统级备份仍保留真实数据库运行列」）。

---

## 演练需要覆盖的场景候选

每条注明依据；「候选」表示由现有文档/代码推出的步骤，不是已有流程。

**A. 升级前检查**
1. 记录当前迁移版本与代码版本（`uv run alembic current`，`README.md:934`）。依据：`README.md:814`「更新前应备份数据库和原配置」。
2. 盘点未结束任务：queued / running / paused 数量与类型（管理端 `GET /admin/tasks` 的 `counts`，`SPEC/admin-task-monitoring.md:35`）。依据：`design.md:89`、`README.md:552`。
3. 盘点会卡住后续回退的存量：`generating` 章节（0046）、智能体提案行（0047）、`interrupted`/`paused` 任务（0049/0051）、pending 管理员取消意图（0026/0049/0051）。依据：§2.1 各 downgrade 条件。
4. 确认只有一个应用实例在跑、没有 `--workers`。依据：`README.md:96`、`SPEC/database-guidelines.md:552-554`。

**B. 排空旧任务**
5. 停止新受理（现无全局开关，见 §6.2——演练需确定用什么手段：停入口、逐用户配额置 0，或直接停机）。依据：`R/phase-e-ai-config-identity-0049.md:9`「部署前停止新受理并排空旧执行体与外呼」。
6. 等待或取消 running；取消 queued / paused（管理端单任务取消，§6.1）。依据：`design.md:89`「先排空旧任务/流」。
7. 验证「未排空就升级」的实际后果：旧 queued 任务在新代码下停滞且占住串行键（§3.2）；旧 running 被记 `interrupted` 或保持 `running` 待核对（§3.3）；0045 前的文档索引任务被自动清理为 failed（§3.4）。依据：`engine.py:1228-1284`、`engine.py:436-577`、`design.md:234`。
8. 停旧服务并确认旧进程已退出。依据：`R/phase-e-engine-fencing.md:14,17`、`R/phase-e-chapter-claim-design.md:27`。

**C. 备份**
9. 整库快照（管理端备份须在停机前做；停机后用手工 `pg_dump -Fc`），核对文件大小与 SHA-256，另存 `.env` / `ENCRYPTION_KEY`。依据：`README.md:552,814,864,868,880-889`、`SPEC/admin-backups.md:56`。
10. 先把快照恢复到隔离库验证可用。依据：`README.md:868`「恢复到隔离环境验证后，再安排目标实例停服」。

**D. 迁移与启动**
11. 带真实旧数据从 0043 升到 head（PostgreSQL 16 与 SQLite 各一遍）；核对 0044 生成记录回填、0047/0049 UUID 回填、外键完整。依据：`design.md:87`；现有测试只做单步（§5.1）。
12. 只启动新版本单实例；观察 `recover()` 与调度日志。依据：`R/phase-e-ai-config-identity-0049.md:9`「仅启动相应新版本」、`main.py:114-126`。
13. （与第 25 项第一个待办相关）两个实例同时启动时 `alembic upgrade head` 的并发表现。依据：`Dockerfile:37` + `env.py` 无锁（§4.2），无既有实测。

**E. 升级后验证**
14. 旧作者数据可读、旧生成记录归作者、新任务可正常创建并运行、协作入口可用。依据：`design.md:135`「存量作者任务保持兼容」、`A/prd.md:62`。
15. 残留停滞任务的处置路径可走通（本人取消、管理员取消、重新发起）。依据：§3.2、`R/phase-e-engine-fencing.md:16`。

**F. 回滚路径**
16. `alembic downgrade` 在有新数据时被拒绝、且拒绝后数据与版本号不变（逐版本或至少 0044/0047/0049/0052）。依据：`design.md:86`、`A/implement.md:96`「不以删除协作数据绕过降级保护」；SQLite 已有单步测试，PG 测试已移除（§5.2）。
17. 「恢复升级前快照 + 启动配套旧版本」的完整回退：停机 → 备份当前库 → `pg_restore --clean --if-exists` → 启动 `4501f18` 对应镜像 → 核对账号、作品、凭据可解密。依据：`README.md:552,891-907`、`R/phase-e-ai-config-identity-0049.md:9`。
18. 确认**不存在**「新库 + 旧代码」的回退方式，并在步骤记录中写明禁令。依据：`design.md:88`（旧服务按作品返回全部生成日志；兼容补丁未实现，§1.6）、`A/implement.md:96`。
19. 升级后未产生新数据时的 `alembic downgrade` 到 0043 是否可行（取决于存量是否触发 0046/0047/0049/0051 的条件）。依据：§2.1 末列。

---

## 未找到依据、需要由用户决定的问题

1. **演练的旧版本基线**：协作迁移的前一版是 `4501f18`（head 0043）。但线上若存在实例，其实际版本未知；当前 head 已是 0065，仓库唯一 tag `v0.0.1` 指向 0064 时期的提交。演练是「0043 → 0054/head」（对应第 11 项原意），还是「当前线上版本 → head」？未找到依据。
2. **是否有真实线上环境**：所有记录都是隔离环境验收，未找到任何生产/预发环境信息。演练是纯本地隔离演练（Docker Compose + 临时卷），还是要对真实实例操作？
3. **「排空」的可接受手段**：设计只说「先排空」，没规定方法。现状没有维护模式或全局停准入开关（§6.2）。是否接受「直接停机，running 记为 interrupted、queued 手工取消」作为排空；还是要求本任务新增停准入能力？这会影响任务范围（是否改产品代码）。
4. **残留旧 queued 任务的目标行为**：现状是停滞在队列、占住串行键、不提示（§3.2）。演练只需「记录现象并在操作步骤中要求升级前清零」，还是要求新增自动清理（类似 E4-e 对旧文档任务的做法）？设计文档未对非 kb.index 的旧任务给出清理裁决。
5. **PostgreSQL 验证方式**：PG 自动化测试已在 `b329323` 移除，README 明示「自动化测试不再验证 PostgreSQL」。演练产物是一次性手工演练 + 文字记录，还是要留下可重复执行的脚本/测试？若是后者，是否允许恢复部分已删除的 PG 迁移测试或新增 Docker 依赖？
6. **回滚是否必须实际启动旧版本代码**：`design.md:88` 要求回滚时确认旧服务行为；实际启动需要构建 `4501f18` 的镜像。是否必须做到这一步，还是「恢复快照 + 校验 schema 版本为 0043」即可？
7. **多实例并发迁移是否属于本待办**：`Dockerfile` 的启动命令在多实例下会并发跑迁移，`env.py` 无锁（§4.2）。这是归入第 25 项第一个待办（多实例调度），还是归入本演练？两处文档都没有划分。
8. **操作步骤记录的落点**：「并记录操作步骤」写到哪里——README「运维与数据备份」章节、新建文档，还是只留在任务目录？未找到约定（仓库无 `docs/`）。
9. **`design.md:88` 的「旧服务日志隔离兼容补丁」**：未实现。是否明确放弃（即回滚只允许走恢复快照），并在文档里写死？

---

## Caveats / Not Found

- 本调研全程只读：未运行 `alembic upgrade/downgrade`、未启动 Docker、未连接任何数据库；§2 的「能否降级」来自阅读 `downgrade()` 源码与现存 SQLite 测试，PostgreSQL 下的行为没有在当前工作区验证过。
- §3 中标「推断」的两处（paused 旧任务继续后停滞；管理员取消无 claim 的旧章节任务能否收尾）没有找到直接测试。
- `backend/tests/backup_acceptance/` 现只含 SQLite 场景（`infra.py:247-251`、`backup_acceptance.py:334,464,565`）；被移除的 PG16 部分可从 `git show b329323^:backend/tests/backup_acceptance/infra.py` 读取，本次未展开。
- 归档验收记录普遍只写「临时 `postgres:16-alpine` 容器」而没有容器启动命令；确切的 `docker run` 参数未找到。
- `.trellis/workspace/aiNovel/journal-1.md` 只有一份（1890 行），E4/E5 相关只有 Session 45 一条摘要。
- `SPEC/quality-guidelines.md:153-167` 的 PG 环境变量表与当前代码不一致（对应支持模块已删）。
