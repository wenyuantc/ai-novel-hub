# Research: 后端 RBAC、实时撤权与兼容边界

- Query: 审计全部后端 `is_admin` / `AdminUser` 触点，确定操作级授权、审计、目标账号保护、并发屏障、角色存储和兼容迁移的最小实现边界。
- Scope: internal；仅阅读代码与规范，没有实施，也未运行产品测试或真实 PostgreSQL 验证。
- Date: 2026-11-01
- Planning target: `.trellis/tasks/10-30-admin-rbac-quotas`。当前任务仍处于规划，本文不构成实施批准。

## Findings

### 1. 已确认产品约束与当前基线

本文依据任务 PRD 及主会话补充的最新确认：四类固定角色与自定义角色并存；多角色取并集；只有超管可管理角色和授权；其他现有管理动作可显式委派；非超管不能对任何管理账号禁用、管理员重置密码、删除或修改配额；内置角色不可改删、可复制；在用自定义角色不可删除；旧管理员转为超管；管理权限变化不强制登出，但后续请求与实际写前必须按最新权限检查；自身与最后一名启用超管保护保留。

认证当前每请求读 User，校验启用状态与 `token_version`，JWT 不存角色：`backend/app/core/deps.py:77-96`。因此无需新增认证库或把权限放进 JWT；继续沿用现有 cookie / bearer 提取、401 清会话与密码变更版本失效机制。角色变化本身不递增 `token_version`，否则违背已确认的“不强制登出”。

`User.is_admin` 是真实存储列而非计算属性（`backend/app/models/entities.py:141-152`）；当前管理员创建、更新和 bootstrap 都直接写它（`auth_service.py:29-49,140-166`、`admin_service.py:149-178`）。登录最近时间的条件 UPDATE RETURNING 快照语义必须保持（`auth_service.py:79-112`），不能为了补充角色而在 commit 后刷新出更新的 `token_version` 再签 cookie。

### 2. 全部布尔权限触点与必要替换

