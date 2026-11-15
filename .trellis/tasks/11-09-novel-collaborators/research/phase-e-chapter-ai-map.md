# Research: 章节、场景、富文档与封面 AI 的协作接入

- Query: 盘点 E1/E3/E4 中同步改写、章节生成、富文档采纳和封面的受理、模型解析、真实出站、保存、衍生任务及失败清理链路，明确协作身份、模型费用和并发边界。
- Scope: internal
- Date: 2026-11-09

## Findings

以下为静态调用链研究；代码正由其他代理并行修改，行号以阅读时为准，集成前应以函数名再次定位。本研究未修改产品代码、未调用模型，也未把已有测试结果当作协作验收结果。

### 相关文件

| 文件 | 作用 |
| --- | --- |
| `backend/app/api/v1/chapters.py:105`、`scene_ai.py:43` | 章级/场景级生成、润色、重写任务入口，真实 HTTP 用户传给 `task_service.create_task`。 |
| `backend/app/services/task_engine/handlers/chapter.py:47` | 创建/重试时抢占、生成 `TaskSpec.input`；从任务重建 `ChapterJob`；失败回退和成功后自动分析。 |
| `backend/app/services/generation_service.py:154` | 章节生成受理、组装多场景上下文、续写、最终保存和失败回退。 |
| `backend/app/services/task_service.py:170`、`task_engine/engine.py:339` | 任务统一受理、锁、真实用户入队、领取、运行、状态完成和 SSE hub。此文件已有其他代理负责。 |
| `backend/app/api/v1/chapter_versions.py:74`、`scene_ai.py:64` | 章、场景、作者区块的同步改写预览和采纳；章级预览路由只有 `chapter_id`。 |
| `backend/app/services/rewrite_service.py:81`、`rich_document_section_ai.py:47` | 选区检查、内容/规划快照、模型和消息组装、同步流及本人日志绑定；区块有一次性发送确认。 |
| `backend/app/services/rich_document_review.py:175` | 将需要人工核对的富文档候选与断点持久化；按任务本人和三重随机身份读取/采纳。 |
| `backend/app/services/ai_dispatch.py:156`、`app/ai/providers/dispatch_hooks.py:12` | 模型真实传输的可见性发送 lease；HTTP request 前取锁，收到响应头后释放。 |
| `backend/app/services/project_ai_config.py:33` | 并行开发中的服务端项目模型/共享向量绑定；当前尚未连接这些调用链。 |
| `backend/app/services/scene_ai_context.py:28` | 场景、正文/规划、设定与可见性戳；仅绑定内容代次，不包含协作轮次。 |
| `backend/app/services/context_builder.py:834`、`retrieval_service.py:124` | 生成上下文和知识/记忆检索，向量服务实际请求发生在文本请求之前。 |
| `backend/app/services/project_service.py:75`、`style_service.py:354` | 项目信息/写作风格描述及风格解析；注意前者目前按项目作者取描述。 |
| `backend/app/services/analysis_service.py:673`、`task_engine/handlers/analysis.py:72`、`kb_index_service.py:98` | 正文成功后的自动分析、分析成功后的记忆向量派生任务；现有任务归项目作者。 |
| `backend/app/api/v1/project_cover.py:40`、`cover_service.py:68` | 封面提示词及图片生成；人工上传/删除已有单独协作授权。 |
| `backend/app/ai/providers/openai_compat.py:103`、`gemini.py:179` | 实际图片 SDK 请求；OpenAI 图片 URL 下载由独立 HTTP 客户端执行。 |

### 受理、任务与生成链

