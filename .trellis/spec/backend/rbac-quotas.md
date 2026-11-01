# 管理角色、授权屏障与用户配额（跨层契约）

## 1. 范围 / 触发条件

修改后台权限、账号角色/目标保护、用户配额、作品创建或任务准入时适用。来源任务为 `10-30-admin-rbac-quotas`，迁移 `0029`（父 `0028`）。本文件替代旧规范的“任意管理员全量放行”假设；认证 Cookie、真实 actor/owner、生命周期、审计和任务轮次契约继续有效。

代码事实源：`app/core/permissions.py`、`app/services/{authorization,role,quota}_service.py`、`app/models/rbac.py`、`app/schemas/rbac.py`；前端镜像在 `src/api/types.ts`，判定工具在 `src/utils/permissions.ts`。不新增第三方依赖、deny、角色继承或通配符。

## 2. 签名（API / DB / 服务）

所有管理响应 `Cache-Control: no-store`，路径前缀 `/api/v1`：

| 方法 / 路径 | 权限 / 请求 |
| --- | --- |
| GET `/admin/permissions` | 仅超管，返回 `{items: PermissionDefinition[]}` |
| GET `/admin/roles`、`/admin/roles/{id}` | 仅超管；列表 `q?, page=1, page_size=20`，返回 `{items,total,authorization_revision}` |
| POST `/admin/roles` | 仅超管；`{name,description?,permissions?,source_role_id?,source_revision?}`，201 RoleOut |
| PATCH `/admin/roles/{id}` | 仅超管；`{authorization_revision,revision,name?,description?,permissions?}` |
| DELETE `/admin/roles/{id}` | 仅超管；query `authorization_revision,revision`，204 |
| PUT `/admin/users/{id}/roles` | 仅超管；`{authorization_revision,role_ids:number[]}`，整体替换，返回 AdminUserOut |
| GET `/admin/users/{id}/quotas` | `quotas.read`，返回 UserQuotaOut |
| PATCH `/admin/users/{id}/quotas` | `quotas.update`；`{authorization_revision,revision,changes:{project_count?,task_concurrency?}}` |

```python
# app/services/authorization_service.py
async def require(db, actor, permission: str | None = None, *, write=False, targets=()) -> AuthorizationSnapshot
# permission=None 明确表示仅超管，不是“任意管理权限”
async def gate(db) -> None
async def lock_users(db, *ids: int, missing_ok=False) -> None
def snapshot_statement(*, include_token_version=True)
def snapshots_from_rows(rows) -> dict[int, AuthorizationSnapshot]

# app/services/quota_service.py
async def check_project_creation(db, user_id: int) -> None
def resolve(row, metric: str, settings) -> tuple[int | None, str]
```

DB 新增四表，不重建 users/projects：
- `roles`：唯一 name≤100、可空唯一 system_key、permissions JSON、revision≥1；SQLite AUTOINCREMENT，不复用旧角色ID。
- `user_roles`：复合主键 user_id/role_id；User FK CASCADE，Role FK RESTRICT。**不存 super_admin/user 的独立成员事实**。
- `authorization_state`：固定 id=1、revision≥0；管理授权串行点与全局陈旧写 CAS。
- `user_quotas`：user_id主键/FK CASCADE、两组 mode/value、revision≥1；limited值非空且≥0，inherit/unlimited值必须NULL。

迁移与 `authorization_service.initialize` 共用相同内置语义；create_all测试必须显式初始化。启动顺序为授权初始化→设置load→bootstrap→恢复/启动任务。新表不属项目备份，不登记项目相关表注册表。存在自定义角色/成员/任何user_quotas记录（含已恢复inherit但仍保留revision的行）/新RBAC审计时拒绝downgrade，不能删除数据假装兼容旧应用。

## 3. 契约（请求 / 响应 / 运行时）

### 身份、目录与目标保护

- **唯一超管事实为 `User.is_admin`**；`is_super_admin` 是其安全输出名。super_admin角色元数据由此投影；无显式成员的非超管投影普通用户。复制super只复制33项可委派动作，不复制身份、角色/授权管理权。
- 四内置角色只读可复制：super_admin全部33项；content_admin为projects三项、workshop八项、recycle_bin.read/restore共13项；reviewer为workshop.read/approve/reject/hide/unhide五项；user无后台项。任意角色都保留本人个人能力。
- 账号可多角色、取权限并集。权限目录唯一来源为 `PERMISSIONS`；变更动作的requires必须显式存在，后端不暗补。角色名trim后非空≤100、说明≤500，未知/重复项拒绝。
- 只有超管管理角色/分配。非超管即使拥有全部可委派项，也不能更改任何管理账号的状态、管理员重置密码、删除或配额；管理账号指超管或有效权限非空，禁用后仍受保护。安全读取和本人自助改密不误禁。
- `AdminUserCreate={username,password}`；PATCH仅 `is_active?`，旧HTTP `is_admin` 字段422，不能留无revision提权旁路。自身/最后启用超管保护继续执行。
- 旧is_admin存量原样成为超管，不改变禁用状态/密码/会话/登录时间。bootstrap配置账号确保启用超管、不覆盖密码；只用内部窄 `user.roles.bootstrap` 系统审计。
- 仅角色变化不递增token_version、不强制退出。每请求查当前权限；管理写在真正写前重验原已认证token_version、启用、具体动作和目标属性。

