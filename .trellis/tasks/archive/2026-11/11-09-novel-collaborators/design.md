# 作品与系列设定协作：技术设计

## 1. 设计边界

- 保留 `project_service.get_owned_project`、`chapter_service.get_owned_chapter/get_project_chapter` 的严格作者语义；不能将名字和原调用方不变的 helper 悄悄扩权。原管理员人工内容路径 `get_manageable_project` 的 RBAC、目标账号保护、写屏障及 owner-only 限制继续有效。
- 新增独立的协作访问解析与具体动作校验。需要协作的调用点显式迁移，不能把 `lock=True`、HTTP 方法或路径中是否有 `project_id` 当成能力定义。
- 访问解析始终使用真实 actor，不将 `project.user_id` 伪装为操作者。作品、系列、任务、私人配置和私人会话各自校验归属。
- 此设计覆盖项目内人工内容、TXT/Markdown/EPUB、AI、任务、系列和书架；开放 API 密钥、作者私有资源、项目备份及管理员旁路不因共享自动扩权。
- 本任务只规划实现；是否支持实时共写、个人阅读进度、共享向量多模型和外部账单结算均不在 V1。

## 2. 身份、能力和错误

### 2.1 访问模型

新增 `app/services/project_access.py`，具体函数名可随实现调整，但能力和调用约束必须明确：

```python
ProjectRole = Literal["owner", "editor", "viewer", "admin"]
ProjectCapability = Literal[
    "content.read", "content.edit", "export", "ai.use",
    "collaborators.manage", "project.recycle", "project.backup", "reading_progress.write",
]

async def resolve_access(db, actor, project_id) -> ProjectAccess: ...
async def require_project(db, actor, project_id, capability, *, for_update=False,
                          grant_identity=None) -> Project: ...
```

`ProjectAccess` 表示当前 actor、项目及各能力来源；它不是可跨事务使用的永久授权凭据。正常项目需通过现有生命周期检查。项目不存在、未接受、已撤销或无读取来源均为 404；作品可读但缺普通内容写入或 AI 能力为 403 `project_readonly` 或 `permission_denied`。项目备份、阅读进度等既有严格作者专属接口对非作者仍可沿用 404；管理员人工旁路的错误保持原规则。禁用/会话失效仍为 401；删除中沿用现有 `project_deleting` 409，回收站为 404，不伪称两者原有行为相同。

| 能力 | 作者 | accepted viewer | accepted editor | 管理员人工旁路 |
| --- | --- | --- | --- | --- |
| 项目资料读取 | ✓ | ✓ | ✓ | 依现有 `projects.read` |
| 人工内容编辑 | ✓ | — | ✓ | 依现有 `projects.edit` 和目标保护 |
| TXT/Markdown/EPUB | ✓ | ✓ | ✓ | 不因管理员身份放行 |
| AI、智能体和项目任务 | ✓ | — | ✓ | 不因管理员身份放行 |
| 协作者管理、项目备份、阅读进度 | ✓ | — | — | 不因管理员身份放行 |
| 作品回收 | ✓ | — | — | 原 `projects.recycle` 路径 |

管理员可以同时是协作者：各能力按真实来源求并集。例：viewer 兼 `projects.edit` 管理员可以按管理员规则人工编辑，但不能因管理员写权限触发 AI；editor 兼管理员可使用自己作为 editor 的 AI 能力。RBAC 降权不能撤掉仍有效的邀请，撤销邀请也不能抹去仍有效的管理员人工能力；每个路径仍执行其来源专有校验。

对外 `ProjectOut` 新增服务端生成的 `access`，至少包含 `role`、`is_collaborator`、`can_read`、`can_edit_content`、`can_export`、`can_use_ai`、`can_manage_collaborators`、`can_recycle`、`can_backup`、`can_write_reading_progress`。`role` 仅表示主要展示身份：owner 优先，其次有效协作角色，最后 admin；**能力以布尔值为准**，不由单一 role 推导。所有 HTTP 投影必须传真实 actor（包括创建、列表、更新和封面响应）；内部无 actor 的投影不得推断为 owner。列表联接协作关系批量投影，不能对每本书执行一次授权查询。

`access` 是显示快照，不能替代后端写前重验。无需把权限长期缓存在 `db.info`；若为同一只读事务做缓存，不能跨 commit/rollback、不能用于锁后校验或外部发送，任何权限数据异常均失败关闭。

### 2.2 调用点与动作清单

实施时以实际路由和 service 调用链建立完整能力清单并逐项迁移，至少覆盖：