1. 章级 `/chapters/{id}/generate|polish` 和场景级对应路由将真实 `user.id` 与参数交给 `create_task`；场景 `target` 被显式固定（`chapters.py:105`，`scene_ai.py:38`）。`ChapterHandler._claim` 解析请求后调用 `generation_service.validate_and_claim`（`handlers/chapter.py:69`）。
2. `validate_and_claim` 目前先用 `get_owned_chapter/get_owned_project` 严格作者锁，在校验场景、版本、批量冲突、模型、风格、Skill 后将章状态 CAS 为 `generating`，不提交（`generation_service.py:154-216`）。`create_task` 再按 `spec.project_id` 使用严格 `get_owned_project`，以 `Task.user_id=user_id` 保存并提交（`task_service.py:170-210`）。单独改一层 helper 会在另一层被拒或使抢占与授权脱节。
3. 章级无项目 ID：应先在 `no_autoflush` 中按章节 ID 安全定位父项目，再以真实 actor 明确要求 `ai.use`，按认证/User → 必要发送写门禁 → 项目父行的约定取得锁，并在锁后重新限定章节属于项目、校验 accepted editor 的相同邀请身份。不得修改原 `get_owned_chapter` 的严格语义；人工 `get_manual_chapter` 只接受 `content.read/edit`，并不能代替 `ai.use`。
4. `ChapterClaim` 当前只存 `ai_config_id` 等；`TaskSpec.input` 含 `request`、`chapter_id`、`prev_status`、`ai_config_id`、`skill`（`handlers/chapter.py:101-114`）。但 `build_job` 又从原请求的 `ai_config_id` 重新解析配置（`handlers/chapter.py:117-138`），忽略受理时已解析的具体身份。默认配置变更后，现状会换配置。协作实现需让可信服务端 `Task.input` 保存原模型绑定及邀请轮次，运行、重试、续跑和每次发送对原绑定重验而不重新选择；显式 ID 始终只能是 actor 本人的。不能将作者回退得到的配置 ID 当作客户端显式选择。
5. 主代理约定服务端保留键 `_collaboration_grant={"invitation_id": UUID字符串, "revision": 正整数}`，作者可省略；模型绑定使用服务端保留键 `_project_model`。这些值由受理/派生流程写入，不能从客户端 `request` 接收、不能作为 TaskOut、SSE、错误或日志的公开字段。由实现者与任务守卫代理确认最终序列化和不可伪造边界。
6. 重试现在提取旧 `request`，刷新章/规划版本后再次 `_claim`（`handlers/chapter.py:55-67`）。仍需核验旧任务的 actor、原邀请和原模型授权，不可在撤销后重新邀请或重开作者授权时为旧任务自动续签；确需新轮次应发起新任务。旧作者任务与已有 Skill 快照行为须兼容。
7. `_claim_and_run` 按 `Task.user_id` 计算配额（`task_engine/engine.py:345-379`），然后 `build_job` 重建上下文；真正的场景/自动续写每次分别调用 provider（`generation_service.py:584-625`）。后台检查不能依赖原 HTTP 会话或进程内缓存，必须验证账号仍启用、当前身份与任务原轮次；已禁用或存储不可用均失败关闭。

### 真实发送和共享向量

`guard_provider` 已支持两个独立钩子（`ai_dispatch.py:170-181`）：`authorize` 在共享发送门禁前执行；`verify` 在共享门禁和可见性戳检查后执行。一次正确的协作发送应在 `authorize` 用请求中捕获的 `AuthenticatedActor`（同步 HTTP）或任务真实用户（后台）走 `require_authenticated_for_send`，然后在 `verify` 中，在现有项目读屏障及共享发送门禁保护下重验原 grant、模型来源/归属/指纹和 `SceneContextStamp`。`project_ai_config.revalidate_project_model(..., lock_config=True)` 当前可在 PostgreSQL 上共享锁住配置，避免校验后至响应头期间被删除或修改（`project_ai_config.py:177-224`）。实际调用必须安排一致锁序并验证，不得在持发送 lease 时反向请求全局认证写锁。

`SceneContextStamp` 的 `sending=True` 还会获取项目行共享读锁，然后重读目标章和全局版本（`scene_ai_context.py:83-106`）。协作 grant/model 核验须置于取得发送门禁后的受保护部分，不能只在构造 Job 时做一次；每个场景、自动续写、SDK 重定向/业务重试均须使用原绑定。`DispatchHooks` 在每个真实 HTTP 请求前申请 lease、响应头后释放（`dispatch_hooks.py:22-38`）；不持写事务等待整个模型流。`ai_dispatch.py:185-195` 对不发 HTTP 的 FakeLLM 只执行检查，不取得真实物理发送锁，因此它不能证明撤权与发送竞争已串行。

生成 `_prepare_scene_step` 先 `build_chapter_context(..., embedder=job.embedder)`，之后才包装文本 provider（`generation_service.py:402-423`）；`context_builder` 内的 `retrieval_blocks` 可先触发向量出站（`context_builder.py:853-857`）。现状 `get_embedder(db, user_id, ...)` 选择真实用户默认向量（`generation_service.py:325-327`），这不满足作品共享向量空间。应先用 `resolve_shared_embedding` 决定作者向量或关键词模式，禁用授权时不得请求作者模型，也不得用协作者其他模型覆盖作品向量；向量 provider 每批真实发送也须绑定相同授权身份、送出前重验，不能仅保护之后的文本发送。现有 `guard_embedder` 只传可见性戳（`retrieval_service.py:162-168`），已有 wrapper 与 `guard_provider` 的重复包装保护需统一设计，避免二次包装绕过或因不同回调误报。

