# Research: E6 作品共享向量与知识库全链路

- Query: 上传、重建、记忆修改/衍生入队到索引分批、真实发送、所有检索消费者的身份、向量空间与并发边界。
- Scope: internal，静态源码与仓库规范；不修改产品代码。
- Date: 2026-11-09

## Findings

### 已读规范、涉及文件

- `.trellis/tasks/11-09-novel-collaborators/implement.md:58-62` 与 `design.md:140-165`：E3/E4/E6 要求真实 actor 和邀请轮次、每批重验、无授权只维护关键词、不用个人向量覆盖项目共享向量；手动重建未授权 403 `owner_embedding_not_allowed`。`design.md:52`：同步预览及后台 AI 要求 `ai.use`，viewer 不能触发 AI。
- `.trellis/spec/backend/retrieval.md:9-83,88-128,154-190`：业务与检索表同事务维护、全部候选向量一致才混合、模型故障关键词降级、现有文档任务重试及导入等；新协作任务的安全拒绝优先于旧规范「提供方故障降级」。`.trellis/spec/backend/setting-ai-visibility.md:5-25`：可能携带设定的负载和真实出站受发送代次保护。
- `backend/app/api/v1/kb.py:43-110`、`backend/app/services/kb_service.py:100-153,160-213,245-297,318-395`：知识库入口、任务受理、检索、响应投影。
- `backend/app/api/v1/memories.py:58-102`、`backend/app/services/memory_service.py:180-242,331-388,421-562`：记忆同步维护、排队和召回。
- `backend/app/services/task_engine/handlers/kb.py:16-50`、`backend/app/services/kb_index_service.py:44-182,185-260,283-391,405-419`：校验/排队、job 构建、批处理和失败清理。
- `backend/app/services/retrieval_service.py:105-168,186-260,276-392,428-477,589-659` 与 `backend/app/models/entities.py:1002-1031`：向量空间 key、embedder、检索 CRUD、缓存和检索。
- `backend/app/services/project_ai_config.py:40-70,84-160,170-193,273-388` 与 `ai_config_service.py:38-65,121-151,201-219`：其他代理正在改造的服务端模型绑定、作者配置选择屏障和复查，不能并发覆盖其文件。
- `backend/app/services/project_access.py:64-102,116-143`、`authorization_service.py:110-118,201-235`、`ai_dispatch.py:91-101,104-153,170-198` 与 `backend/app/ai/providers/dispatch_hooks.py:12-53`：认证、写/发送锁序与真实 HTTP hook。
- 消费者：`generation_service.py:323-327`、`context_builder.py:834-856,1000-1045`、`wizard_service.py:257-271`、`entity_ai_service.py:719-739`、`outline_ai_service.py:426-446`、`inspiration_service.py:449-463`、`agent/tools/read.py:305-345`。衍生入队还在 `task_engine/handlers/analysis.py:72-74`、`batch_service.py:539,581`、`agent/service.py:689-693`；备份导入在 `project_transfer/importer.py:1537-1545,1612-1617`。

### 现在的端到端路径和身份选择