| 所有者 | 代码锚点 | 当前行为 / RBAC 必要改动 |
| --- | --- | --- |
| `core/deps.py` | `:90-96` | 单一 `AdminUser` 全量放行。保留超管专用依赖用于角色/授权；其余管理路由改成具体 permission 依赖，不能让“有任一管理权限”放行全部 `/admin/*`。 |
| `api/v1/admin.py` | `:22-70` | 作品目录、用户列表/详情/创建/状态/密码/删号共用 AdminUser。逐动作授权；含角色字段的旧 create/PATCH 需额外超管检查。 |
| `api/v1/admin_dashboard.py` | `:20-32` | 首页三种 GET 使用独立首页读取权限。 |
| `api/v1/admin_tasks.py` | `:19-31` | 元数据读取与取消拆分，不能借取消权开放 owner 任务 output / retry / resume。 |
| `api/v1/admin_audit.py` | `:20-46` | 日志读取与清理拆分，清理预览按清理权限，不因预览 UI 顺带开放全部日志权限。 |
| `api/v1/admin_system_settings.py` | `:9-19` | 设置读取与修改拆分；独立 manager 会话也必须重新授权。 |
| `api/v1/admin_recycle_bin.py` | `:12-35` | 回收站读取、恢复、物理彻删分别授权。 |
| `api/v1/workshop.py` | `:82-112` | 投稿读取、通过、拒绝、官方发布、元数据编辑、物理删除分别受控。普通 CurrentUser 工坊接口仍存在管理分支，不能只改 `/admin` 路由。 |
| `services/project_service.py` | `:37-52` | 他人人工内容仅查 `User.is_admin/is_active`；`lock=True` 只锁 Project，不能阻止与撤权竞争。统一 helper 必须区分读取/人工维护/软删除动作，并接入写前权限屏障。 |
| `services/recycle_bin_service.py` | `:29-35,59-73,184-201,215-250` | `_actor(admin=True)` 与两阶段删除的第二阶段仍查布尔；恢复/purge 会等待锁后重查，须换成对应具体动作。系统到期清理保留明确内部身份，不伪造超管。 |
| `services/admin_task_service.py` | `:182-198` | 已有 actor/owner 按 User ID 排序的真实 no-op UPDATE 屏障，且核对 actor token 版本；替换为 task.cancel 权限时必须保留这些保证。 |
| `services/system_settings_service.py` | `:118-145` | 应用锁之后另开会话读 actor 布尔；若只改 HTTP 依赖，等待期间撤权仍可能生效过晚。manager 必须使用统一 DB 屏障重查，提交/发布/shield 行为不变。 |
| `services/audit_writer.py` | `:82-104` | 仅 `actor.is_admin` 才启用审计。若漏改，自定义角色的管理业务可成功却不记审计；writer 不应成为授权器，业务仍须单独拒绝无权动作。 |
| `services/audit_service.py` | `:164-207` | cleanup 用 `audit.enabled` 当兜底权限，且有自己的提交出口。需显式 audit.cleanup 写前授权，保留 nonce/日期/高水位/清理留痕同事务。 |
| `services/workshop_service.py` | `:43-47,76,105,122,155,206` | 匿名作者显示、隐藏条目、投稿详情可见性使用布尔；仅用户管理或任务管理权不得自动获得这些工坊信息。 |
| 同上 | `:276-300,405-419` | 投稿上限豁免、自动官方发布、hide/unhide 的 owner/admin 分支使用布尔；须按具体工坊能力拆开，不能因有其他管理权限便自动通过自己的投稿。 |
| `services/skill_service.py` | `:55-56,130-150,304-310` | editable 投影、全局创建/导入、修改、references、删除与恢复内置受布尔控制；分动作判定，不放开他人私有 Skill 的 visible_clause。 |
| `schemas/auth.py` / `schemas/admin.py` | `auth.py:33-40`、`admin.py:27-64` | 安全输出加明确超管标记、角色摘要、有效权限与权限快照版本；旧布尔不能继续承担 UI 的全部授权。 |
| `schemas/audit.py` | `:14-60,108-180` | 新角色/授权/配额动作及局部有界摘要要显式加入 Literal 与目标类型；不能直接塞任意角色请求 JSON。 |

`rg -n '\\bis_admin\\b|\\bAdminUser\\b' backend/app` 可作为实施前后漏网审计，但需对每条残留逐个分类，不能机械要求完全零匹配：超管唯一事实、兼容输出、迁移和超管专用检查允许保留。

### 3. 人工内容共享 helper 的实际影响面

`get_manageable_project` 不只供作品资料页调用。以下服务必须随 helper 的 read/write/action 语义变化检查：

- 资料与章节：`project_service.py:242-268`；`chapter_service.py:57-60,71-72,91-93,125-132,171-177,228-230`。
- 章节版本与选角：`chapter_version_service.py:14-15,38-78`；`cast_service.py:142-152`。
- 通用设定与人物/世界观：`story_service.py:46-55,87-88,99-139`；人工路由才显式 `manage=True`（`api/v1/story.py:40-68`）。
- 组织、关系、职业：`organization_service.py:34,115-168,229-269,339`；`relationship_service.py:162-192,435-543,595`；`career_service.py:140,246-305,554-607`。
- 伏笔、大纲与快照：`foreshadow_service.py:275-423`；`outline_service.py:42,72,101`；`outline_snapshot_service.py:119-145,298`。
- 手工封面、知识库人工维护、剧情记忆、阅读目录：`cover_service.py:19-28,51`；`kb_service.py:69-99,159-190`；`memory_service.py:238-354`；`reader_service.py:39`。

只读自定义角色必须真正不能从任一复用写入口写入；不能把“允许进入工作区”当成“允许维护内容”。实现可继续用一个项目人工维护权限覆盖现有子资源维护，但目录必须明确标注其包含子资源新增/修改/删除，不能让 UI 显示成仅修改项目资料。若目录决定细分子资源删除/版本恢复，须在上述调用方显式传动作，而非根据 HTTP 方法或 `lock=True` 猜权限。

