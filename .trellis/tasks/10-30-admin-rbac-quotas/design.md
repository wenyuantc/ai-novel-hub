# 管理员权限分级与用户配额：技术设计

日期：2026-11-01。状态：用户已在最终摘要后回复“开始”，**当前方案已批准实施**。产品权威来源为 `prd.md`（D1–D8、R1–R6、AC1–AC11）。

## 1. 变更边界与证据

最小行为差异：将现有“管理员全部放行”变成明确的操作级管理权限，并在真实作品创建/后台任务领取处执行两项用户配额。沿用 FastAPI → service → SQLAlchemy、React → api/client，不增加第三方 RBAC、队列、缓存或 UI 框架。

三份内部研究给出完整 `file:line`、必改清单及风险：

- `research/backend-rbac-contracts.md`：二元授权、约18个人工内容服务、审计、角色表示、撤权屏障与迁移。
- `research/backend-quota-contracts.md`：四处真正作品创建、候选/真实运行占用、领取与设置发布、锁序及测试。
- `research/frontend-rbac-quota-contracts.md`：菜单/路由/表单/快捷键/actor gate、跨域读取依赖、403与权限刷新。

不改严格 owner 的含义，不把 actor 替换成 owner，不扩权到他人模型/Key/私人配置、AI/智能体、任务输出/重试/继续、阅读进度或作品备份。无多worker调度、角色继承、deny规则、按作品ACL、计费或额外配额指标。尚未实现的管理员备份管理不加入虚构权限项。

## 2. 数据、角色表示与初始化

保持现有不声明 ORM relationship 的规范，显式 select/join。计划新增四张实例/用户级表；实施前复核唯一 Alembic head（当前0028，计划0029），不重建 users/projects 父表。

| 表 | 主要字段与约束 |
| --- | --- |
| `roles` | Integer PK，SQLite AUTOINCREMENT；唯一可空 `system_key` 标识四个内置角色；名称/说明；允许权限代码JSON列表；`revision >= 1`；时间。内置标识与定义不能由API改写，名称不可用来推断权限。 |
| `user_roles` | `(user_id, role_id)`组合主键；User FK CASCADE；Role FK RESTRICT；保存内容管理员、审核员与自定义角色分配。 |
| `authorization_state` | 固定 `id=1`、单调非负revision；既是数据库写屏障也是权限快照版本来源，不包含密钥。 |
| `user_quotas` | User PK/FK CASCADE；两项mode/value；`revision >= 1`；时间。无行表示两项inherit、revision0；CHECK约束mode/value组合。 |

### 超管与普通基线只有一个事实来源

- `User.is_admin` 保留为**唯一超管身份事实**，不再代表“具有任意管理权限”。旧管理员无需双写迁移即可成为超管，原启用/禁用、密码、token_version、last_login_at不变。
- 四个内置Role元数据均可持久化。super的成员由is_admin投影，不再保存另一条可独立变化的super UserRole；ordinary是个人能力基线，不要求依靠membership才能使用自己的资源。
- 专用分配服务将请求中的super角色转换为受保护的is_admin更新，其余真实分配进入UserRole；空集合退回普通基线。super/ordinary关联行即使异常出现，也不能成为提权来源。
- 自定义角色即使勾选所有可委派权限，也不是超管，不能管理角色/授权或维护管理账号。复制super只复制可委派权限，不复制特殊身份；复制ordinary得到无后台管理权限的自定义角色。
- 安全输出 `is_super_admin` 来自同一is_admin；`is_management_account` 为超管或有效管理权限非空，与角色名、角色数量和账号启用状态无关。

迁移初始化内置目录和state行；测试的create_all不会运行迁移/lifespan，因此fixture显式调用同一个幂等初始化原语。运行时遇到缺失/非法授权状态不静默全权放行，也不通过普通读接口修复数据库。新表不涉及projects外键，不登记进项目备份NOT_EXPORTED，不改备份格式/APP_SCHEMA，不从小说导入角色或配额。

## 3. 统一权限目录与默认矩阵

目录定义放在一个后端所有者模块（建议 `app/core/permissions.py`），含稳定代码、中文标签、分组、危险标记和显式读取依赖。请求只接受目录内代码，拒绝通配符、未知代码、deny、伪造内置标记；权限列表去重/规范顺序由边界统一处理。