### 私人风格、提示词、输出与日志

`style_service.resolve(db, user_id, ...)` 对显式风格和继承风格按 `user_id` 或全局预设筛选（`style_service.py:354-370`），已经提供正确的单独权限过滤，协作者找不到作者私有风格时不能改为作者身份查询。但章节 `build_chapter_context` 另调用 `project_info(db, project)`（`context_builder.py:859`），后者 `default_description(db, project)` 使用 `project.user_id` 查作者默认风格描述（`project_service.py:75-77`、`style_service.py:381-388`）。因此仅修 `resolve` 或 HTTP `style_id` 仍会把私人风格送给协作者的模型；向导、实体、大纲、分析和 Agent 也调用 `project_info`，须以真实 actor 为显式参数逐调用点迁移，并允许可见的全局预设、对不可继承风格给明确提示。不要为了兼容把缺失 actor 默默当成作品作者。

章节生成成功 `generation_service._save_chapter_result` 与富文档采纳均直接 `ChapterOut.model_validate(chapter)`，分别进入任务结果/SSE（`generation_service.py:519`、`rich_document_review.py:387`）；章级改写采纳 `rewrite_service.apply_rewrite` 返回 ORM Chapter（`rewrite_service.py:384-390`）。均须复用 `chapter_service.manual_chapter_views` 等面向真实 actor 的安全投影（`chapter_service.py:157`），过滤私人 style ID 及异常跨项目关联，而不改变原数据库绑定。日志已在生成与改写处传 `actor_user_id=job.user_id`（`generation_service.py:553-561`、`rewrite_service.py:230-245`）；作者模型配置 ID 只可作为服务器内部绑定使用，对协作者的 GenerationLog、任务结果及嵌套公共对象须脱敏，不能删除内部需要的真实成本/撤销身份。日志读取隐私正由其他代理负责。

### 同步改写和区块一次确认

- `prepare_rewrite_job` 当前通过严格作者 helper 找章/作品，再解析当前用户模型、风格、Skill 和设定；它为 provider 加的 `verify` 只有场景戳，没有协作或模型绑定检查（`rewrite_service.py:97-106,130-135,183-200`）。退出请求 session 后 `run_rewrite_job` 原样 yield 每个 `delta`；成功前只检查场景戳并在另一个 session 保存日志、返回全文和 `log_id`（`rewrite_service.py:247-276`）。协作版需要安全的流存活复查或撤权通知，并以数据库为权威；在授予后发出的内容无法撤回，但撤权后不得继续输出后续帧或成功结果。场景戳检查、授权重验和成功记录/返回不能靠彼此分离的检查假装原子。
- `prepare_section_rewrite` 在锁内提交用户与 `request_id` 的一次性 receipt，随后以局部 `sent` 锁阻止 SDK 重试/重定向重复发送（`rich_document_section_ai.py:129-157`）。它在 `authorize` 已检查初始 token，但 `verify` 当前只检查场景戳。应在首次受控请求门禁内补原 grant/model 校验，只有全部检查通过才标记首次发送；撤权或权限存储失败不能触发重发，也不能弱化 receipt 的去重语义。
- `rewrite_service._apply` 目前只按 actor、项目、章筛选 log 并核验场景戳；旧章级入口允许无 log 的乐观替换（`rewrite_service.py:295-351`）。`apply_section_rewrite` 绑定 receipt 与本人成功 log，但同样没有授权轮次（`rich_document_section_ai.py:174-210`）。场景戳不包含 invitation UUID/revision：撤权再重邀后，单凭当前 editor 身份可能采纳旧轮次预览。协作者预览需将服务端原轮次与成功记录绑定，在项目/发送写门禁锁内核验；无法证明轮次的旧无日志章级采纳不能作为协作 AI 采纳，作者兼容路径保持原行为。内部存储的身份不可由客户端伪造，公开日志需过滤保留字段。

### 富文档候选、自动分析与受限清理

`_save_chapter_result` 当前经 `lock_author_visibility_write(..., manage=False)` 和严格作者 chapter helper 取得锁，检查场景戳，然后要么持久化待审核候选，要么写正文、本人日志与版本并同事务提交（`generation_service.py:447-528`）。协作版必须在写屏障内重验原 grant、绑定模型和 Task UUID/执行轮次，权限变化不能落旧结果；候选 `persist_pending` 还检查 `task.id/statistics_key/status/admin_cancel_operation_id` 并保存大体积快照（`rich_document_review.py:175-215`）。