- 人工读写：`projects`、章节/场景/富文档、版本、`story.*`、`series_read`、`planning`、`outline_pro`、各类设定、伏笔、组织、关系、职业、卡片、知识库、记忆、笔记、提及、分析及手工封面。
- 不带项目 ID 的入口：先按章节、场景、分析、回写、会话、提案或任务 ID 找到父作品，再校验资源属于该作品和具体动作；包括 `/chapters/{chapter_id}/rewrite`、generate/polish、任务输出/订阅/审核、设定条目和大纲快照。
- AI：所有同步预览和后台生成均显式要求 `ai.use`，包括章节/场景改写、封面、向导、大纲、实体、批量、分析、知识库向量和智能体工具；不能因该 POST 只预览或无持久化而让 viewer 使用。
- 导出走明确的 `export`；`project_transfer.exporter.prepare_export/estimate`、项目回收、协作管理、`reader_service.save_progress` 继续严格作者或既有专用管理员规则。编辑者阅读时不调用阅读进度写入，服务端也需拒绝其直接调用。
- 保留 `style_service`、prompt、Skill、MCP、`api_key_service` 和 `open_api.catalog` 的用户级隔离；项目配置中现有跨用户禁止修改的私人风格绑定与 AI 设置继续禁止。协作者的提示词/风格选择必须来自自己的可见资源，不读取作者私人库。
- 项目/章节默认风格可能指向作者私人风格：面向协作者的 `ProjectOut.default_style_id/default_style` 和章节 `style_id` 对不可见的私人风格返回 null，不暴露名称、描述或 ID；全局预设仍可用。`project_info/default_description`、向导及实体提示词也必须按真实 actor 判断，不能把作者私人风格描述发到协作者的模型；无法继承时不应用该私人风格并给出明确提示。协作者提交普通项目/章节编辑时必须省略这些私人风格绑定字段，服务端对显式提交（包括 null）继续拒绝，不能把脱敏后的 null 写回覆盖原绑定。作者与原管理员响应保持既有语义。
- 聊天和提及：`AgentConversation.user_id` 始终为真实 actor；viewer 不挂载智能体。协作者的提及可包含**自己的**聊天，过滤他人会话；管理员人工旁路维持原行为。`series_read` 和 `ProjectOut.series` 不向仅有单书权限的用户泄漏系列身份。

路由级依赖可作补充，但不是主安全屏障。测试通过 OpenAPI 与递归展开的实际路由建立清单，断言待测端点数量及已知代表路由，避免当前 FastAPI 的 `_IncludedRouter` 造成空扫描；每个能力还需真实 HTTP / service 反例，而不是仅检查依赖是否挂载。

## 3. 数据与迁移

### 3.1 协作关系

迁移 `0044_novel_collaborators`（父 `0043`）新增：

| 表 | 主要字段与约束 |
| --- | --- |
| `project_collaborators` | `id`、`project_id`、`user_id`、`role`（viewer/editor）、`status`（pending/accepted/declined/revoked）、`allow_owner_ai`、`invitation_id`（UUID，唯一）、`revision`、`invited_by_user_id`、`responded_at`、时间戳；`UNIQUE(project_id,user_id)`，项目和用户删除时级联 |
| `series_collaborators` | 同上，以 `series_id` 替代 `project_id`，角色固定为 editor，不含 `allow_owner_ai`；`UNIQUE(series_id,user_id)` |
| `collaboration_audit_events` | 独立追加式事件：operation UUID、scope、资源 ID、邀请轮次、actor/target 的 ID 与安全名称快照、action、result、错误码、前后角色/模型授权摘要、时间；资源与用户快照不设级联外键 |

为 role/status、正 revision、不同 actor/target 等建立适当检查；不能邀请实际作者由服务层在锁内校验。邀请人删除后快照允许为空，不能以可空字段的数据库 CHECK 代替服务层自邀检查。按 `(user_id,status)` 建待处理索引。

`revoked` 是保留身份的无权限状态，不在可访问查询中；`declined/revoked → pending` 重新邀请时更新新的随机 `invitation_id` 并递增 revision。接受/拒绝、角色及授权变化、撤销均递增 revision；权限或授权变化会使该用户先前创建的进行中协作 AI 工作失效，需要重新发起。作者和协作者的真实 user ID 不能改写。

### 3.2 生成记录归属和备份

- `generation_logs` 新增 nullable `actor_user_id`（用户外键 `ON DELETE SET NULL`）。迁移前的项目生成记录属于原有严格作者路径，按当时 `projects.user_id` 回填；新建记录必须由调用方显式传真实 actor。用户删除后为 null，不能把 null 当成当前作者的记录。
- 章节生成、局部改写、富文档采纳、Agent、拆书等所有现有 `write_log/save_log` 路径携带 actor；同步操作不能依靠 `task_id` 推断。列表、详情、Agent 对话日志按 `actor_user_id` 过滤，且要求当前账号是作者或 accepted 作品协作者；仅有管理员人工读取权不能旁路。已有 token 用量有值才记录，未知保持 null。
- `ChapterVersion.generation_log_id` 对无权读取该记录的用户投影为 null，但版本正文仍按作品读取权限展示；非本人日志配置 ID 不得通过版本或任务响应泄漏。
- 单书备份仍仅作者能发起；可选生成记录只导出作者本人记录。若章节版本引用的生成日志因隐私过滤未导出，备份中的 `generation_log_id` 必须置 null，导入仍可恢复版本。注册表将 `actor_user_id` 声明为导入时填当前导入用户，不导出原用户身份；旧备份的日志由导入者拥有。验证旧版格式、含/不含日志与引用映射。
- 将新项目相关授权表登记到 `project_transfer.registry.NOT_EXPORTED`；只登记确实被项目表反射发现的表，避免违反现有注册表测试。整库备份按现有权限保留所有表。