严格 owner helper 必须保持不变：AI/智能体/任务输出、个人模型/模板/风格/Skill/MCP/API Key、阅读进度、项目导入导出、知识库上传/重建/向量调用，不因新自定义角色获得额外权限。依据 `.trellis/spec/backend/admin-projects.md` 与现有 `get_owned_project`（`project_service.py:26-34`）。不修改 actor 为 owner、不全局放宽 owner helper。

### 4. 推荐数据与身份方案：保留 is_admin 为唯一超管事实

比“全量成员规范化后额外双写兼容布尔”更小的方案：

1. `users.is_admin` 保持唯一的超管身份事实（语义收敛为超级系统角色，旧管理员自然保留超管；不是“有任一管理权限”）。不新增第二个可独立写入的 super flag，也不再用它对非超管细粒度能力作宽泛判断。
2. 新 `Role` 存四个固定角色的稳定 `system_key` 与安全元数据，以及自定义角色的名称/说明/允许权限列表、revision。内置角色定义从固定目录受控产生，API 不可改删；自定义权限列表只允许目录中的代码、不允许通配符/显式 deny/未知代码。
3. 新 `UserRole` 存普通以外、超管以外的预置角色及自定义角色分配（组合主键；User 删除 CASCADE；Role 删除 RESTRICT）。super 的显示成员关系从 `is_admin` 投影，不另存一条 super UserRole。ordinary 是全账号个人能力基线，没有后台管理权限，不需凭一个 membership 才能访问自己的资源。
4. API 分配包含 `super_admin` 时，统一角色分配服务在受保护事务中转换为 `is_admin` 更新；普通角色只表示基线。分配结果投影回同一角色列表。解析器禁止/忽略历史异常的 super/ordinary 关联行产生额外权限，不能让伪造 role_id 获得超管。
5. 复制超级角色只复制可委派目录中的允许能力，结果仍是普通自定义角色；超管身份和角色/授权管理不属于可复制的权限代码。即使勾选全部可委派权限，也不是超管，仍不能维护管理账号。
6. `is_super_admin` 响应值直接从同一 User 标记派生；安全 `is_management_account` 则为“超管或当前有效管理权限非空”，不能从角色名、角色数量、启用状态或旧 bool 推测。禁用但仍持管理角色的目标也应受目标账号保护。
7. 保持 `CurrentUser -> User`，避免为了 principal 包装让自助改密等 ORM 写失去持久化语义。另加统一不可变的权限快照/查询器与 `require_permission(code)` / 超管专用依赖；按请求批量解析权限，写入边界在屏障后重读，不能用跨请求缓存或 User identity-map 的旧值当真相。

完整规范化所有系统角色为 UserRole 也可实现，但必须另维护 is_admin 兼容投影、迁移回填、bootstrap 和所有低层 fixture 的双写一致性；本任务不需要这层额外权威同步。当前大量测试直接设 `is_admin=True`（如 `backend/tests/conftest.py:97-106`），保留唯一超管事实也减少无关 fixture 迁移。

### 5. 旧 create/PATCH 与权限绕过防线