| 分组 | 固定可委派代码 |
| --- | --- |
| 首页 | `dashboard.read` |
| 账号 | `users.read`、`users.create`、`users.status`、`users.reset_password`、`users.delete` |
| 配额 | `quotas.read`、`quotas.update` |
| 人工作品 | `projects.read`、`projects.edit`、`projects.recycle` |
| 回收站 | `recycle_bin.read`、`recycle_bin.restore`、`recycle_bin.purge` |
| 任务 | `tasks.read`、`tasks.cancel` |
| 日志 | `audit.read`、`audit.cleanup` |
| 系统设置 | `settings.read`、`settings.update` |
| 工坊 | `workshop.read`、`workshop.approve`、`workshop.reject`、`workshop.publish`、`workshop.update`、`workshop.hide`、`workshop.unhide`、`workshop.delete` |
| 全局Skill | `global_skills.create`、`global_skills.update`、`global_skills.references`、`global_skills.delete`、`global_skills.restore_builtin` |

角色/授权管理没有可委派代码，使用超管专用依赖。`projects.edit` 的标签明确为“人工内容维护（含子资源增删/版本恢复）”，覆盖现有人工维护面，不伪装成仅修改作品资料；作品彻删是独立purge权限，不随edit授予。

| 内置角色 | 默认权限 |
| --- | --- |
| super_admin | 全目录，并具备不可委派的角色/授权管理身份 |
| content_admin | projects三项、recycle_bin.read/restore、全部workshop操作；无账号/配额/设置/任务处置/审计/全局Skill/作品purge默认授权 |
| reviewer | workshop.read/approve/reject/hide/unhide |
| user | 零管理权限；个人资源/公开能力沿用原规则 |

### 读取依赖不得变成隐式扩权

- 同域变更项需要对应read；global Skill原本全局可读，不额外发明私密读取权；references/restore_builtin依赖global_skills.update。
- 账号变更项依赖users.read。配额编辑复用用户详情：quotas.read需**显式同时授予users.read**，quotas.update再依赖quotas.read；目录/UI明确展示这些前置项，后端拒绝缺失依赖的角色，不在运行时从update偷偷推导read。
- 用户详情的所属作品区块只有projects.read才挂载；回收站深链另需recycle_bin.read。作品/任务owner搜索没有users.read时不请求完整用户目录，保留合法owner ID筛选/深链，不增加隐式权限。
- audit.read不包含关联章节版本正文，后者仍需projects.read。无关管理权不获得工坊匿名作者/隐藏内容/待审核资料，也不自动官方发布投稿。

## 4. 身份读取、写前撤权与共同锁序

### 私有权限快照

CurrentUser仍返回User，保留原密码/禁用/会话版本行为；另建不可变的 `AuthenticatedActor(id, token_version)` 和 `AuthorizationSnapshot`。snapshot输出 `{is_super_admin, roles, effective_permissions, authorization_revision}`，不得放JWT或接收浏览器回传的effective_permissions作为事实。

权限列表、账号/角色摘要和state revision必须来自**同一数据库读快照**（优先一条显式join查询再聚合），不能在默认READ COMMITTED下用几个独立查询拼出“旧权限+新版本”。管理目标详情同理，返回is_management_account和授权revision，省略所有哈希/密钥/内部会话字段。

login/register/me投影一致，me为no-store。登录仍使用已核验的认证快照签Cookie；新增角色查询不能在commit后refresh出新token_version再给旧密码签新版本。独立设置会话等入口接收已认证primitive快照，不能只拿actor_id重新读取新版本并假定旧请求通过了它。

### 写屏障

采用同一应用级短协调临界段（覆盖提交返回与运行态发布）加 `authorization_state` 真实no-op UPDATE数据库锁。由统一授权服务管理，所有session/manager/TaskEngine拿到同一协调器，不能各建一个名字相同但不相通的锁。

固定顺序：`SystemSettingsManager已有_lock（仅设置路径） → 共享runtime协调器 → auth DB gate → User IDs升序 → Role/业务资源`。其他路径从runtime开始，**不得**反向调用会获取设置_lock的load/update。同事务重入通过统一受控helper标识处理，不对非重入锁重复acquire。