1. 上传 `/kb/documents` 先做 `content.edit`、`ai.use` 预检，再读限长文件；`upload_document` 随后仍调用 `get_owned_project(lock=True)`，故现在 editor 不会通过。它校验数目/哈希、解析正文、插入 pending 文档，并通过 `task_service.create_task` 在同一业务事务受理 `document` 任务（`kb_service.py:113-153`）。解析当前在作者项目锁内等待线程；若搬到锁外，要先做可读/大小预检、解析完在短写事务中重新鉴权并处理唯一约束竞争。
2. 文档重建 `/kb/documents/{id}/reindex` 先清空该文档的所有 `KbChunk` 和检索行（含旧向量），再建 `document` 任务；全书 `/kb/rebuild` 建 `rebuild`（`kb_service.py:171-175,201-213,389-395`）。两种手动重建都必须在任何清除之前验证共享向量授权并绑定选择，拒绝时不改变文档/旧向量；没有作者向量配置沿用原缺配置提示。无作者授权的 editor **上传仍成功**，只创建关键词任务。
3. `KbIndexHandler.validate_and_claim` 使用 `validate_index_task`，当前再次要求严格作者；`build_index_job` 从 `Task.input.request.project_id` 读项目、强制 `project.user_id == task.user_id`，以 `task.user_id` 的默认配置创建 embedder（`kb_index_service.py:66-95,148-182`）。放宽等式而不改模型来源会使协作者个人模型覆盖共享空间。`IndexJob` 没有 actor、邀请轮次、任务运行轮次、向量选择或配置绑定字段。
4. 文档任务读取 doc、写 `indexing` 和关键词分块并 commit，然后以最多 32 条读取 stale 检索单元，调用模型、写向量、每批 commit，最后写 `ready`/`ready_keyword_only`；memory/rebuild 直接做分批、rebuild 再刷新各文档状态（`kb_index_service.py:185-260,306-391`）。当前网络请求发生在仍打开的读取 session 内，没有协作发送 guard；任务错误路径和 `on_abort` 只按文档 ID/状态更新，未验证项目、任务及运行轮次。
5. 手动 `StoryMemory` 创建/修改在项目写事务中同步 `upsert_many` 关键词；正文变了才清空该条旧向量，原样正文可保留向量（`memory_service.py:331-380`，`retrieval_service.py:276-320`）。HTTP 提交后 `_enqueue_vectors` 调 `may_enqueue_vectors`，当前只作者允许；分析、批量、Agent undo 另行 `enqueue_memory(project_id)`。该函数无 actor 参数、按项目寻找已排队任务，直接写 `Task.user_id = project.user_id`（`kb_index_service.py:98-142`）。若开放协作，不能把已提交的协作者修改归到作者名下。
6. 本进程的调度按 `Task.user_id` 查个人并发额度，`serial_key=index:{project_id}` 只由 `_running_serial` 内存集合约束，领取 CAS 只锁定同一个 Task（`task_engine/engine.py:264-303,343-403`）。`resource_key` 的活跃唯一索引不等同跨实例的项目 serial 锁（`models/entities.py:140,654-680`）。两实例可能同时执行不同任务，不能以注释中的「同项目串行」代替数据库层重验与 CAS。

### 权限、模型、费用和输出契约

| 调用者及状态 | 共享搜索 / 自动入队 | 手动重建 | 身份与费用 |
| --- | --- | --- | --- |
| 作者 | 现有作者默认向量；缺配置按现有关键词行为 | 有配置可重建，缺配置保留既有提示 | task 和配额归作者，费用由作者密钥承担 |
| 已接受 editor，`allow_owner_ai=false`，即使**本人有向量配置** | 上传/编辑只维护关键词；搜索和生成上下文只用关键词，不发作者或本人向量请求；不批量覆盖他人现有向量 | 403 `owner_embedding_not_allowed`，先拒绝后不得清除旧分块 | task 的 actor/配额仍是 editor；关键词无模型费用 |
| 已接受 editor，`allow_owner_ai=true` | 只能选择作品作者当时的默认/最新 embedding；缺作者配置则关键词 | 作者有有效配置时可重建 | task/并发额度是 editor；外部费用是作者密钥账户，UI 明示来源而不暴露作者配置细节 |
| viewer、管理员但不是 accepted editor | 不得触发模型和后台 AI；人工读/编辑另按既有能力矩阵，不因此取得 AI 权 | 不得重建 | 管理员额外人工权限不代替 AI 协作资格 |
| 管理员同时为 accepted editor | 按该 editor 的授权轮次及其 `allow_owner_ai` 判定；不因管理员身份跳过 | 同 editor | 操作者仍为真实管理员/editor |

`project_ai_config.resolve_shared_embedding` 当前已表达作者唯一空间、vector/keyword 选择和原因，并以 `ResolvedSharedEmbedding` 保存 actor、project、owner、grant（`project_ai_config.py:59-70,334-360`）；它要求 `ai.use`，所以它本身不能被当作允许 viewer 做纯关键词人工读取的授权 API。直接搜索端点现预检 `ai.use`；如产品以后单独开放 viewer 纯关键词读，应显式另设计 `content.read` 路径，绝不从现有 AI 入口悄然扩大。

建议在任务受理的服务器侧锁内固定选择与原邀请轮次，`Task.user_id`、`Task.project_id`、`statistics_key` 和运行轮次为可信来源；严格核对 `input.request` 的项目/文档属于同一作品，不把客户端可控字段当作安全身份。`collaboration_task_access.py:18-47` 已提供服务端保留 `_collaboration_grant`；现有模型绑定只序列化 vector `ResolvedProjectModel`，没有 keyword 选择序列化。可由原语所有者增加严格版本化、不可回显的共享选择载荷，记录 keyword/vector、原因、三方身份和原授权轮次，vector 再嵌套服务端模型绑定。保留键覆盖/剥离须由受理方完成。keyword 一经受理不可因为后来开放授权/新增配置而自动变 vector；vector 也不可因失效改用本人模型或其他作者配置。