- `AdminUserCreate.is_admin` 与 `AdminUserUpdate.is_admin` 当前可由调用者提供（`schemas/admin.py:58-64`），旧 UI 仍发送此字段。不能只新增 `/roles` 接口而留下旧字段任意可写。
- 建议兼容保留旧字段，但只允许当前真实超管通过；将其作为超级系统角色分配/撤回的别名，进入同一个角色授权服务与 auth gate，复用自身/最后超管保护、revision、审计，不保留第二套直接 setattr 逻辑。非超管只要显式携带该字段即拒绝，不能因为是 false/no-op 或与 is_active 混合而绕过。
- 若新授权写要求全局 revision 防 User ID 删除复用，旧 is_admin 字段别名也必须执行同等 CAS；无版本的旧 role 字段修改应明确拒绝/迁移到新分配接口，不能另留无 CAS 的提权入口。现有产品确认是旧管理员身份迁移，不等价于旧 HTTP 客户端所有写载荷永久无变更兼容。
- 普通 `user.create` 授权者只能创建普通账号，不得用额外 `roles`、`is_super_admin`、`permissions`、`is_admin` 注入管理身份；新请求 schema 应 `extra='forbid'` 并严格校验布尔/整数，角色操作单独授权。
- 目标账号状态、管理员重置密码、删除与配额修改必须先在共同屏障后读取目标真实有效权限。只锁目标 User 不足以防 Role 定义的并发修改把普通目标变成管理账号。
- 角色赋予超级身份不改变用户密码、启用状态、最近登录时间或个人数据；bootstrap 独立保留“确保启用超管但不覆盖密码”的既有明确行为。

### 6. 写前撤权、锁顺序与 CAS

#### 6.1 为什么只有 User 锁不够

角色分配修改会动 UserRole；角色定义修改会一次改变所有成员权限，却不一定改任何成员 User。若管理写只锁 actor User，Role 编辑仍可并发完成，旧请求随后按旧权限提交。逐成员更新/锁 User 既有扇出成本，也容易和 actor/owner 相互取锁形成死锁。

#### 6.2 推荐最小共同 DB 屏障

新建固定一行的 `AuthorizationState(id=1, revision>=0)`。所有角色创建/编辑/删除/分配、超管 flag/管理账号状态变更，以及非 owner 的管理写，在读取最终权限前对该行作真实 no-op UPDATE，并持锁至该事务提交。复用 `project_lifecycle.lock_record` 的 no_autoflush / no-op UPDATE 模式（`project_lifecycle.py:50-68`），避免 SQLite 的 `FOR UPDATE` 被忽略。

统一顺序：`auth gate -> 需要的 User 行按 id 升序 -> Role/业务资源行`。纯 owner 创作不用 auth gate；管理路径必须在拿 Project/Workshop/Skill 等业务锁之前进入 gate，不能在已锁资源后再反向取 auth gate。角色/授权/账号状态的实际变化递增 state revision；普通管理内容写只取屏障，不必令所有用户权限快照版本变化。

充分性：任何影响权限的事务与使用权限的管理写事务都争用同一行，因此有确定串行顺序。若撤权先提交，随后获锁的写重读后拒绝；若管理写先获锁，撤权等待其提交，这次写属于撤权前已完成行为。目标账号在普通/管理之间变化同样串行。此方案不依赖客户端、进程内角色缓存或逐成员扇出。

代价：所有管理写串行，但只覆盖当前低频管理面，符合既有单进程部署，不宣称横向扩展方案。不要持 gate 等待外部模型、任务 drain 或用户交互；密码 bcrypt 可在取 gate 前计算，但写前仍须复核 actor/target。应用 manager 的锁顺序必须固定，不允许角色服务拿 DB gate 后反向等待 system-settings 应用锁。

#### 6.3 特殊写路径

- 任务取消：`admin_task_service.py:182-198` 原有 User 升序锁、token_version、持久取消意图/受理审计事务不变，在它之前加入 auth gate 并精确核对 task.cancel。受理后的独立 drain 不因管理员随后撤权而取消已经持久受理的操作，仍按原 UUID/轮次处理。
- 系统设置：`SystemSettingsManager._update` 的独立会话内在真实修改前进入 gate，重读当前 actor 权限；现有应用锁、revision CAS、commit 后同步发布/shield/未知提交恢复不变（`system_settings_service.py:118-181`）。不能仅在路由通过一次。
- 回收站：第一阶段取 gate、核实 project.recycle、锁父行并提交意图后释放；等待真实 drain 时不持 gate；第二阶段重新 gate -> actor/owner -> Project，重查相同权限、启用状态及删除 token。恢复/purge 同样使用各自权限，系统 expire 保持内部路径。现有第二阶段 `is_admin` 重查见 `recycle_bin_service.py:68-88`。
- 其他人工内容/工坊/全局 Skill 管理写：都必须持 gate 至其既有业务提交；已经接入 AuditOperation 的动作仍与成功审计共同提交，service 的显式写前守卫不能被 `AuditOperation.enabled=False` 替代。不因此给原来未纳入统一审计的所有人工模块顺带扩大全量采集。
- Actor 的请求会话版本在已有路径中必须保持 primitive 快照；不要在刷新 User 后把新 token_version 当作老请求已验证版本。独立会话（如设置 manager）需要传递已认证 snapshot，而非只有 actor_id 时偷换成新会话。