`get_review` 通过 `get_owned_task` 加 `(task_key, run_key, token)` 取候选（`rich_document_review.py:218-278`）；`accept_review` 在锁后重取任务/提案、验证原文和位置、写日志/版本，并可能继续批量或派生分析（`rich_document_review.py:281-405`）。采纳不仅要当前 actor 是任务本人，还须在锁后验证作品权限和候选任务原邀请轮次；隐含的批量继续和自动分析必须继承真实 actor 与授权，不能因为当前登录身份而替旧任务续签。内部候选包含 `log_values` 和正文，不应直接放入响应；既有分块哈希与幂等采纳不可删弱。

普通章任务成功后 `on_success` 调用 `enqueue_auto(ctx, project_id, chapter_id)`（`handlers/chapter.py:172-175`），采纳时调用 `enqueue_auto_in_transaction`（`rich_document_review.py:394-395`）；后者将 `Task.user_id=project.user_id`（`analysis_service.py:673-719`）。分析完成的 `AnalysisHandler.on_success` 和 batch 又用没有 actor 参数的 `enqueue_memory`，后者同样把索引任务归作者（`handlers/analysis.py:72-74`、`kb_index_service.py:98-140`）。在开启协作者生成或采纳前，需将真实触发者、原授权和相关模型绑定穿过全部后续任务；不能证明身份或权限已变时不应创建衍生任务，且任务额度只计真实 actor。分析/索引本身的出站、每批写入仍须各自接入门禁。

`run_chapter_job` finally 调用 `_restore_status`，其 UPDATE 只按 chapter ID 和 `status=generating`；引擎完成失败时再调 `ChapterHandler.on_abort`，同样只按章 ID/状态回退（`generation_service.py:432-440,663-669`、`handlers/chapter.py:154-170`）。两次回退之间可能出现批量任务或其他新轮次抢占，旧清理不能把新工作从 generating 复位。仅检查旧 Task 仍存在不足以证明当前 chapter 抢占属于该任务；需要持久、原子绑定到章占用的任务 UUID/本次 run 身份（或等价可证明方案），在失败/取消/恢复的 UPDATE 中 CAS，配合项目父锁与资源约束。旧作者任务及升级前异常占用需单独设计安全恢复；不能为清理而重新写旧正文，也不能用无身份的 `generating` 作为充分条件。

### 封面

`default_prompt` 目前严格作者且 `db.get(WritingStyle, project.default_style_id)` 不按 actor 过滤（`cover_service.py:68-71`），模板会拼入风格名称、描述和 tone（`ai/prompts/cover.py:6-15`）。放宽给协作者前须按真实 actor 过滤，决定本地提示词入口的明确能力，至少保持既有管理员/非成员行为；实际图片生成必须 `ai.use`，viewer 不能调用。

`generate_cover` 目前严格作者、解析本人 image 配置后直接 `provider.generate_image`，无 `guard_provider` 或授权/模型/可见性 lease（`cover_service.py:74-96`）。应采用 image 类型的绑定和相同发送协议，完整校验端点；响应返回前再检查身份/授权，防撤权后的图片预览继续交给调用者。OpenAI 兼容 provider 可能在 size 明确 400 时第二次发送（`openai_compat.py:119-128`），第二次必须重新过门禁。返回 URL 图片时 `_download_image` 使用没有 dispatch hooks 的独立客户端（`openai_compat.py:103-117`）；即使下载不是模型推理请求，也要评估撤权期间继续获取/返回图片的边界，并保留 SSRF/redirect/10 MB 防护。Gemini 图片也通过注入的 HTTPX hooks 发请求（`gemini.py:182-224`）；需真传输反例而非仅 FakeLLM。

### 建议并行所有权和安全实施顺序

1. **先冻结公共契约**：模型解析代理完成 `ResolvedProjectModel/ResolvedSharedEmbedding` 与只读重验接口；任务守卫代理完成 `_collaboration_grant`、SSE 与系统终态裁剪；统一给 Job、任务与日志内部身份的序列化/反序列化及锁序。处理项目风格 `project_info/default_description` 和共享向量守卫的代理必须集中拥有这些公共文件，逐调用点要求真实 actor，避免不同代理同时改 `context_builder.py/retrieval_service.py`。
2. **章节生成一组**：`generation_service.py` + `task_engine/handlers/chapter.py` + 章/场景生成专用测试；与任务服务所有者约定输入/领取/轮次协议，与批量代理约定 `build_chapter_job/claim_chapter` 的新参数。受限 cleanup 的章占用身份必须先有统一设计与持久化，不能单靠更改 finally。
3. **同步改写/区块一组**：`rewrite_service.py` + `rich_document_section_ai.py` + 两个改写路由和专用测试；与生成组先约定 `ensure_outside_batch`、安全章投影、统一发送验证 helper 的接口，不并发编辑生成组文件。
4. **富文档采纳一组**：`rich_document_review.py` 和专用测试；依赖任务守卫/章占用/自动分析入队契约，与章节生成、batch、analysis 负责人协调。日志/Agent 隐私代理拥有生成日志服务，不要重复修改。
5. **封面一组**：`cover_service.py` 与封面 API/测试独立，使用统一模型绑定/发送验证和已完成的 actor 风格投影。图片 SDK provider 需要改动时另行协调公共所有权。
6. 任何一组仅在其实际发送、落库、撤权、隐私及衍生链闭环后才能打开对应协作入口；这些中间阶段不可部署，且不能先把 `get_owned_*` 扩权来让用例通过。