旧作者任务无新字段时，仅在可信 `Task.user_id == project.user_id` 且确认是旧记录的分支显式兼容，用当前作者策略重建安全绑定/采用既定旧作者行为；旧协作任务缺证明一律失败关闭。备份导入同步重建关键词及由新作品作者自行触发的重建保留（`project_transfer/importer.py:1537-1545,1612-1617`），不让导入逻辑误认协作者或信备份中模型身份。

当前 `index_status` 返回作者配置 `name · model`（`kb_service.py:369-385`）；`resolve_embedder` 的错误可包含配置名及详细异常（`retrieval_service.py:201-209`）；历史 `job.warnings` 被持久到文档，再由协作者可访问的 `list_documents` 直接序列化（`kb_index_service.py:331-341`、`kb_service.py:100-105`）。包括历史值在内，`KbDocumentOut.warning/error`、索引状态、任务进度与错误都应按身份作安全投影；用户可看到「关键词/作者承担费用/请联系作者」等业务信息，但不能看到作者配置名、ID、端点、密钥、可逆指纹或原始 provider/SQL 错误。作者自身原有安全诊断应保留。`kb.index` 现有 handler 不写 GenerationLog，不能虚构零费用或混淆统计事实。

### 发送、每批写入、清理与锁序

- 在任务创建、领取/build、每次实际 HTTP 发送之前及每次业务写入/状态刷新之前，使用可信 actor + 原 grant 重验活动项目、`ai.use`、邀请身份、作者共享授权及已绑定模型；不要在校验失败后重新解析成新邀请或新模型。撤权后重新邀请不恢复旧任务。短提交事务采用现有 `require_project(for_update=True)` 顺序：认证写门禁 → actor/作者 User 按 ID 排序 → 如涉及可见性策略写再取发送写门禁 → 项目父行锁 → 锁后重读成员与配置；发送使用独立短 lease，认证发送锁 → 项目可见性发送共享门禁 → grant/绑定/模型重验，持至响应头而不持到响应体。不要保留任何业务写事务跨模型调用，SQLite 下也不要让打开的业务写 session 等待另一个取得物理写屏障的发送 session（`ai_dispatch.py:91-101,170-198`）。具体实现需同认证/配置原语所有者确认所有调用者的统一顺序，避免先锁项目再反向拿 User。
- 截至本次检查，`ai_config_service.lock_config_owner` 已为作者配置 create/update/default/delete 引入 PG 共享/独占 advisory（SQLite 同一 User no-op 写屏障），`project_ai_config._validate_binding` 在 `lock_config=True` 时取得该共享屏障并重查当前默认/最新（`ai_config_service.py:38-65,121-151,201-219`；`project_ai_config.py:288-319`）。这是其他代理的进行中变更。E6 实际发送 lease **及结果写事务**均须在不反转既有锁序的前提下采用同一屏障：仅锁旧配置行不能防无默认时插入新默认造成 phantom。新增/删除/改默认/改模型/改 endpoint 都需要交错验证。写事务若模型已经改变则失败关闭，旧 A 的成功响应不能覆盖当前作者模型 B；有权限且当前模型仍为 B 的显式 B 重建才可有意替换旧 A 行。
- 当前 `_embed_stale` 在一个 session 查询行，网络后直接用旧 ORM 行赋值 `embedding/key` 并使旧 `version + 1`（`kb_index_service.py:236-260`；`retrieval_service.py:360-365`）。在同批 HTTP 等待期间记忆可能改正文、删除，或新作者模型完成重建；旧响应仍可能把旧正文向量写进新正文，甚至覆盖缓存版号。推荐：短读得到不可变输入快照后结束读事务；每次 HTTP 请求、重定向/重试分别取得 guard；返回后启动短写事务，按上述锁序复验，再以 `id + project_id + source + ref_id + version + 内容身份` 条件 CAS 写入当前绑定向量并原子递增 `version`，冲突丢弃并重读/重排。包括缓存命中的向量也要对写入进行同样校验；已发字节和外部费用不能撤回，但撤权后不能继续后续发送或提交旧结果。
- orphan memory 检索行删除也要按同一快照身份/版本进行受控 CAS，不能因较早查询未找到实体而无条件删掉并发重建的新行。每次文档分块、`indexing`、`ready`/`ready_keyword_only` 和 rebuild 汇总状态也需项目归属、actor/grant、任务 UUID 与运行轮次约束。`on_abort` 允许仅清理本轮占用的受限状态，不写旧正文、不把新任务状态改成 failed；当前文档无清晰任务轮次字段，不能只比较 pending/indexing，必须设计可靠的持久化 provenance/CAS 或可证明的关联，不能假设状态就能证明归属。
- `run_index_job` 的 broad `except Exception` 会直接 `_set_doc(failed)`；`_run_document`/`_run_vectors` 的 `except AIProviderError` 又会捕获其子类 `VisibilityPolicyError`（`kb_index_service.py:289-303,331-341,366-370`）。权限或配置变化必须从 provider SDK 异常链恢复为受控策略错误，并在这些分支**先重抛**；外层不能将其视为普通网络故障或成功关键词任务。`describe_error` 只恢复 `RequestPolicyError`（`ai/providers/base.py:25-30,97-110`）；`embed_query`、`knowledge_text`、`memory.recall` 目前只对 `VisibilityPolicyError` 特判（`retrieval_service.py:248-260`、`kb_service.py:318-336`、`memory_service.py:421-448`）。需要统一能跨 OpenAI/Gemini SDK 包装恢复的安全策略异常，并保证这些 catch 正确传播；只有真正的提供方失败/超时才允许关键词降级。保持最终任务结果及错误投影与任务引擎协议一致。
- `Embedder.embed` 内部也以 32 条分批，去重、截断 2000 字、检查维度并缓存（`retrieval_service.py:124-159`），因此「每次调用前验证一次」不保证逐 HTTP 包/SDK 重试受保护。现有 `DispatchHooks` 在 HTTPX 每次 request 前获取 lease、收到 response headers 放锁并在异常/取消关闭（`ai/providers/dispatch_hooks.py:22-53`），OpenAI/Gemini 应复用；`guard_embedder` 现在只传 visibility stamp，须统一组合 actor authorize 和绑定 verify（避免重复 guard 丢失已有回调），不得只用 FakeLLM 证明真实发送。