#### 6.4 角色修改/删除与分配竞争

- Role 有 revision；PATCH/DELETE 带预期 revision，UPDATE/DELETE 条件含该 revision。冲突统一 409，前端保留草稿并刷新，不自动重发旧变更。
- 用户角色替换有独立目标授权 revision（或等价明确 CAS），整体提交多角色列表；同目标并发保存必须只有一个赢家，不能最后到达静默覆盖。
- 新 Role 整数主键应使用 `sqlite_autoincrement=True`；否则最高 role_id 删除后重建可能复用，旧 role_id/revision 组合会误指新实体。角色操作同时带全局授权 revision 与资源 revision，可进一步防止跨角色/账号生命周期的陈旧请求。
- 在 gate 内检查角色仍可分配、目标成员关系与内置保护。DELETE 仅在成员引用数为零时执行，DB RESTRICT 兜底；分配事务也取同一 gate，避免“删除检查零成员 -> 新分配 -> 删除级联撤权”。
- 移除/禁用/删除超管时，在同一屏障内检验自身禁令与剩余启用超管。全目录权限自定义角色不能计为超管替代者。
- 权限快照版本用全局 AuthorizationState revision，可以覆盖“在用角色内容改动而用户分配不变”的撤权；仅用 User 的局部 revision 不够。返回权限列表与版本必须来自一致读快照，避免旧权限配新版本；不可将单独查询各表的不同快照冒充原子权限投影。

### 7. 建议的操作目录与前置依赖

代码名为技术设计候选，不是新增产品能力；最终应在一个后端目录定义，前端镜像类型并通过安全 catalog 读取描述/分组/依赖。角色与授权管理不出现在可委派目录。

| 分组 | 建议动作 | 现有入口 |
| --- | --- | --- |
| 首页 | `dashboard.read` | `api/v1/admin_dashboard.py` 三种 GET |
| 用户 | `users.read/create/status/reset_password/delete` | `api/v1/admin.py`；target 管理账号保护独立于动作允许 |
| 作品 | `projects.read/edit/recycle` | 管理目录、人工内容 helper、软删；edit 明示包含目前已开放的人工子资源维护，不含 owner-only AI/私有配置 |
| 回收站 | `recycle_bin.read/restore/purge` | 列表、恢复、彻删 |
| 任务 | `tasks.read/cancel` | 安全元数据与取消受理，绝不包含 output/retry/resume/finish |
| 日志 | `audit.read/cleanup` | 列表/安全详情、日期清理预览与执行 |
| 系统设置 | `settings.read/update` | 现有白名单运行设置，不开放完整 Settings |
| 用户配额 | `quotas.read/update` | 本任务新增安全额度投影与修改，管理账号修改仅超管 |
| 工坊 | `workshop.review.read/approve/reject`、`workshop.items.read/publish/update/hide/unhide/delete` | 所有管理与普通接口的管理分支；只读/审核不能自动得到官方发布 |
| 全局 Skill | `global_skills.create/update/references/delete/restore_builtin` | 全局资源的原创建、导入、修改、references、删除与恢复；可见全局读取/个人复制继续是普通能力 |