- 角色定义/分配、超管/账号管理变更、配额写及非owner管理写均先取屏障，再重读actor启用/原token版本、所需具体权限和目标管理属性，持至业务commit；属于既有明确审计范围或本任务新增审计动作时，成功审计与业务同事务。
- 屏障/权限重读使用no_autoflush并避开identity-map旧快照，显式保留无关updated_at，不能在授权前自动flush调用方的待写业务。持久权限数据非法时安全报错，不将其默认为普通账号以绕过目标保护。
- 角色/授权的实际变化和账号删除（包括普通用户）推进全局revision；普通内容写仅取屏障，不使所有身份快照版本变化。Role本身另有revision；配额另有revision。
- 非超管账号管理只能作用于无管理能力的目标；目标通过角色编辑新获管理权与该账号修改也在同一屏障下串行。
- 普通owner内容操作不新增全站管理锁，不能因actor本人恰有管理角色，就把所有个人创作变成管理员操作。授权判定由业务完成，AuditOperation不是授权器。
- 不持管理屏障等待模型、外部工具或任务drain；bcrypt/解析等纯计算尽量放取锁前，写前仍重新核验原认证版本及目标。

**充分性**：影响权限的变更与使用管理权限的写争用同一DB行。撤权先提交则后到写在锁后重读并拒绝；写先获锁则撤权等待它提交，该写属于撤权前行为。仅锁actor User不充分，因为编辑Role可同时改变所有成员而不修改成员User；没有共同序列点会允许旧权限写晚提交。runtime临界段还覆盖数据库已释放锁但commit await尚未返回的内存发布窗口。

### 特殊路径

- 人工内容helper显式区分read/edit/recycle；逐个迁移研究列出的service，不根据HTTP方法或lock=True猜权限。子资源仍核对project/owner/生命周期；严格owner helper不放宽。
- 两阶段回收：第一阶段核验后提交意图并释放锁，drain结束再按同序取锁、复核权限/原token/删除代次。撤权不允许旧第二阶段继续写。系统expire只走内部专用路径。
- 任务取消保留原actor/owner锁序、token版本、UUID/轮次CAS、持久受理与审计；已持久受理的独立drain不因后续撤权被当作未受理回滚。
- 设置manager的独立会话也复核权限；保留revision、shield、自有会话、提交确认丢失重读与关停等待。
- 原 `is_admin` HTTP创建/更新字段不再作为角色写入口：新版schema禁止此字段（422，无副作用），引导使用专用带revision的角色分配。内部bootstrap仍可使用既有创建原语，原有状态/密码端点按权限继续工作；不能为旧HTTP客户端保留无CAS提权旁路。

## 5. 角色与用户配额API契约

路由只做依赖、schema和service调用，响应no-store；类型由后端schema定义、前端types.ts镜像。新请求extra=forbid，revision/整数严格校验，禁止bool冒充int。

| 方法/路径（/api/v1下） | 请求与语义 |
| --- | --- |
| GET `/admin/permissions` | 超管专用，返回代码/标签/分组/危险标记/显式依赖 |
| GET `/admin/roles`，GET `/admin/roles/{id}` | 超管专用，分页/名称搜索、内置标记、revision、成员数与权限摘要/详情，带authorization_revision |
| POST `/admin/roles` | 超管；创建自定义角色。复制复用此创建原语，source_role_id/source_revision仅作受校验来源，不可复制特殊身份；名字/说明有界，无任意raw JSON |
| PATCH `/admin/roles/{id}` | 超管；`{authorization_revision, revision, name?, description?, permissions?}`，省略不变、permissions给出即整体替换；拒绝内置修改 |
| DELETE `/admin/roles/{id}` | 超管；携带全局及角色revision，内置/仍有成员拒绝，FK RESTRICT兜底 |
| PUT `/admin/users/{id}/roles` | 超管；`{authorization_revision, role_ids}`整体替换，空数组=普通基线；自己移除super及最后启用super受保护 |
| GET `/admin/users/{id}/quotas` | quotas.read；仅安全身份/两项配置、有效值、来源、占用、quota revision及authorization_revision |
| PATCH `/admin/users/{id}/quotas` | quotas.update；`{authorization_revision, revision, changes:{project_count?, task_concurrency?}}`；非空局部变更，每项mode判别联合；管理目标另要求超管 |

两项mode统一为 `inherit | limited | unlimited`。limited必须带 `value: StrictInt`，范围0..2_147_483_647（SQL Integer技术边界）；其他mode不允许带value。无行revision0；effective_limit用null表示不限额，不能同时拿null表示继承。source明确为 `user | system | default`：用户覆盖为user，任务inherit为现有系统设置有效值，作品inherit为代码默认不限额。

角色替换用全局revision CAS保护，Role/Quota另校验对象revision。全局revision变化可能使无关管理表单需要刷新，接受低频管理场景的保守冲突，不自动重放。新角色AUTOINCREMENT防整数ID复用；账号删除推进全局revision使旧分配/额度请求不能命中后来复用同user_id的账号。