### 3.3 降级与部署

- 任一协作表或协作审计表有数据时拒绝有损降级；若存在触发者不是作品作者或触发者为 null 的生成日志，也拒绝删除归属字段。报错说明原因，不静默清表。
- 迁移覆盖空库、已有库和 SQLite/PostgreSQL；旧作者日志回填、用户删除、项目彻删、外键、约束与降级均有测试。
- 旧服务会按作品返回全部生成日志，因此数据库保留新列并不能让旧服务安全读取多用户日志。回滚时须先停用访问，确认旧服务已具备日志隔离兼容补丁；否则不能部署旧服务。禁止通过只撤前端入口或直接清表声称已安全回滚。
- 不允许旧版和新版写入进程混跑：先排空旧任务/流，再升级数据库与服务，最后开启入口。

## 4. 邀请接口与审计

均在 `/api/v1` 下，沿用项目的 `{detail, code?}` 错误格式；数字 ID / UUID / 输入长度严格校验。

| 方法与路径 | 权限 | 请求 / 结果 |
| --- | --- | --- |
| `GET /projects/{project_id}/collaborators` | 作者 | 成员及 pending/declined/revoked 状态，含 role、allow_owner_ai、revision、安全用户信息 |
| `POST /projects/{project_id}/collaborators` | 作者 | `{username, role, allow_owner_ai}`；创建/重新邀请，返回当前行及新 invitation_id/revision |
| `PATCH /projects/{project_id}/collaborators/{user_id}` | 作者 | `{role?, allow_owner_ai?, expected_revision}`；至少一个变更字段 |
| `DELETE /projects/{project_id}/collaborators/{user_id}?expected_revision=N` | 作者 | CAS 撤销，204；旧版本 409 |
| `GET /series/{sid}/collaborators` / `POST` | 系列作者 | POST `{username}`；角色固定 editor，不带模型授权 |
| `DELETE /series/{sid}/collaborators/{user_id}?expected_revision=N` | 系列作者 | CAS 撤销，204 |
| `GET /collaboration/invitations` | 登录用户 | 返回本人当前 pending 的作品/系列邀请，含 scope、invitation_id、revision、角色、作品模型授权和必要标题 |
| `POST /collaboration/invitations/{scope}/{invitation_id}/accept` | 被邀请人 | `{expected_revision}`，原子接受并返回新状态/revision |
| `POST /collaboration/invitations/{scope}/{invitation_id}/decline` | 被邀请人 | `{expected_revision}`，原子拒绝并返回新状态/revision |

`scope` 仅 `project|series`。非本人或不存在的邀请为 404；邀请状态/版本变更为 409 `stale_invitation`；pending/accepted 重复邀请为 409 `already_collaborator`；自邀为 400；用户名不存在或禁用统一为 400 `invitee_unavailable`。修改 pending 的角色/模型授权会递增 revision，被邀请人必须重新查看后操作。

这些写接口以预期版本保护，不声明为可盲目重试。超时/响应丢失后，前端刷新实际状态再决定是否提示重试；不能用旧请求自动接受新邀请。DELETE 成功没有“最新 revision”响应的承诺。

审计使用独立事件表和明确 writer，**不直接复用只记录管理账号的 `AuditOperation` 开关**。成功事件与状态同事务提交。失败尝试回滚业务后，用已保存的安全快照与 operation UUID 独立写失败记录；提交结果未知时先按 operation UUID 核实，不能补造相反结果。审计写入失败不得伪装业务成功；日志不含正文、提示词、密钥或原始异常。

## 5. 并发、撤权和锁顺序

### 5.1 项目协作写入

项目内容写、任务受理/结果落库、角色变更和撤销遵守统一顺序，锁到业务提交/回滚：

1. 认证授权 gate 与相关 User 锁：沿现有 `authorization_service`，多个 User 按 ID 升序；管理员动作保留原 RBAC/目标保护。
2. 涉及 AI 发送策略或撤权时，按既有机制取得 `ai_dispatch.lock_visibility_write`；PostgreSQL 的 advisory 锁不是项目行锁，不能二选一。
3. `project_lifecycle.lock_active/lock_record` 取得项目父行锁；锁后重新读取生命周期、成员 status、role、invitation_id、revision 和所需能力。不能采用锁前或跨事务缓存的结果。
4. 同一事务更新内容或协作行及审计，然后提交。需要可见性戳/内容版本的路径仍按原契约 CAS；子资源必须再次限定项目。

普通人工写即使不需要 AI advisory 锁，也必须拿相同的项目父行锁并在锁后重验；撤权同时持有发送写门禁和父行锁，因此能与普通写及受控发送分别串行。若撤权先提交，后续写锁内读取不到旧授权；若业务写先提交，其结果有效，不能事后回滚。SQLite 遇到旧快照/锁冲突时回滚并安全返回可重试冲突，不得沿旧快照继续；PostgreSQL 按实际数据库事务隔离验证。

邀请接受/拒绝、修改与撤销都采用上述相关父行锁及行内版本条件；系列邀请使用系列行屏障。唯一约束处理并发首次创建，冲突转业务 409。锁内对比 `invitation_id` 和 `expected_revision`，旧轮次不能响应新邀请。