查看依赖要透明：能改某项不应默默获得另一个模块读取权限。catalog 可声明必须同时授予的读取依赖，角色保存校验并由 UI 清晰勾选；不能因为用户详情要显示作品列表，就隐式给 users.read 持有人 projects.read。工坊匿名作者/隐藏内容的管理读取必须归属工坊能力，而非“any admin”。

内置默认矩阵可按已讨论边界映射：super=全目录且独占角色授权；content_admin=作品人工维护/软删、回收站读取/恢复、工坊内容管理，不含作品 purge/平台设置/账号管理；reviewer=工坊投稿审核与上下架所需读取；user=零管理权限，保留自己的资源。其他能力可通过叠加自定义角色扩展。

### 8. 审计与安全输出

- 新角色 create/update/delete/copy、用户角色替换与配额修改进入既有显式 AuditOperation，成功与业务同事务；失败沿用回滚后安全摘要和 operation_id 的已提交事实识别（`audit_writer.py:187-254`）。
- bootstrap 当前只有用户名日志，不经 AuditOperation；writer 的系统 actor 例外仅支持 project.expire（`audit_writer.py:68-70`）。若角色调整审计覆盖启动恢复超管，应增加窄化内部 bootstrap 动作/系统身份路径，只在真实变更时记录且不携带密码，不开放任意 actor=None 的通用管理授权。若保持原日志作为例外，须在最终契约明确，不能默默漏记。
- writer 是否采集应看真实管理动作与对应权限/作用范围，不应仅 `is_admin`，也不宜因为“拥有任意管理权限”就把其普通个人投稿记为官方发布。保留个人作品和私人 Skill 的原排除条件（`:95-110`）。
- 工坊普通 submit 仅在拥有官方发布动作时走原自动官方发布分支；仅角色读取/用户管理权不能免审核，也不能看匿名作者的真实用户名（`workshop_service.py:43-47,291-300`）。
- 新摘要只保存稳定 role IDs、目录内权限代码、局部配额模式/数值与变更前后，限制列表长度与类型；不保存请求体、密码/hash、模型密钥、任意 raw JSON。扩展 `AuditAction/TargetType/Summary` 的局部白名单及 action 约束。
- 当前 `SkillOut.editable` 是一个布尔投影（`skill_service.py:55-80`），拆分修改/删除/恢复权限后不能继续靠一个 editable 同时启用全部按钮；可额外返回动作能力或使用 actor 权限结合资源归属派生，但服务端依然逐动作验证。

### 9. 必改文件与验证落点

核心新增建议：`backend/app/models/rbac.py`、`backend/app/schemas/rbac.py`、`backend/app/services/authorization_service.py`、`backend/app/services/role_service.py`、`backend/app/api/v1/admin_roles.py`；职责可依既有文件大小合并，但不引入外部 RBAC 框架。

核心现有文件：`models/__init__.py`、`core/deps.py`、`schemas/auth.py`、`schemas/admin.py`、`schemas/audit.py`、`services/auth_service.py`、`services/admin_service.py`、`services/project_service.py`、`services/audit_writer.py`、`services/audit_service.py`、`services/admin_task_service.py`、`services/system_settings_service.py`、`services/recycle_bin_service.py`、`services/workshop_service.py`、`services/skill_service.py`、对应管理路由与路由注册。人工 helper 的必要调用方见第 3 节，不顺便改 owner-only 服务。

测试必须覆盖：