### 向量空间、跨作品和缓存

`config_model_key = provider:model`，持久 key 是截断到 200 字符的 `provider:model:dim`，仅校验前缀及维度（`retrieval_service.py:105-118,135-141`；`entities.py:1006,1025-1026`）。这不足以区别同 provider/model/dim、不同 endpoint/后端空间或模型版本；不可因为个人配置同名就认定它与作者空间兼容。建议由共享向量原语和检索所有者共同确定不暴露端点或配置秘密的版本化稳定空间标识，例如对规范化 endpoint/provider/model 等必要空间属性作服务端不可逆标识，明确 key 轮换、模型版本变化语义；不能仅使用含密钥的配置绑定 fingerprint 作为空间定义而让每次密钥轮换都无谓改空间。新格式须兼顾 200 字限制、模型名含冒号、维度验证、所有 stale/search 比较及缓存。旧 key 没历史 endpoint 证明时不能声称向量必定兼容；可保留旧字节、按 stale 走关键词并由真正授权的作者空间重建。此项可能影响旧作者既有混合召回，需要与任务兼容验收协调，不能默默删除旧数据或在没有空间证明时强行复用。

`RetrievalChunk` 只有 `(source, ref_id)` 唯一约束，没有 ref 的业务外键（`entities.py:1009-1026`）；`upsert_many`、`set_flags`、`delete_refs` 只按 source/ref 查询或修改（`retrieval_service.py:276-356`），错误归属数据可能影响别书的检索行。`_embed_stale` 对 memory 联查 `StoryMemory.project_id`，但 kb 直接发送 retrieval row.text，没有验证 `KbChunk → KbDocument.project_id`；`agent/tools/read.search` 对 KB 原样返回 hit.text，而仅 memory 二次回查本项目正文（`agent/tools/read.py:305-337`）。业务写/删除、发向量前和返回 Agent 文本前应验证 source、ref 与其所属项目一致；如果存在冲突应安全失败并回滚而不是仅加 project filter 后默默插入/覆盖冲突。调整 CRUD 签名时迁移章节删除/改号、Agent undo、备份导入等所有调用者；注意错误不回显别书 ID/正文。

`search` 以 `(project_id, source)` 为进程缓存键、签名为 count/max(id)/sum(version)/sum(chapter_no)；仅所有候选都有当前模型 key 才请求查询向量，否则整次关键词且不发向量（`retrieval_service.py:442-477,589-659`）。cache key 不是授权证明；调用者在复用缓存及返回结果前仍要校验本轮资格。最终文本查询当前只以 `RetrievalChunk.id IN (...)` 过滤（641-643 行），可一并加 project/source 防并发或异常数据越界。模型失效与无授权不应转换为可继续后续 AI 的提供方降级。