### 5.2 系列与跨书

系列写遵循现有系列顺序：认证 gate/User → 系列行 → 按项目 ID 排序获取发送写门禁 → 同序获取项目父行锁 → 锁内重验系列能力、项目生命周期及本操作所需的逐书权限/依赖 → 提交。项目单独写与撤权不反向获取系列锁。系列撤权取得同一系列行锁并锁后复查；跨书传播的系统内部投影权限只用于 §7 的共享字段，不授予 actor 单书内容访问。

### 5.3 外部请求与流

- 在发送 lease 中按现有认证用户锁 → 共享发送门禁顺序，**取得门禁后**重验作品 AI 能力、任务绑定的邀请轮次/权限版本、模型来源授权及原 VisibilityStamp/SceneContextStamp；发送后按既有规则在响应头释放 lease，不持项目写事务等待模型流。
- 协作者任务在创建时记录协作 `invitation_id` 与 revision；作者任务不要求协作身份，存量作者任务保持兼容。任务准备、每次模型/向量真实出站、工具执行、每批数据库写和最终采纳均重新校验。任何收紧或旧轮次不为旧任务续签；恢复授权或重新邀请不自动恢复旧任务。后台无 HTTP 会话时仍核验账号启用、真实 actor 和授权状态。
- 成功撤权/降级后通知本进程中的相关执行体和订阅者；队列领取、重启恢复、事件出队和心跳仍以数据库最新状态为权威，不能只依赖内存通知。授权存储不可用时停止而非放行。项目任务详情/输出要求任务本人且当前为作品作者或 accepted 协作者，不能仅凭管理员读取权放行；活动 AI 流还需当前 `ai.use` 及原授权轮次有效。
- HTTP 流已发送响应头后不能改成 404；向客户端发不含私有资料的权限失效事件并关闭。已发送的字节/已到达供应商的请求不可撤销，取消只阻止后续受控发送、输出及业务写入。
- 失败关闭允许受限的系统清理：按任务 UUID/轮次 CAS 恢复 `generating` 等占用状态、记录任务终态/必要安全审计；不写正文、设定或旧模型产出，不以用户权限失效为由遗留永久占用。已经提交的章节与已发生的外部工具效果不回滚。

## 6. AI、任务和知识库

### 6.1 配置解析

为有作品上下文的 AI 入口引入统一解析结果，例如 `ResolvedProjectModel(source, config_id, config_owner_id, grant_identity)`；它只保存在服务端任务/内存，不返回密钥或作者配置 ID。

1. 客户端显式传入配置 ID 时只允许本人同类型配置；ID 无效、类型错误或配置不可用直接报原有安全错误。
2. 未指定时使用本人该类型默认配置；沿用现有解析器在无默认配置时选择最新配置的规则。本人有配置但解密、端点或提供方校验失败时直接报错，不静默回退。
3. 本人没有该类型配置，且当前 accepted editor 的 `allow_owner_ai=true` 时，才选择作者同类型默认配置；否则返回明确配置缺失提示。作者自身沿用原解析。
4. 入队时保存服务端解析的来源和配置身份，不把回退得到的作者 ID 当作客户端显式选择重新走本人校验。运行、重试、续跑和每次发送前重验 actor、角色、同一邀请身份、授权开关、配置归属与有效性；模型删除或授权变化则失败，不切换到另一人的密钥。

对 `generation_service`、`rewrite_service`、`rich_document_section_ai`、`analysis_service`、`wizard_service`、`outline_ai_service`、`entity_ai_service`、`batch_service`、`cover_service`、`agent` 等项目上下文调用点逐一覆盖；账号级或无项目上下文调用仍保留原解析。服务端投影的 `ai_config_id` 仅在配置属于当前 actor 时可见；作者回退只返回 `model_source=owner` 等安全标识，不返回密钥或配置对象。

### 6.2 共享向量索引

当前 `retrieval_chunks` 每个项目检索单元只有一份向量，换模型会覆盖同一列；V1 不引入多模型索引。

- 项目作者仍按原有默认向量模型建索引和检索。协作者在作者授权关闭时上传/修改资料只维护关键词索引；搜索和 AI 上下文只用关键词，不自动向作者模型发请求，也不使用自己的不同模型改写共享向量。
- 作者授权开启后，协作者触发共享向量检索/建索引可使用**作者的项目向量模型**，即使协作者另有个人向量配置。这是项目共享索引的显式例外；任务归协作者，外部费用由作者密钥账户承担。作者无向量配置时仍采用关键词或返回现有重建缺失提示。
- 编辑者手动重建在无作者授权时返回 403 `owner_embedding_not_allowed`；上传仍成功并显示关键词模式。新增或修改条目只清理受影响的过期向量，不以另一个模型批量覆盖现有向量。
- 将 actor 与协作授权身份传入 `IndexJob`；`build_index_job` 不再用 `project.user_id == task.user_id` 判断编辑者，但每批读取/发送/持锁写均执行 §5。`enqueue_memory`、自动分析、批量和 Agent 派生索引需传真实触发者与授权，不能悄悄创建作者名下的任务。
- 所有检索入口，包括 `kb_service.knowledge_for_project`、章节生成、向导、实体生成和直接检索，都遵守本段；不能只改上传入口。UI 明示关键词降级、费用来源和重建不可用原因。