1. 四类固定角色及自定义只读/高风险/全目录角色；直接接口绕过、未知权限、伪造内置标记、复制超管不变超管、多角色并集与撤回。
2. 旧 create/PATCH 带 is_admin/混合 is_active 的超管专用限制；缺权限时无业务副作用；最后超管、自身保护、禁用管理目标仍受保护。
3. User 角色分配、Role 在用编辑、目标由普通变管理与已接纳管理写的双向竞争，使用事件屏障而非 sleep；角色编辑不需要逐成员 token 自增也能即时撤权。
4. Role edit/edit、edit/delete、delete/assign 与相同用户角色替换 CAS；在用删除 409、DB RESTRICT；旧请求不能在获新 revision 后静默覆盖。
5. 管理写全部有审计，含非超管、自己的回收站处置、新角色/授权/配额；审计 flush/commit 故障回滚，无密码/权限请求原文泄漏。只读与普通个人写不误记管理行为。
6. 全部共享人工内容读写与版本恢复、封面、设定、知识库人工维护、记忆；只读角色逐入口不可写；AI/任务 output/私密资源/阅读进度仍严格隔离且 FakeLLM 零调用。
7. 工坊无关管理权限不看匿名作者、不看隐藏条目、不自动官方发布；审核员不意外得到删除/官方发布/元数据编辑；全局 Skill 每种动作独立且他人私有仍 404。
8. 设置等待应用锁期间撤权、任务取消等待 User 锁期间撤权、两阶段回收 drain 中撤权、审计清理预览后撤权；不破坏原系统设置持久发布及任务 UUID/取消受理契约。
9. 原回归落点：`test_admin.py`、`test_admin_user_details.py`、`test_admin_projects.py`、`test_admin_audit_logs.py`、`test_admin_tasks.py`、`test_admin_task_cancellation.py`、`test_admin_task_adversarial.py`、`test_recycle_bin*.py`、`test_system_settings*.py`、`test_workshop.py`、`test_skills.py`。新增 `test_rbac.py` / `test_rbac_concurrency.py` / `test_rbac_migrations.py` 等聚焦套件。

### 10. 迁移、初始化与回滚

- 当前最后迁移为 `0028`，父 `0027`（`backend/alembic/versions/0028_user_last_login.py:10-11`）；建议后续 `0029`，但实施前重新核对唯一 head。只创建实例级 RBAC 表/必要的 revision 列，不重建 users 父表，不破坏 Project→User RESTRICT 与私有资源 CASCADE。
- 四类固定 Role 元数据、auth state 固定行应在迁移初始化；`metadata.create_all` 不运行数据迁移，当前测试 fixture 也不触发 lifespan（`tests/conftest.py:46-66`），必须显式调用同一幂等初始化或提供受控 fixture，不能让测试碰巧只有超管 bool 就漏测角色数据。
- 固定角色元数据与权限目录一致性必须有检查；不可通过普通角色 PATCH 修改 system_key/is_builtin，不能用名称识别内置角色。
- 新表是实例/用户授权数据，与 project 无 FK，不登记项目备份 `NOT_EXPORTED` 来伪装项目级表；项目备份不得携带角色、权限或用户配额。导入普通项目不新增管理授权。
- **已证实 User 的 SQLite ID 可复用，不能把它当作新接口的稳定实体身份**：`models/entities.py:141-152` 未设 sqlite_autoincrement；`alembic/versions/0001_initial_schema.py:25-32` 创建 users 时也未设置，之后 users 仅 0003 增字段与 0028 原位加列。独立内存 SQLite 3.43.2 实验创建 id1/id2、删最大 id2、再插入新行时 users 重用 id2；同样实验为 Role 开 AUTOINCREMENT 后新行是 id3。该实验没有访问仓库业务数据库。
- 无 UserQuota 行时“目标 user_id + quota revision0”不足以防删号重建：建议新分配/额度写同时携带全局授权 revision 与对象 revision，并使普通账号删除也在 auth gate 内递增全局 revision。这样删除后的旧目标请求会因全局 revision 不符而拒绝，不必为了新接口重建 users 父表。读取版本与目标数据仍须同快照，不能先读新版 revision 再返回旧目标快照。
- 旧管理员 is_admin 保留便是超管迁移；禁用标记保留。bootstrap 通过同一 auth gate 保证启用超管但不重置密码，不重新给其他普通账号授权，不影响最近登录时间。
- 回滚不能只说“保留新增表即可回旧应用”：旧代码不认识自定义角色，新审计 Literal 也不认识 role/assignment/quota 的 action/target/summary（`schemas/audit.py:14-60,137-180`）。新业务授权或新审计已产生时，downgrade 应 fail-closed，不悄悄删除角色/授权/配额/日志或把所有管理账号提升成旧布尔管理员。优先停写、备份并前滚修复；确需旧版本，只能用已验证兼容版本或明确的备份恢复方案。
- 迁移测试至少验证 0028 存量普通/启用管理员/禁用管理员、作品/章节/私人资源、bootstrap 的密码与启用语义、固定角色与 association FK、SQLite foreign_key_check、受保护 downgrade 和空业务可回退。新旧授权版本不混跑，沿用现有单进程部署约束。