### 所有检索消费者与衍生入口

| 调用路径 | 当前行为 | 接入要点 |
| --- | --- | --- |
| 章节 generate/revise → `context_builder.build_chapter_context` → `retrieval_blocks` | 以 `generation_service.py:325` 的任务 user 默认配置构造 embedder；context builder 加 visibility stamp，KB + memory 共用 | 从真实任务 actor、原 grant 和作者共享选择构造；仅 keyword 时不调用作者/本人向量；重验后再组装/返回，维持记忆内容隐私 |
| 章节 context-preview | `context_builder.py:1025-1045` 当前严格作者、actor 配置；可发送查询向量 | 同步预览仍要求 `ai.use`；纳入共享绑定和真实发送 guard |
| 向导 | `wizard_service.py:262-271` → `knowledge_for_project` 选作者，但未传已构建的 `scope.stamp` | 增加真实 actor/grant/选定共享模型和作用域；不能仅因项目 owner 即有共享授权 |
| 实体生成 | `entity_ai_service.py:722-739` 走同 helper，只有 selected 非空时才传 stamp | 查询可能携带受保护作品/设定信息；按实际 scope 统一 guard；保留原实体 AI 权限 |
| 大纲续写/扩写 | `outline_ai_service.py:426-446` 直接选作者 embedder，先查 KB 和 memory，再继续模型生成 | 不能仅 guard 后续主模型；两种查询向量出站也要绑定 actor/grant/visibility |
| 灵感 | `inspiration_service.py:449-463` 只在当前用户是作品作者时查询作品知识 | 保留此作者路径及旧行为，除非独立需求授权扩充；仍应使用同一致性模型路径 |
| `/kb/search`、`/memories/search` | API `ai.use` 预检，service `get_owned_project`，各从 user 个人模型检索 | editor 按共享选择；无作者授权返回关键词；viewer 不调用模型；显示安全降级原因 |
| Agent `knowledge` / `memories` | `agent/tools/read.py:305-345` 使用 actor 个人 embedder，scope 有时只含 visibility；memory 二次取原正文 | 复用可信 Actor/原 grant/共享选择与策略错误传播，验证 KB 跨书归属；仅修既有可用入口，不扩大协作者 Agent/AI 能力 |
| KB helper / 底层召回 | `knowledge_for_project` 当前无 actor，自动选作者；`knowledge_text` / `memory.recall` 会吞大多数异常 | 改成显式可信上下文，不依赖隐式当前用户；策略失败须穿透，普通提供方失败仍按既有规范降级 |
| 衍生索引任务 | HTTP 记忆、分析成功、batch、Agent undo 均只传 project_id | 将写入锁内取得的真实 actor 与原 grant 贯穿提交后的新事务；新事务重验证相同 grant，再排队，不可按当前成员重新签发；无法证明触发者时不排 |

`enqueue_memory` 当前只按项目去重 queued 任务（`kb_index_service.py:119-128`）：如果 A 任务待排队而 A 撤权，B 的后续记忆改动可能被错误合并到 A，随后 A 失败而 B 无任务。按 actor + grant 可兼容身份去重，或采用能证明共同授权且能重唤醒待处理工作的方案；项目级跨实例串行/安全 CAS 与身份级去重是不同问题。

### 建议分工与调用边界

1. **模型原语所有者**：`project_ai_config.py`、`ai_config_service.py` 的向量选择序列化、当前默认复查、共享配置屏障及安全策略错误接口；变更前与其协商，勿并发重写。
2. **检索基建/索引所有者**：`retrieval_service.py`、`kb_index_service.py`、`task_engine/handlers/kb.py` 一体设计空间身份、对项目有界的 CRUD、批次快照/CAS、受限清理与 guarded embedder。生产代码与持久化 schema 若要新增占用轮次需单一所有者及迁移。不要依赖其他所有者私自改同一核心文件。
3. **知识库/记忆入口所有者**：`kb_service.py`、`memory_service.py`、`api/v1/kb.py`、`api/v1/memories.py` 迁移真实 actor、接受/发起条件、状态投影和提交后衍生身份；先冻结与 2 的函数签名再并行。
4. **任务及其他消费者所有者**：`task_service.py`、`task_engine/engine.py`、`collaboration_task_access.py` 由任务隐私/引擎代理拥有；`generation_service.py`、`context_builder.py`、wizard/entity/outline、batch/analysis 和 Agent 由相应现有代理接线，提供共用的 selection、grant、guard、enqueue 签名，不覆盖对方文件。备份导入及旧任务回归归协调者确认。