**E6 裁决（2026-11-13，用户全部采纳 `research/e6-kb-inventory.md` 推荐）**：
1. 向量空间键：作者与授权协作者统一写作者现用的旧键 `provider:model:dim`；实体、大纲等读取端改为旧键，调整依赖 `shared:v2` 的红灯测试。不迁移到 `shared:v2`，避免作者现有索引失配。
2. 已授权但作者无向量配置：编辑者上传同步走关键词，界面提示作者未配置向量模型。
3. 授权编辑者可全书重建，前端加费用确认（费用由作者承担）。
4. 授权编辑者修改记忆后只向量化本次写入的条目，不补全书。
5. 协作者订阅 `kb.index` 实时进度保持 404；前端改为轮询状态。
6. 作者自身索引也迁到受控发送/每批重验路径；向量服务普通故障仍降级为关键词。
7. 仅关键词资料导致整书检索降级：接受，向作者提示「N 条资料待建向量」。
8. `index-status` 对编辑者开放精简版，不返回作者配置名/ID。
9. 大纲、实体、向导在 E6 内切换到作者向量（授权时）。
10. 无授权编辑者对失败文档的重新索引（含仅关键词重分块）按设计返回 403 `owner_embedding_not_allowed`。
11. 编辑者索引任务遇向量服务普通故障降级为仅关键词；授权、策略类错误直接失败关闭。
12. （2026-11-13 追加裁决）单文档索引（文档模式）恢复替换本文档内旧模型向量为作者当前模型向量，作者与授权编辑者均适用，范围仅限本次索引的文档；不批量改写其他文档或其他条目。
13. （2026-11-13 E6-d 遗留裁决）作者自身的向导任务也在受理时冻结共享向量选择并走发送守卫逐次重验，与大纲、实体一致；在 E6 收口切片补上。
14. （同上）`shared:v2` 相关原语（`shared_embedding_model_key`、`store_embedding_snapshots` 及其原语测试）已无生产读写方，在 E6 收口清理切片删除。
15. （同上）向导重试/续写暂保持「按当前授权重新冻结共享向量选择」，不与大纲「沿用原冻结再重验」对齐；当前授权不会多给权限。
16. （2026-11-13 E6-d 复核裁决）作者向量配置存在但本地不可用（解密失败、端点无效等）时，`index-status` 新增 `keyword_reason=owner_embedding_unavailable` 并报 `retrieval_mode=keyword`；在 E6-e 与前端类型一并落地，编辑者视图仍不暴露配置名/ID。
17. （同上）大纲、实体、向导任务在构造作者向量 provider 时遇普通故障，与同步检索一致：锁内复核原邀请轮次、身份、作者模型授权与冻结选择均未变则降级为关键词继续；任一变化则失败关闭。在 E6 收口切片与第 13、14 条一起做。
18. （2026-11-13 E6-e 裁决）作者本人 `keyword_reason=owner_embedding_unavailable` 时，作者横幅显示「向量配置不可用，请到模型配置检查」，重建/重新索引按钮置灰并说明原因（允许改动作者界面）。在 E6 收口切片做。
19. （同上）后端 `TaskOut` 增加只读字段标识「记忆条目绑定、不可直接重试」的任务，前端改按该字段隐藏重试，替代按任务标题识别；跨作品同样生效。在 E6 收口切片做。
20. （2026-11-14 E6-f 复核裁决，维持现状）①作者向导受理时本人向量配置本地不可用：照常受理、仅关键词（大纲/实体同情形受理 400，不统一）；②受理后作者配置才变为不可用：任务失败关闭，不重选配置；③作者配置不可用时文档「重建索引」置灰，不提供临时仅关键词重分块，修好配置后恢复。