错误：认证仍401；缺权限403 `code=permission_denied`；受保护目标403 `management_account_protected`；非法输入422；作品超额409 `quota_exceeded`（只含安全metric/used/limit）；角色在用409 `role_in_use`；旧revision409 `stale_revision`；不存在404。自身和最后超管沿用既有400/409保护语义。业务422不回显密码或整份请求。

## 6. 作品数：四处创建共用同一准入

新增窄 `quota_service`，统一解析有效值、全生命周期计数、用户父行屏障与安全投影，不维护会漂移的“已用作品数”缓存。

四个实际入口：`project_service.create_project`、`inspiration_service.convert`、`project_transfer/importer.import_backup`（包括直接multipart）、`book_import/service.start`。向导/应用到已有作品/恢复不创建新Project，不重复扣名额。

每处在任何持久副作用前，以真实owner User作no-op UPDATE（显式保持updated_at），读最新UserQuota，COUNT全部Project，仅在n<L或无限时允许创建，持User锁直到原有整笔业务commit/rollback。所有显式额度更新也锁同一User。作品默认不可编辑，无需为了创建再取全站auth gate；**不得在持User后反向取gate**。

- 备份准入在导入风格/子表写入前，预览不占名额；拒绝不消费预览token，不留风格/作品半成品，不提前commit缩短锁。
- 灵感准入失败不改变converted状态；锁后重读会话，避免两个旧快照都转为作品。
- 拆书调整为User→BookImport再重验revision/归属，避免与删号User→级联解析记录反锁；失败保留解析记录，不留章节或queued任务。
- 软删不释放，恢复不再扣；purge/expire物理提交成功才释放。现有数据超新上限保留且可继续维护，后续新增拒绝。

**最坏情况保证**：同owner所有新增在父User下串行，每次锁后按最新n检查n<L，成功后n+1≤L。跨入口再多并发也至多填满剩余名额。无共同屏障时，两个请求在n=L−1同时通过即可产生L+1，所以单次前置查询/前端判断没有匹配保证。降低至当前占用以下是保留旧数据的明确例外，不能宣称历史存量被强制压回L。

## 7. 任务并发：预筛选不等于真实准入

保持单进程TaskEngine、FIFO、serial/resource互斥、paused不占并发及超额仍可排队。无需修改所有入队处为拒绝：手动/重试/继续、自动分析与拆书派生最终共用引擎领取检查。

- `_dispatch` 批量读每用户有效额度做预筛选，不再用统一task_max_per_user阻挡显式更高值/无限用户；0显式跳过，避免拒领后的notify热循环。候选仍先注册执行体，保留DB出现running前已有可取消执行体的不变式。
- 增加按任务UUID+当前轮次绑定的admitted登记，区别于候选 `_running_users`。真实占用覆盖runner finally、DB终态后的on_success，直到真实 `_on_done` 才释放；不能用数据库running行数代替。
- `_claim_and_run` 在共享runtime→DB gate→owner User内读最新用户额度和共享Settings的全局值；只按admitted计数，候选不互相判满。允许才做原UUID/status/生命周期/取消意图CAS和统计，commit确认后**仍持runtime**同步登记admitted，再释放并执行。
- 超限候选保持queued，不伪造running→queued新轮次、不改started_at、不取消已运行任务。提高/恢复继承后提交成功notify；失败不发布缓存额度。最小方案不另建持久额度缓存。
- 所有活跃期Settings `_publish` 也进入同一runtime，包括load、更新前_reload、未知提交/后台恢复，不只成功PATCH。设置_lock始终在runtime之前，领取不得反向调用设置load/update。
- DB真实提交可能早于commit await返回，**仅DB行锁+“返回后无await”不足以保护内存占位**；runtime必须持到admitted/Settings同步发布。新增可控屏障回归专测这个窗口。
- 提交确认未知时，不盲重放领取/额度写，也不在未确认状态启动外部执行；按UUID/轮次重读，保守保留占位/暂停后续准入直到确认，复用现有任务恢复边界，不私自吞异常。

读取配额的task_concurrency.used为引擎真实admitted占用，不是queued/running/paused活动任务总数；Project用量是SQL当前计数。两项可来自邻近实时快照，不承诺跨DB/内存原子历史时刻。

## 8. 前端实现与权限刷新