`UserOut` 必含 `is_super_admin,roles:RoleBrief[],effective_permissions:PermissionCode[],authorization_revision`，保留旧安全字段与is_admin兼容输出；`AdminUserOut` 必含 `is_super_admin,is_management_account,roles,authorization_revision`，**不返回目标完整权限/token_version**。RoleBrief为 `{id,name,system_key,is_builtin}`；RoleOut另含 description/permissions/revision/member_count/created_at/updated_at/authorization_revision。

用户列表/详情、Role和Quota读模型必须同一SELECT取得身份/角色/authorization_revision，不得旧User拼新revision或逐用户N+1 snapshot；详情原三生命周期资源聚合仍保持一次SELECT。

### 锁序、提交和撤权

```text
系统设置既有 _lock（仅设置路径）
  → 同一 AuthorizationRuntime
  → authorization_state no-op UPDATE（DB gate）
  → User ID 升序 no-op UPDATE
  → 业务资源 / 原生命周期锁
  → 原业务与成功审计提交
```

`create_sessionmaker` 的session.info共享一个runtime，不能每session新建协调器。`authz.require(...,write=True,targets=(...))` 在资源锁前取得gate，租约由根事务结束事件释放。最后超管保护在gate下exists，不能在User锁后再无序锁全体User。回收站drain期间不持gate，第二阶段用原actor primitive重新授权。

作品创建只取owner User屏障（无可编辑的全局作品默认，不反向取gate），与所有配额修改同序。设置load/reload/recovery/保存的**所有发布路径**与领取共用runtime；commit确认后无await同步发布。提交事实未知时 `settings_confirmed=False` 暂停新准入，dispatch返回而非热循环；在同runtime内恢复确认后再开放。继续沿用单进程TaskEngine，不宣称多实例租约。

新增 `role.create/update/delete`、`user.roles.update/bootstrap`、`user.quotas.update` 审计，安全类型化 `rbac_change` 只存角色ID/允许权限/两项额度前后值；原已采集管理动作在自定义角色下也不能漏审。**不扩大全量人工子模块/本人日常/普通AI审计**。业务与成功日志同事务，未知提交不补相反失败。

### 两项配额及真实准入

```typescript
type QuotaConfig = { mode: 'inherit' } | { mode: 'unlimited' }
  | { mode: 'limited'; value: number }
// value 是严格整数 0..2147483647，非limited禁止value
// QuotaItem = {config,effective_limit:number|null,source:'user'|'system'|'default',used}
```

UserQuotaOut包含 user_id/username/is_management_account/revision/authorization_revision/global_task_limit/project_count/task_concurrency。无行revision=0、两项inherit。作品inherit=默认不限，任务inherit=现有有效task_max_per_user；不加第七项系统设置。null有效上限仅表示不限，不能表示继承；0禁止新增/新启动。

- `project_count.used` 包含全部未物理删除作品（正常/回收站/删除中/过期待清理）。降低不删数据、软删不释放、恢复不重复扣、purge完成才释放。所有角色含超管均不豁免。
- 普通创建、灵感转换、备份实际导入、拆书start，均在任何作品/风格/状态/token消费/Task副作用前锁owner，并持至原整体提交。拆书按User→BookImport锁并重验revision，灵感锁后重读converted状态。预览不预占。
- 用户任务unlimited仍受全站task_max_concurrency硬顶。达到并发上限继续queued，不失败/取消；降低不强杀已执行体。
- `_dispatch` 批量预筛选不等于准入；候选与admitted分离，真正claim在runtime/gate/User后用最新额度/全局顶复核。commit确认后在runtime内同步发布admitted才执行，直到真实_on_done（含on_success）释放；DB终态不是释放时刻。
- `_claiming` 保护须覆盖领取session的异步 `__aexit__`，随后无await进入 `_run` 的try/finalize。提前清标记会使owner取消/stop留下孤立running；取消只登记意图，由真正运行体安全收尾。

### 前端读取、异步失效和人工只读

- `RequirePermission` 对每管理路由声明具体read；角色路由superOnly。菜单/移动/账户/面包屑同能力，不固定跳无权dashboard。目录之间独立检查users/projects/recycle/audit读权。
- 私有授权刷新独立于init与公开配置；路由进入、可见轮询/回前台及权限403触发。403不登出、不自动重发业务；401才清会话。epoch+请求代次阻止旧me/401/logout覆盖新login，旧logout菜单回调也须重验当前anonymous才导航。
- `useAuthorizationScope`/cleanup及页级controller管理本页Modal，不destroyAll。撤写只关失效动作、保留合法读取；撤读移除相应数据。仅reload不充分：工坊Browse按actor+workshop.read重挂，刷新500时也不得留下隐藏条目/匿名真名；个人草稿不因无关revision被清空。
- `projectAccess` 派生canRead/canEdit/canRecycle/canUseAI。只读覆盖正文/标题/选角/Meta-S、版本恢复、阶段编辑器（即使在Form外）、成员/职业卸下、伏笔及移动章节操作。事件提交前再查实时canEdit，不能只隐藏主按钮。
- owner-only AI/模型/Key/私人Skill/MCP/任务输出/生成日志/备份/阅读进度不扩权。管理他人时不得挂载这些自动请求/订阅；本人功能保留。
- 角色桌面Table、375px卡片共享动作规则；内置不可编辑删除、在用不可删。配额显示三态/来源/当前占用/全站顶，0不经truthy转为不限；409/未知结果保留草稿并先刷新核实。