## Related specs

- `.trellis/spec/backend/auth-admin.md`：真实 actor、会话版本、用户安全输出、自身/最后管理员保护、登录快照。
- `.trellis/spec/backend/admin-projects.md`：人工维护与严格 owner 边界，以及完整人工服务清单。
- `.trellis/spec/backend/admin-audit.md`：显式事务边界、安全摘要、提交确认丢失与清理保护。
- `.trellis/spec/backend/admin-task-monitoring.md`：User 锁顺序、会话版本、受理/执行区分、UUID/轮次与 drain。
- `.trellis/spec/backend/system-settings.md`：应用锁/独立会话、revision、持久后发布与 shield。
- `.trellis/spec/backend/recycle-bin.md`：两阶段删除、父行生命周期屏障、真实排空、系统身份。
- `.trellis/spec/backend/skills-mcp.md`、`.trellis/spec/backend/workshop.md`：可见/可写边界、私密资源、工坊审核与匿名。
- `.trellis/spec/backend/database-guidelines.md`、`.trellis/spec/backend/quality-guidelines.md`：迁移与 DB 约束、测试隔离、FakeLLM 和 PostgreSQL 证据界限。

## External references / dependencies

未增加外部引用或依赖。方案仅复用仓库已有 FastAPI dependency、SQLAlchemy 异步 session、Pydantic 校验、Alembic、pytest、SQLite/PostgreSQL 及显式 no-op UPDATE 屏障。本文的 DB 竞争充分性是设计推理，不是已经运行的并发验收证据。

## Caveats / Not Found

- 本轮仅静态调研。未执行测试、Alembic 或真实 PostgreSQL，多连接 SQLite 的等待/繁忙表现与 PG 锁顺序必须在实施验收中实测；SQLite 测试成功不能证明 PG 行锁正确。
- 单全局 auth gate 是简单而保守的技术建议，不是当前既有机制。若实施改用细粒度角色/User 锁，必须给出等价线性化与统一锁序证明，尤其覆盖 Role 权限修改影响所有成员的情形。
- 权限目录的候选代码与项目子资源动作分组由最终 design 固化；本文不擅自新增功能或将前端 UI 的读取需求变成隐式扩权。
- 新角色/授权读取接口、权限版本与 effective_permissions 的投影必须在同一一致快照中构建；不能因为单次请求内读取了三张表，就声称在默认 READ COMMITTED 下天然一致。
- 上述新写接口的全局 revision **不修复既有 SQLite 下旧 JWT 身份复用问题**：JWT 仅 sub/ver（`core/deps.py:77-83`），User 的 token_version 默认0（`models/entities.py:150`）；若旧 User ID 删除后被新账号复用且版本相同，旧 token 可能被重新解释成新实体。此风险在本任务前已存在，PostgreSQL 常规序列分配不具有同样的默认最高 ID 复用行为。是否扩展实体 nonce/单调 User ID 分配属于主会话范围与风险决策；不能宣称仅新增授权写 revision 已经保证旧会话实体隔离，也不应无声重构认证。
- 未发现现有 Role/UserRole、通用 permission resolver 或可直接复用的 RBAC 框架；不应把人物/职业模型中的 `role` 字段误当账号角色。