- 沿用AntD/主题token、局部controller、现有Cookie客户端与路由懒加载。新增 `/admin/roles` 超管页；列表/复制/编辑表单/成员数/在用删除提示；多角色分配和配额编辑复用管理员用户列表/详情，不新建无关运营看板或配额独立后台。
- 扩展types/auth为显式super、effective_permissions、roles及revision。统一 `can(permission)` / 超管判断；RequireAdmin升级为参数化RequirePermission，全部桌面/移动/账户菜单与面包屑一致。没有dashboard权限时入口指向首个可读管理页，不强制跳首页403。
- 权限私有刷新独立于auth.init/publicConfig：复用visiblePolling，登录态下可见30秒/回前台/进入管理路由刷新；具体RBAC403触发single-flight刷新，不自动重发业务。401才清会话；网络失败保留最近成功快照，不能因权限轮询失败登出。
- actor/account epoch、authorization_revision与动作/目标请求代次共同保护异步；降权再恢复也不接纳前一代响应。只失写权则撤下确认框并转只读，失读权才清对应管理数据；不使用全局Modal.destroyAll。
- `projectAccess/useProjectAccess` 明确canRead/canEdit/canRecycle及原canUseAI。逐个覆盖人工编辑页、关联子抽屉、封面、章节标题/正文/Cmd-S、版本恢复、知识库人工维护；不能只改顶部按钮。失他人项目read撤下他人内容，本人作品能力/草稿不一概清空。
- 工坊/Skill同样逐动作授权和失权清理；Skill原editable不能一口气开启删除/恢复按钮。任务取消失权不撤掉合法只读列表；MCP关闭等业务403不误作角色撤权。
- 配额表显示继承/数值/无限、有效来源与占用；0不经 `value || default` 转换。管理目标且actor非超管不挂编辑动作。作品创建/灵感/导入等拒绝保留表单并展示统一安全额度错误。

## 9. 审计与回滚

新动作包括role.create/update/delete、user.roles.update、user.quotas.update；复制走role.create并记录安全来源ID。扩展既有AuditAction/TargetType及有界专用摘要：稳定ID、目录内代码、mode/value前后与截断计数，不存请求原文、密码/hash、密钥或任意JSON。

AuditOperation不再仅看is_admin才采集：**既有明确审计范围内的动作**由自定义角色执行时同样不能漏审，新增角色定义/分配/配额与窄内部bootstrap按本任务接入。其他原未采集的人工子模块不因更换授权模型扩大全量采集，个人作品/私人Skill原排除规则保留；所有管理写仍需权限屏障并保留各自提交边界。缺权限业务由service拒绝，不把audit.enabled=False当作授权结果。

bootstrap真实创建/恢复超管增加**窄内部系统审计动作**，只允许启动代码的明确内部来源、无HTTP入口、只在变更时记录，不开放任意actor=None。无变化仍幂等、不改密码/last_login。迁移本身不伪造人类操作者。

上线需停旧服务→备份→迁移→新后端/匹配前端→smoke，不混跑新旧授权/调度版本。旧应用不认识自定义角色、新审计或配额，保留新增表也不等于能安全回滚；一旦有自定义授权/显式配额/新审计，downgrade受保护拒绝，优先停写后前滚修复。只有确认无新语义数据的空业务状态才允许无损结构回退，不自动清日志/升权/扔配额。

## 10. 风险与验证边界

- 管理写共享屏障降低吞吐，但符合当前单进程/低频管理；不持锁等待外部drain。大型导入的owner锁仍可能使后续gate→User等待形成全站队头阻塞；解析/计算尽量锁外，必要时PG领取非阻塞跳过繁忙owner，不能靠反序锁/提前commit牺牲一致性。
- 真实PG独立事务并发验证必须覆盖角色撤回、互相降权、最后作品名额、Role删除/分配和领取/调低；SQLite通过不等于PG行锁通过。不可运行时明确记录未验，不能宣称全场景保证已验证。
- 研究已证实SQLite users最高ID可复用，旧JWT仅sub/ver存在本任务前的实体复用风险；新授权/额度写的全局revision仅保护新写接口，不修旧JWT。生产仍沿用PostgreSQL，不无声扩大本任务为JWT/用户实体重构，不声称旧会话实体隔离问题已解决。
- 研究属于静态证据，产品尚未实现/测试；具体门禁和AC映射见 `implement.md`。发现与已确认需求冲突时回到规划，不在实现中悄悄降低范围或安全语义。