**E4-i 协作者 Agent 裁决（2026-11-14，用户全部采纳 `research/e4i-agent-inventory.md` §11 推荐）**：
1. 未开启作者模型授权的编辑者可用本人模型开 Agent；授权开启且本人无配置时按 R4 回退作者模型。
2. 编辑者 V1 不开放 MCP 工具（外部效果不可撤、unknown 屏障会阻挡作者删除作品）。
3. 编辑者可开启自动执行，规则与作者相同。
4. viewer 可只读查看本人会话，不能发送、确认或撤销。
5. 编辑者可启动现有 `start_task` 白名单 10 类任务，不新增。
6. 管理员兼编辑者的 Agent 写入按现有规则记管理审计，与人工路由一致；撤销接审计协调器，不新增审计动作。
7. 撤权后重邀：旧会话恢复只读可见，旧轮次不能续跑。
8. 撤销需要 AI 使用权与原邀请轮次；降为 viewer 后不能撤销。
9. 读工具不单独加锁重验，依赖结果写入前的执行体复核。
10. 协作者订阅 `agent.turn` 实时进度 V1 保持 404，前端改轮询。
11. 撤权后的 paused 轮次纳入受限收尾：任务失败、待确认提案作废、外部调用 unknown 屏障保留。
12. 检索工具与 E6 一致：已授权且作者配置可用用作者向量（受理冻结、发送守卫重验），否则仅关键词。
13. 编辑者会话自身摘要允许继续使用，加来源标记与作者摘要区分。
硬约束：Agent 写入必须同时要求 AI 使用权与原邀请轮次，管理员 `projects.edit` 来源不得并入；确认路径不得跳过工具内复核。切片顺序 a→g 见 research §10。
E4i-a 复核追加裁决（2026-11-14，用户按建议处理）：
14. 协作者撤销被授权拒绝时返回 409 `permission_denied`（与确认路径一致），不再返回 404；会话不属于本人等不存在语义仍为 404。
15. 拒绝提案与整轮拒绝视同写操作：viewer 与无 AI 使用权/原邀请轮次者不能执行；因这两条路径不经过 `execute_change`，E4i-d 入口须自行做 AI 使用权 + 原邀请轮次检查（锁内）。runner 自动 MCP 绕过 `lock_agent_write`，按裁决 2 对编辑者关闭。
16. 运行器加锁顺序为 E4i-b 硬门槛（开放前必须修复）：编辑者运行前先按 ID 升序锁作者（及相关）User 行，再取发送写入关卡，与任务引擎领取顺序一致（全局门 → User 锁 → 发送写入门 → 作品父行 → Task → 配置）；补 PostgreSQL 两连接并发死锁测试（Gate/Event 编排，不用 sleep）。
E4i-b 复核追加裁决（2026-11-14，用户按建议处理）：
17. Agent 写入与撤销涉及记忆时，E4i-c/E4i-d 接 `enqueue_collaborator_memory` 同款口径（编辑者本人 + 原邀请轮次、仅单条记忆）补建向量；E4i-b 维持协作者撤销跳过作者全书记忆向量任务。
18. 引擎 `_event_task` 锁序（outline/entity/wizard 等协作 handler 的事件提交先取发送写门、后锁作者 User，PG 下作者偶发 2 秒 `ai_visibility_busy`）单独立为小切片 E4i-b2，在 E4i-d 开放入口前完成：`_event_task` 传 `project_owner_id` 给 `require_write_execution`，补双连接回归。
19. 迁移 0053 降级在存在非空 `summary_source` 时拒绝（与 0052/0029 一致），保留。
E4i-c 复核追加裁决（2026-11-14，用户“都维持”）：
20. 协作轮次冻结为作者向量后整轮依赖作者模型授权（与大纲/实体一致）；作者修改成员任何设置都会令进行中轮次终止，故仅额外影响“作者改坏向量配置时终止用作者向量的轮次”（符合 E6 裁决 20②），维持。
21. 作者向量配置本地不可用时，Agent 协作轮次照常受理、运行时仅关键词（与向导及作者本人一致，E6 裁决 20①），不改为 400。
E4i-d 复核追加裁决（2026-11-14，用户按建议处理）：
22. viewer 与旧轮次会话主人不能删除本人会话（409，与只读一致）；如需清理另开“仅删除”例外，须保留外部未知屏障且禁止进行中删除。
23. 会话可写性由后端计算 `writable` 布尔字段（不输出邀请 UUID），放在 E4i-f 的后端部分，同步 `get_conversation` 投影快照比对；409 保留兜底。
24. 通用 `/tasks` 重试的 403/400/409 不统一，前端统一显示“不可重试，请在当前会话重新发起”。
E4i-e 复核追加裁决（2026-11-15，用户按建议处理）：
25. 提案作废不追加系统消息；前端按提案 `status/error` 与任务失败文案展示。
26. 已结束轮次残留的 proposed 提案不事后补作废；前端按“轮次已结束”统一置灰。
27. 作者改向量配置致续跑失败后同轮重试持续失败，维持；E4i-f 对“模型配置已失效，请重新选择”类失败隐藏重试，提示“作者向量配置已变化，请在会话中重新发起”。
E4i-f 复核追加裁决（2026-11-15，用户确认）：
28. “模型配置已失效，请重新选择”除作者向量配置变化外，也会在编辑者本人或作者的文本模型配置被改 / 删时出现；协作者侧隐藏重试不变，提示改宽为“模型配置已变化（可能是本人或作者的配置），请在会话中重新发起”（常量 `AGENT_MODEL_CHANGED_HINT`，取代裁决 27 的措辞）。偶发失败测试已修，多轮测试须等待上一轮结束；重试提示仅协作者可见、置灰 / 隐藏的区分维持。
E4i-g 复核追加裁决（2026-11-15，用户按建议处理）：
29. 授权失效早于工具审计作用域时不记审计：§9.2 第 3 项验收改为“授权失效时不产生成功审计、也不产生重复失败审计；工具先持锁成功时恰好 1 条 success”。
30. 并发改动提案可见字段（如 `updated_at`）时 Agent 确认在投影核验处返回 404 而非 409，维持（投影核验是隐私边界，失败关闭、零副作用、可重试）；Gate E 在 backend/agent.md 错误矩阵写明，防止被“修”成 409。
31. 作者开启模型授权且作品共享检索使用作者向量时，不论文本模型来自编辑者本人还是作者，前端均提示“检索向量费用由作者承担”；Gate E 必做，仅前端文案。
遗留（不在本任务）：`chapter_service`、`project_access`、`setting_ai_visibility`、`series_service` 的 SQLite `busy_timeout` 恢复同款写法，后续统一。
后续切片带入：E4i-e 补 paused→queued 恢复时冻结向量选择锁内重验测试；E4i-f 在作者修改成员授权处提示“会中止其进行中的 AI 任务”。