### 必要验证（建议，未在本研究中执行）

- SQLite 角色矩阵：作者、已接受 editor 开/关授权、viewer、管理员和管理员兼 editor；editor 有自己的不同/同名向量配置；上传与记忆改动应关键词即时可搜、0 次向量请求、只清受影响条目的旧向量；两种手动重建未经授权都 403 且快照逐列未变。授权开启后断言实际选作者模型，任务 owner/配额为 editor、费用显示来源为作者；取消授权/撤权/重邀请中止旧任务，新任务正常。
- 跨所有消费者的集成测试：章节生成/预览、向导、实体、大纲、直接知识库/记忆搜索、Agent 既有入口；参数化无授权/有授权/作者无配置/协作者自有配置。对 provider 真实调用次数及出站正文作断言，保证隐藏设定名和外书内容不发送。普通 provider 失败仍关键词；权限/绑定变化不可降级或继续主模型发送。检查包括历史文档 warning/error、任务输出、index-status 的所有非作者响应及日志不含配置 ID、名称、endpoint、key、指纹和 SQL 参数。
- 实际 HTTP + PostgreSQL 双应用/双连接：复用 `tests/test_ai_visibility_dispatch.py:56,140` 的本地受控 HTTP server 与 `tests/test_collaboration_access_postgresql.py:24` 的隔离 schema 思路（需要隔离 PG 测试 URL），验证 OpenAI 和 Gemini embedding 的发送前后门禁、redirect/SDK retry 每跳、挂起响应头时撤权/降级/开关授权/切默认/改 endpoint/停用 actor 的两种提交顺序；证明撤权已提交先于 send 时 0 请求，send 获 lease 先行时对方等待至响应头，释放后不能继续下一批或落旧结果。响应体不应长期持写门禁。FakeLLM 和 SQLite 不能证明 PG 锁竞争。
- PG 索引竞争：真实 HTTP A 挂起期间并发修改、删除、重建同条记忆/KB，或作者从 A 切 B 并让新任务完成，再返回 A；断言新正文、B 的 embedding/key、递增 version、缓存签名及搜索结果均不被 A 覆盖；同时测试不同项目、相同项目两实例不同 resource_key、同条目重复队列、同项目两 editor 不同授权轮次。两实例任务可以并跑，但只能产生符合授权和 CAS 的结果，不假设本进程 serial_key 足够。
- 针对错误归属手工构造 retrieval row 指向外书 KB/memory 的损坏状态，在 read、写、删除、向量发送、Agent 返回各入口断言不接触外书且无敏感详情；覆盖章节删章/章号平移、undo 和备份导入。针对无默认时新建最新配置、默认 A→B、A 的 endpoint/model 更改以及删除，检验作者配置共享屏障阻止 phantom 与死锁。
- 故障/恢复：验证每批取消、任务失败、旧任务 on_abort 与新任务抢占、旧作者无绑定数据、keyword 未自动升级、向量空间版本/旧 key、重启恢复及异常 commit；仅本轮状态被清理，成果不写错模型、不泄露错误。复用 `tests/test_kb.py`、`tests/test_memories.py`、`tests/test_retrieval.py` 的现有功能基线，不以简单 mock 内部函数代替真实 HTTP 和数据库竞争。

### External references

本次为仓库内部实现与规范调研；未使用外部资料。SQLAlchemy/SDK/数据库的具体超时及隔离差异，以当前依赖和上述真实集成测试为准。

## Caveats / Not Found

- 本文为并行代码库上的静态快照，不证明线上已有越权；特别是 `project_ai_config.py`、`ai_config_service.py` 和任务授权原语正在被其他代理实时改动，落地时以最新代码及其测试为准。
- 本次未执行 SQLite、PostgreSQL 或真实 HTTP 复现；向量竞态、跨实例 serial 缺口和异常转换是源码支持的风险分析，需上述反例落实。任务级向量 selection 序列化和文档占用轮次 provenance 在检查时未找到，具体方案需与任务/模型所有者商定。
- 旧 `retrieval.md` 允许提供方故障降级并展示作者自身配置名；新协作任务新增保密和鉴权约束。实现完成后应由规范所有者同步规范，而非在本研究中改规范。