## 4. 校验与错误矩阵

| 条件 | 结果 |
| --- | --- |
| 未登录、禁用或原token_version失效 | 401，不能把撤角色变成401 |
| 缺具体能力/非超管访问角色管理 | 403 code=permission_denied，可带required_permission |
| 非超管更改管理目标的受限账号动作/配额 | 403 code=management_account_protected |
| 未知/重复权限、缺依赖、旧HTTP is_admin、非法mode/value、空changes | 安全422，无业务副作用 |
| 全局授权或对象revision过期 | 409 code=stale_revision，不自动覆盖/重发 |
| 内置变更/在用角色删除 | 409 builtin_role_readonly / role_in_use，FK RESTRICT兜底 |
| 作品used≥有限limit（含0） | 409 quota_exceeded，仅安全metric/used/limit |
| 任务并发已满/为0 | 仍queued，不能错误宣布失败 |
| 目标不存在/未开放owner-only能力 | 原404；原自身/最后超管护栏维持400/409 |
| 持久权限/额度无效 | 固定安全500，fail-closed，不静默补超管或无限 |

## 5. 正例 / 基准 / 反例

- 正例：两个角色分别projects.read/edit和tasks.read，并集可以人工改他人正文/读安全任务，不可读他人模型或任务输出；撤edit仍能读，下一写被拒。
- 基准：无UserQuota行的普通用户作品不限、任务继承系统；显式0与unlimited是不同值。回收站3部时作品额度3仍满。
- 反例：复制super后允许调用角色分配；或用 `limit or default` 让0变无限；或Task已succeeded但on_success未退出就把新执行体放入。

## 6. 必需测试（断言点）

- `test_rbac.py`、`test_rbac_permissions.py`：33动作直接HTTP正反、内置/自定义/多角色、超管复制、管理目标/旧字段/自身/在用/CAS、明确审计范围及owner隔离。
- `test_rbac_concurrency.py`：Event控制双向撤权、User锁顺序、角色编辑/删除/分配竞争；四真实入口同时争最后作品名额最多1成功，失败无风格/Task半成品、token未消费/灵感及解析状态不变；claim提交/会话退出×owner cancel/stop、设置未知发布窗口、真实执行峰值/on_success未完。
- `test_user_quotas.py`：继承/0/正整数/不限、全部生命周期、超管不豁免、四入口原子拒绝、任务排队/全局硬顶/调低不强杀。
- `test_rbac_migrations.py`：0028代表性存量启用/禁用超管和普通账号，以及作品/章节/私人AI密文子行逐字段不变、FK完整、角色ID不复用、守护降级；create_all不能冒充Alembic。
- 前端auth/privateCaches/API/controller、`manualReadOnly.test.ts`、`AdminRolesPage.test.ts`、`AppLayout.test.ts`：真实异步prepare/submit回调、迟到logout/me/401、撤写保读/撤读清数据、关联子表单零API调用、375px卡片；角色名称100/说明500需保持前后端一致。本轮卡片测试覆盖布局/动作/空态/非超管，不能据此宣称已自动覆盖表单长度。
- 浏览器另验直达/刷新、两会话撤权+故障刷新、真实Meta-S、配额失败草稿、取消202到真实终态、移动明暗和网络owner边界。SQLite/Event与Node/SSR不替代真实PostgreSQL/浏览器；未运行必须在报告明示。

## 7. 错误写法 vs 正确写法

```python
# 错误：读时角色有效不代表等待资源锁后仍能写。
if user.is_admin:
    await lock_project()
    await save()

# 正确：helper内部在跨用户时一次获取actor+owner升序屏障，重验原token；资源锁在后。
# 不要提前单独require锁actor，否则owner.id更小时破坏升序，也会误拒普通owner编辑。
project = await get_manageable_project(db, actor.id, pid, lock=True, permission="projects.edit")
```

```typescript
// 错误：撤读后reload失败仍展示旧管理数据，或旧logout无条件导航。
// 正确：读取能力决定数据实例；导航前重查真实登录态。
<Browse key={`${user?.id}:${can(user, 'workshop.read')}`} />
await logout()
if (useAuthStore.getState().status === 'anonymous') navigate('/login')
```

边界：旧SQLite User整数ID+JWT sub/ver实体复用风险不在本迁移修复范围，authorization_revision不能被描述为旧JWT隔离机制。