### 必需的真实 PostgreSQL 并发与隐私反例

在专用隔离库、随机 schema 下，以两连接和本机 HTTP 模型替身记录真实请求，至少验证：

1. 邀请撤销/降级或作者模型授权关闭先提交 → 生成、改写、区块、封面及每个向量出站零请求；发送 lease 先取得 → 撤权等待到响应头，完成后后续场景、续写、重试及新网络发送被拒。模型配置在验证后并发修改/删除，验证 lease 持锁期间不能换密钥或发已失效绑定。
2. 模型已回响应头但正文/SSE 未完时撤权：之后的改写 delta、done、任务订阅及最终正文/提案不继续泄露或落库；保留已发送字节和已发生费用的事实。进程通知丢失或重启后数据库复查仍拒绝。
3. 同一协作者撤销、重邀或修改 revision 后，旧 task、同步改写 log、区块 receipt、富文档提案、批量续跑不可续用；新的独立任务可按新授权工作。viewer 与管理员+viewer 的 AI 均拒绝，纯管理员无法读取私人结果，editor 仅能读自己的日志。
4. 旧任务失败后，在旧 `_restore_status` 与 `on_abort` 之间让另一个批量/单章任务抢占同章；旧清理不得复位新占用，且新任务最终提交正文。取消、异常、服务重启及任务 ID 复用也需验证 UUID/run CAS。
5. editor 本人模型优先；缺配置且作者授权才使用作者配置；本人无效不回退；显式伪造作者 config ID 不被当作本人选择。记录实际 actor 和外部调用次数，HTTP 日志、ChapterOut、任务结果、封面 prompt 均不泄露作者私有风格及模型配置 ID。
6. 协作者章生成、富文档采纳成功后，自动分析和记忆索引均归 actor、同轮次且计其任务配额；在正文提交后、自动派生前撤权时不以作者身份创建任务。无作者向量授权时整个路径零作者向量调用、不覆盖旧向量；授权后查询/索引使用作者共享空间。
7. 复用 `test_ai_visibility_dispatch.py` 本机 HTTP 服务模式、`test_rich_document_postgresql.py` 的候选/CAS/重放模式、`test_scene_ai_postgresql.py` 的多场景原子性，以及 `test_project_cover.py`/`test_rich_document_section_ai.py` 的现有作者兼容回归；协作新的实际 PostgreSQL 测试不可由 skip 或纯 mock 代替。

## Related Specs

- `.trellis/tasks/11-09-novel-collaborators/design.md` §§2、5.1、5.3、6、8–9；`implement.md` D3、E1–E7。
- `.trellis/spec/backend/setting-ai-visibility.md`：发送 lease 和可见性戳；`.trellis/spec/backend/scenes.md`：场景快照与原子保存。
- `.trellis/spec/backend/rich-documents.md`：一次性区块确认、持久提案和采纳；`.trellis/spec/backend/project-cover.md`：图片和 SSRF 限制。
- `.trellis/spec/backend/rbac-quotas.md`：真实 actor、认证门禁、no-autoflush 和任务配额；`.trellis/spec/backend/retrieval.md`、`batch-generate.md`：共享向量、分批和批量任务。

## External References

无；此项仅核对本仓代码和任务规范，不依赖外部资料。

## Caveats / Not Found

- 本文未执行或声称通过任何协作 AI/模型发送测试；尤其真实 PostgreSQL 的邀请撤权与 AI 发送/落库竞争仍待实现与运行。
- `project_ai_config.py` 和任务访问/SSE 模块处于并行开发，最终签名、序列化格式和代码行数可能变化；不能凭本文的中间形态认定已接入。
- 本文聚焦章节、同步改写、富文档和封面，并涉及其分析/向量派生边界；向导、实体、大纲、Agent、索引内部等仍须独立逐入口审核。