### 6.3 任务与私人数据

`Task.user_id` 与并发准入使用真实触发者；`analysis_service.enqueue_auto/enqueue_auto_in_transaction`、`kb_index_service.enqueue_memory`、batch、富文档采纳和 Agent 等衍生任务透传 actor 与授权轮次。作者自己触发的旧行为不变；无法证明触发者或已撤权时不创建衍生任务。

任务列表、详情、输出、重试、继续、取消、富文档提案与 SSE 同时核验本人及当前作者/accepted 协作身份；仅有管理员人工项目权限不授予个人任务权，一个成员不能控制作者或其他成员的任务。编辑者被降为 viewer 后不再运行或订阅活动 AI；仍为 accepted 协作者时可读取其本人已结束的私人记录，协作授权撤销后即使保留管理员人工读取权也隐藏这些记录。后台按 Task 本人 ID、终态和任务 UUID 裁剪历史并更新统计，不要求重新获得作品权限；不返回隐藏内容、不操作其他人的任务或作品正文。待审核/运行中任务先走系统失败收尾再参与裁剪。

**旧无占用知识库文档任务（E4-e 裁决，2026-11-12）**：0045 之前入队、文档侧没有 `index_task_key/queued_at/claim_key` 的 `kb.index` 文档任务不补造身份、不执行；在可证明无执行体时做一次「无内容写入的系统清理」，与 book_import 失败关闭同一原则（不留永久锁）。证明条件全部在锁内、同一事务成立：Task 仍为 `queued`、`execution_key IS NULL`、无管理员取消意图、`queued_at`/UUID 与读取时一致、请求为本作品 document 模式且 `resource_key == kbdoc:{id}`；文档属于该作品、`status == pending` 且三列占用全为 NULL；不存在该文档的其他未结束 `kb.index` 任务。清理按 Task UUID/queued_at CAS 把任务记为失败（固定文案提示重新索引），并按「仍为 pending 且三列为 NULL」CAS 把文档置为 failed；不写正文/分块/向量、不调用模型或向量、不派生任务。引擎领取、用户取消（成功后记 cancelled 语义按现有取消约定）与作品删除共用同一清理函数；任一条件不成立仍保持原失败关闭（`task_cleanup_unproven`）。旧任务本身的重试继续拒绝，作者通过重新索引走新受理建立带占用的新任务。

**E5 裁决（2026-11-12）**：
- 实时推送：只有协作者订阅走受控管线（每帧发送时锁内复查数据库）；作者订阅保持既有管线与行为不变。
- 编辑者被降为 viewer 或撤权时，其 `paused` 任务（如批量审核点）与运行中任务一样由系统受限收尾为失败：已提交内容保留、占用释放、不写新内容、不派生；不依赖本人操作。
- 收据收尾（章节/分析/批量/向导）的终态帧改为带任务快照的 `done`，附“结果未发布”说明；以已提交的任务行为证明，不依赖当前授权；已失权订阅者由发送时复查拦截。
- 管理员按运维权限取消他人任务保留现状（不读输出、不订阅、有审计）。
- 跨实例撤权发现沿用心跳周期（约 15 秒），不加跨实例唤醒；直接改库关闭作者模型授权而不递增 revision 不作为支持路径。
- 批量“后继来源”（2026-11-13 采纳）：仅 `chapter.batch`，hub 未关闭且无终态时，被同一执行后续步骤取代的帧可由同一 hub 已登记、同一执行、游标不回退、与持锁行逐字一致的最新来源证明，并以该来源 guard 在锁内复核当前授权；不跨执行、不以持锁行补签。其他任务类型不适用。
- 续写设定向导保存后核验（2026-11-13 采用方案 A）：保存后的核验以构建时可见性 epoch 比对续写来源，并要求保存后新戳仍成立，证明中间唯一变化是本任务自己的保存；发送前检查仍与当前 epoch 比对。
- 向导“同执行戳后继”（2026-11-13 采纳）：仅设定类向导（角色/世界观/职业），保存前产出、在本任务收紧可见性的保存提交后才发出的帧，可由本任务保存事务内记录的新可见性戳证明（须同一执行、带本任务完成收据），并按当前授权在锁内复核；他人造成的可见性变化不得被接受。

## 7. 系列设定设计

- `_readable` 对系列作者及 accepted 系列 editor 放行；`_edit` 获取系列行后重验系列成员。系列作者管理邀请、重命名、删除系列与加入/退出书籍。
- 新建、读取、编辑条目允许系列 editor；`patch_entry` 使用内部投影事务，只按既有 `series_policy` 同步共享基础与 AI 策略，保留各书本地事件、引用预检、可见性/规划版本及对回收站成员的既有处理。不得借内部投影权限读取或返回书内私有内容。
- 系列 editor 删除条目时，持系列锁后重新检查全部 `SeriesEntryUse`；存在任意绑定即 409 `series_entry_bound`，不返回不可见书的标题、ID 或阻断详情；零绑定可删除。系列作者删除继续走既有多书锁和依赖预检。不能用内部传播锁直接调用 `_retire_entry/_remove_projections`。
- `promote/use/unuse/move/impact` 同时要求系列操作权限和所有实际受影响书籍的作者或 accepted 作品 editor 权限；仅管理员人工旁路不授予系列能力。`move` 需要覆盖所有将被解绑的书，而非仅目标书；涉及回收站书籍或无权书籍时仅系列作者可完成现有完整操作。锁内重验绑定与依赖，失败整体回滚。
- 系列详情中的 `books`、条目的 `uses[].project_id/ref`、预检的 `affected_books/blockers` 仅投影调用者有作品读取权的书；系列级总数可以展示。给每个条目增加服务端计算的 `has_uses` 布尔值，包含无权查看与回收站书籍，供系列 editor 判断能否删除，不泄露具体书籍；列表批量计算以免 N+1。无法安全展示的跨书冲突使用通用错误，不携带隐藏书名或 ID。原系列作者的完整响应不变。

## 8. 前端

- `ProjectOut.access` 在真实 HTTP 响应中为必填，TypeScript 与服务端一致；`projectAccess.ts` 和 `useProjectAccess.ts` 同时改造，按能力而非 `!managingOther` 推导 `canReadNow/canEditNow/canRecycleNow`。管理员与协作者重叠单独测试，原管理员代管 AI 行为不变。
- `ProjectListPage` 合并自有/共享书架及待处理邀请；展示角色、作者、模型授权，接受/拒绝提交 invitation_id 和 revision。邀请、角色修改、撤销/费用开关有清晰确认；409 保留当前界面并刷新事实，不盲目重放。
- `ProjectLayout` 与独立 Reader 对没有独立管理员写能力的 viewer 只读；viewer 兼管理员的人工编辑按 `can_edit_content` 判断，AI 与任务操作仍单独按能力关闭。订阅、快捷键和阅读进度写入同样按能力控制。编辑者可操作人工内容和自己的 AI，但不显示作者专属操作；页面在 403/404、退出、换用户/项目后清理数据及旧请求。
- 新增成员面板与系列成员面板。系列 editor 的已绑定条目删除禁用并说明需作者处理；即使绕过 UI，服务端仍返回安全冲突。向量索引界面提示关键词模式与实际费用来源。
- 非作者、不同会话或已撤权时不得返回作者阅读进度、私人聊天和生成日志；响应落地前校验 actor、项目和请求代次，避免旧响应恢复已撤销界面。

## 9. 验证和实施门禁

必须覆盖：

1. 角色矩阵：owner、viewer、editor、pending、declined、revoked、非成员、只读/可写管理员及协作者+管理员重叠；验证 401/403/404、生命周期、owner-only 备份/回收/进度、私人风格脱敏和单书导出。
2. 邀请：相同用户并发邀请、重复请求、不同轮次延迟响应、pending 改角色/授权后旧响应、自邀、禁用/不存在同响应、接受/撤销竞争、审计成功/失败/未知提交。
3. PostgreSQL 与 SQLite：写锁先后两种顺序、角色降级/撤销竞争、系列行及多个项目稳定顺序、模型发送 lease 与撤权、锁超时和回滚；PostgreSQL 必须真实运行，不得以 SQLite 代替。
4. 后台：排队/运行/待审核、章节/场景写入、Agent 提案、自动分析、批量、索引每批、模型发送、活动 SSE 与任务输出在撤权或降级后关闭；系统清理不破坏其他轮次。
5. AI：本人配置优先、无配置可授权回退、无效本人配置不回退、智能体内部 owner 配置 ID、撤销模型授权、向量关闭零作者调用、开启后费用/actor、不同模型不覆盖共享索引。
6. 隐私与迁移：日志 actor、同步改写无 Task、任务/用户删除后归属、跨成员/管理员隔离、版本日志 ID 脱敏、备份过滤与引用重映射、旧备份导入、两表任一有数据降级拒绝与审计保留。
7. 系列：独立系列/作品权限、未绑定删除、隐藏或回收站绑定的 `has_uses` 与安全拒绝、传播、隐藏书籍投影、双重权限、回收站成员和依赖冲突原子回滚。
8. 前端：能力、管理员重叠、邀请版本、权限变化、旧响应、流断开、只读快捷键/自动请求、阅读进度和关键词降级；实际浏览器检查作者、viewer、editor 三种路径。

后端 `cd backend && uv run ruff check . && uv run pytest`；并使用独立的 `AINOVEL_COLLAB_POSTGRES_TEST_URL` 运行专用 PostgreSQL 用例。前端 `cd frontend && npm run lint && npm run test && npm run build`。运行环境不可用时必须明确记录未验证，不可把 skip 当成并发门禁通过。
