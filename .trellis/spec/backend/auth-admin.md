# 管理员用户与会话失效

> 扩展自 [认证与密钥场景](./quality-guidelines.md#场景认证与密钥环境契约)。
> 二者不一致时，以下内容以本文件为准：JWT payload（`ver`）、`get_current_user` 校验、已禁用用户的登录。

平台签发的 API Key 使用独立的 X-API-Key 认证，与 token_version 会话失效机制分离；禁用账号同样阻止其 Key 使用。密钥的再次查看、授权、撤销及开放接口见 [平台 API Key 与开放读取](./open-api.md)。

管理员全站任务权限仅通过独立[任务监控](./admin-task-monitoring.md)入口提供安全元数据及取消请求受理；每请求复核AdminUser，写前以真实actor/owner用户锁复核启用/角色/token版本。普通/tasks、output/stream/retry/resume/finish仍严格owner隔离，不能通过冒用owner复用权限；无作品但尚有pending取消意图的用户也不能被删号CASCADE掉恢复依据。

---

## 场景：管理员角色、用户管理与 token 版本控制

### 1. 范围 / 触发条件

管理员引导创建（bootstrap）、`/admin/users` 端点、用户自助修改密码，以及通过 `users.token_version`
使会话失效。触发条件：修改 `app/core/{security,deps,config}.py`、
`app/services/{auth_service,admin_service}.py`、`app/api/v1/{auth,admin}.py`、`users` 表，
或任何会改变用户密码 / 启用状态的改动。

### 2. 签名

```python
# backend/app/models/entities.py — User（migration 0003_user_admin_fields，down_revision '0002'）
is_admin: bool       # server_default false
is_active: bool      # server_default true
token_version: int   # server_default 0

# backend/app/core/security.py
def create_access_token(user_id: int, token_version: int, secret: str, expire_minutes: int) -> str
def decode_access_token(token: str, secret: str) -> tuple[int, int] | None   # (user_id, ver)

# backend/app/core/deps.py
async def get_current_user(...) -> User          # CurrentUser
async def get_admin_user(user: CurrentUser) -> User   # AdminUser

# backend/app/core/config.py
admin_username: str | None = None   # "" 视为 None
admin_password: str | None = None
@property admin_credentials -> Credentials | None

# backend/app/services/auth_service.py
async def create_user_uncommitted(db, creds: Credentials, *, is_admin: bool = False) -> User  # 创建/flush，不提交
async def create_user(db, creds: Credentials, *, is_admin: bool = False) -> User   # 保留bootstrap等非登录创建的提交包装；register另走登录记录边界
async def change_password(db, user: User, data: ChangePasswordRequest) -> User
async def bootstrap_admin(sessionmaker, settings: Settings) -> None              # 在 main.py 的 lifespan 中调用

# backend/app/services/admin_service.py
list_users(db, *, q, page, page_size) -> AdminUserList
create_user(db, actor, data) / update_user(db, actor, user_id, data) / reset_password(db, actor, user_id, data) / delete_user(db, actor, user_id)
```

### 3. 契约

- 管理员用户写入接入[统一操作审计](./admin-audit.md)：创建/重置密码同样传真实 actor；业务与成功审计同事务。创建走 create_user_uncommitted，由审计边界提交；bootstrap 保留 create_user 提交包装，普通注册另走创建原语与成功登录记录的同一提交边界（见下方用户详情场景）。密码/哈希不进入审计。

- JWT payload：`{"sub", "ver", "iat", "exp"}`。缺少 `ver`（0003 之前签发的 token）→ 视为 `0`，
  与回填的默认值一致，因此升级不会让任何人掉线。`ver` 为非 int 或 bool → 无效。
- `get_current_user`：用户不存在、`ver != user.token_version` 或 `not user.is_active`
  → 401 `未登录或登录已过期`（前端已将 401 映射为清除认证状态 + 跳转登录页）。
- 以下操作会递增 `token_version`（在 SQL 中递增，见 §7）：管理员重置密码、管理员 PATCH `is_active: true→false`、
  用户自助修改密码。以下操作不递增：角色变更、重新启用。
- 环境变量：`ADMIN_USERNAME` / `ADMIN_PASSWORD` 可选，必须同时设置，按 `Credentials` 规则校验；
  通过 `docker-compose.yml`、`.env.example`、`backend/.env.example` 传入。
  Settings 使用 `hide_input_in_errors=True`，确保校验错误绝不回显密钥。
- Bootstrap（在 lifespan 中、`alembic upgrade head` 之后执行）：用户不存在 → 以 `is_admin=True` 创建；
  已存在 → 设置 `is_admin=True, is_active=True`，**绝不改动密码**；并发创建
  （IntegrityError）→ 提升已存在的那一行。日志只记录用户名。忽略 `REGISTRATION_ENABLED`。
- 端点（`/api/v1`）：

| 方法与路径 | Body / query | 成功响应 |
|---|---|---|
| `GET /auth/me`（login/register 同样） | — | `UserOut {id, username, is_admin, created_at}` |
| `POST /auth/change-password` | `{old_password（1–200 字符）, new_password（Password 规则）}` | 204 + 为当前会话下发新的 `Set-Cookie` |
| `GET /admin/users` | `q`（≤64，大小写不敏感的子串匹配，`%`/`_` 按字面量处理）、`page ≥1`、`page_size 1–100`（默认 20） | `{items: AdminUserOut[], total}`，按 id 排序 |
| `GET /admin/users/{id}` | — | `AdminUserDetail`：原安全账号字段、last_login_at、三组基础资源用量，见下方场景 |
| `POST /admin/users` | `{username, password, is_admin=false}` | 201 `AdminUserOut` |
| `PATCH /admin/users/{id}` | `{is_admin?, is_active?}` | `AdminUserOut` |
| `POST /admin/users/{id}/reset-password` | `{new_password}` | 204 |
| `DELETE /admin/users/{id}` | — | 名下无任何作品时204，清理账号及其用户级私有配置；有作品（含回收站/待清理）时409 |

  `AdminUserOut = {id, username, is_admin, is_active, created_at, project_count}`。列表/写入响应保持这些字段，详情另提供最近登录与分组统计。用户管理接口只返回
  账号信息与计数；管理员另可通过[作品管理](./admin-projects.md)维护他人的小说内容。
  这不授予所有者模型凭据、平台密钥、私有资源或后台任务的额外访问权限。

  `project_count` 包含全部未物理删除作品，不能复用正常目录的生命周期过滤。删号先锁目标User并检查全部Project；非零返回 `code=user_has_projects`（通常带安全 `project_count`），可先禁用账号。0024起 Project→User 为RESTRICT，兜底并发/漏查，不能再依靠删号级联作品。恢复/彻底删除作品见[回收站契约](./recycle-bin.md)。

- **管理员的其他权限**（`10-23-skills-mcp`，见 [skills-mcp.md](./skills-mcp.md)）：
  - **全局 Skill**：`skills.owner_user_id` 为空的 Skill 只有 `is_admin` 用户可以新建（`POST /skills` 或导入时 `scope=global`）、修改、删除、
    `PUT /references` 与 `POST /{id}/restore-builtin`；普通用户可见但写操作 403（文案提示复制为自己的副本），他人私有 Skill 对管理员同样 404——
    管理员**不能**读取其他用户的私有 Skill。权限按当前请求的 `user.is_admin` 判断，降级后立即失去写权限，不需要 `token_version` 递增。
  - **MCP有效开关**（部署基线 `MCP_ENABLED`，不是用户级）：管理员可通过[系统设置](./system-settings.md)持久覆盖，后台覆盖优先；关闭后 `/api/v1/mcp-plugins/*` 全部 403 `管理员已关闭 MCP`，智能体不暴露MCP工具，连接/排队后尚未发出的调用也受约束；已发调用可结束、插件保留。`GET /auth/config` no-store返回 `{registration_enabled, mcp_enabled}`，前端可见时30秒/回前台同步。注册开关同样可后台覆盖；未覆盖沿用环境/.env/代码默认，Compose已转发两变量。
  - 删除用户时其私有 Skill、MCP 插件经外键 CASCADE 删除；全局 Skill 不属于任何用户，不受影响。

### 4. 校验与错误矩阵

| 条件 | 结果 |
|---|---|
| `ADMIN_USERNAME`/`ADMIN_PASSWORD` 只设置了其一，或格式无效 | 启动时抛出 `ValidationError`，不回显密码 |
| 密码正确但 `is_active=false` 时登录 | 403 `账号已被禁用，请联系管理员`（密码错误仍为 401，不泄露账号状态） |
| 任意需认证请求中 `ver` 过期 / 用户已禁用 | 401 `未登录或登录已过期` |
| 非管理员调用 `/admin/*` | 403 `需要管理员权限` |
| 非管理员写全局 Skill / 恢复内置 Skill | 403 |
| `MCP_ENABLED=false` 时调用 `/mcp-plugins/*` | 403 `管理员已关闭 MCP` |
| 目标用户不存在 | 404 `用户不存在` |
| 管理员创建时用户名重复 | 409 `用户名已存在` |
| 操作者禁用 / 降级 / 删除自己 | 400 `不能对自己执行该操作`（对自己的无变化 PATCH 允许） |
| 操作会导致启用中的管理员为零 | 409 `至少需要保留一名启用中的管理员` |
| 删号时仍有作品，包括回收站或过期待清理作品 | 409 `user_has_projects`，账号/作品保留，可先禁用 |
| 修改密码时原密码错误 | 400 `原密码不正确` |
| `page`/`page_size`/`q` 越界、密码无效 | 422 |

### 5. 正例 / 基准 / 反例

- 正例：管理员重置 Bob 的密码 → Bob 的旧 cookie 在下一次请求时得到 401；新密码可以登录。
- 基准：用户修改自己的密码 → 当前标签页保持登录（cookie 以新的 `ver` 重新签发），其他浏览器得到 401。
- 反例：在 `.env` 中修改 `ADMIN_PASSWORD` 后重启并期望密码随之改变——bootstrap 从不覆盖密码；请使用 UI 修改。

### 6. 必需的测试

- `backend/tests/test_admin.py` —— bootstrap（创建、幂等、不覆盖密码、提升已有用户、
  成对校验、未设置时不做任何事、lifespan 会调用它）；403/401/404；列表分页/搜索/project_count；
  关闭注册时仍可创建 + 登录 + is_admin + 409；重置 → 旧会话 401；禁用 → 401 且
  登录 403；启用 → 可登录；`test_legacy_token_without_ver_still_valid`；`test_token_with_bad_ver_rejected`；
  自身防护规则；最后一名管理员防护（service 层测试——通过 HTTP 顺序调用无法触发，因为
  操作者本身始终是剩余的启用中管理员）；有作品删号拒绝、明确purge后无作品才可删号及用户私有资源级联；`test_change_password_keeps_current_session`。
- `backend/tests/test_migrations.py::test_0003_backfills_existing_users` —— 来自 0002 的行得到
  `is_admin=false, is_active=true, token_version=0`；`downgrade -1` 回到 0002。

### 7. 错误写法 vs 正确写法

错误——在 Python 中先读后改再写，重置与禁用并发时会丢失一次递增：

```python
user.token_version += 1
```

正确——在 SQL 中递增（`admin_service.py`、`auth_service.change_password`），然后 commit + refresh，
再重新签发 cookie：

```python
user.token_version = User.token_version + 1
```

错误——用普通 SELECT 检查"是否还有其他管理员"，会让两名管理员并发地互相降级。

正确（`admin_service._ensure_other_active_admin`）——在与更新相同的事务中锁定启用中的管理员集合
（PostgreSQL 在等待锁后会重新求值；SQLite 会忽略该锁，但本身就是串行执行）：

```python
await db.scalars(select(User.id).where(User.is_admin.is_(True), User.is_active.is_(True)).with_for_update())
```


---

## 场景：管理员用户详情与最近成功登录

### 1. 范围 / 触发条件

用户详情、最近登录、用户所属作品统计与共享账号动作。修改 User、auth_service 的注册/认证、管理员用户 GET、AdminUserDetailsPage 或 components/adminUsers 时适用；原角色、会话版本、审计与作品管理权限仍遵守上方契约。

### 2. 签名

```python
# models/entities.py，migration 0028_user_last_login（父0027）
last_login_at: Mapped[datetime | None]  # DateTime(timezone=True)，nullable，无历史回填

# services/auth_service.py
async def _record_successful_login(db: AsyncSession, user: User) -> User
class AuthenticationPersistenceError(AppError):
    status_code = 500

# services/admin_service.py；api/v1/admin.py 使用 AdminUser
async def get_user_detail(db: AsyncSession, user_id: int) -> AdminUserDetail
# GET /api/v1/admin/users/{user_id}
```

`AdminUserDetail = AdminUserOut + {last_login_at: datetime|null, usage: AdminUserUsage}`。`usage` 包含 active / recycled / deleting，每组都是 `{project_count, chapter_count, total_words}` 非负整数。前端 types.ts 精确镜像，adminApi.getUser(id, signal?) 走统一 Cookie 客户端。

### 3. 契约

**登录与事务**

- 只有成功密码登录和注册后的自动登录记录时间；管理员创建/bootstrap、日常请求、改密续签、API Key 使用、失败/禁用登录不更新。
- register 调用 create_user_uncommitted，再通过 _record_successful_login 提交；管理员创建仍由 AuditOperation 提交，bootstrap 仍用 create_user。不能在共用创建函数或 _set_auth_cookie 中通用记录，否则会误算未登录账号或续签。
- bcrypt 后的条件 UPDATE 同时核对用户ID、is_active、已验 password_hash、token_version；SQL CASE 保持 last_login_at 不倒退，显式 updated_at=User.updated_at，不把登录算作账号资料编辑。
- UPDATE RETURNING(User) 的已验证快照提交后直接返回，不再 refresh 成并发重置后的新版本。并发禁用/重置若发生在该事务之后，只使旧快照 Cookie 失效，不能让旧密码签出新版本。
- 记录持久化成功才由原路由签 Cookie。注册 flush、登录写入/commit 故障回滚，返回固定安全500，不发成功Cookie；只记录异常类型，不让含SQL/哈希参数的原异常进入全局堆栈日志。
- 0028 原位 ADD/DROP COLUMN，不重建 users 父表。存量 null 不以注册/更新时间回填，前端明确“暂无记录”；UTC 经 formatTime 展示。降级只移除新增列，会丢失登录记录，不误删子行。

**单快照资源统计**

详情 service 只选安全账号列，使用 chapter_stats_subquery 按作品预聚合章节，再对目标用户项目做条件聚合，与 User 外连接；一次 SELECT 返回账号与用量。禁止 ORM 整对象序列化、按项目逐条查、分页后相加或直接按章join计作品。

| 分组 | 精确条件 | 展示 |
|---|---|---|
| active | deleted_at IS NULL 且 deletion_started_at IS NULL | 正常作品 |
| recycled | deleted_at IS NOT NULL | 回收站，含到期待清理 |
| deleting | deleted_at IS NULL 且 deletion_started_at IS NOT NULL | 非零时单独提示删除中，不提供正常编辑链接 |

三组作品数之和等于顶层 project_count；顶层继续用于全部现存作品的删号保护。空作品算1部/0章/0字，零资源均0；章节数包含空章，字数仅当前 Chapter.word_count，不叠加历史版本/设定/日志。不计算字节、Token、费用或其他资源用量。

**页面与动作**

- `/admin/users` 与 `/admin/users/:userId` 同在 RequireAdmin 下懒加载；用户名与详情链接进入详情，菜单仍选用户管理。parseAdminUserId 仅接收正十进制安全整数，拒绝指数、小数和越界ID。
- 内部详情以 actorId:targetId 为key，加载/失败/404不展示旧用户和危险动作。所属正常作品始终请求 owner_user_id=目标ID，分页不影响总统计；正常目录/回收站深链带同一owner，两个管理目录按当前owner key重建以响应同路由查询串切换。
- 账号动作共用页级 AdminUserActionsController、useAdminUserActions、AdminUserActionButtons、声明式 AdminUserActionDialog；新建仍留列表。不创建全局控制器/通用CRUD框架，不使用全局destroyAll。
- activate/deactivate 支持StrictMode；失效清目标并递增gate。提交前、异步表单校验后、响应交付前均核对真实actor、权限、目标对象及代次；busy同步生效防双击。卸载/退出/换目标后旧响应不toast、不刷新、不导航；声明式Modal自然撤下。
- 沿用rowActions、全部project_count、自操作/最后管理员/待收尾任务保护和严格用户名确认。409计数只经userProjectCountFromError收窄；普通错误保留可重试但不自动重发。列表删末项保留回退，详情删号成功replace返回列表。
- 作品进入原人工管理工作区，保留真实owner提示和actor身份；不开放他人模型/平台Key/私人资源/AI任务输出/阅读进度。

### 4. 校验与错误矩阵

| 条件 | 结果 |
|---|---|
| 未登录/禁用/已撤销会话 | 401，统一前端清会话 |
| 当前非管理员读详情 | 403 需要管理员权限；不因目标存在与否泄露详情 |
| 目标用户不存在 | 404 用户不存在 |
| 前端ID不是正十进制安全整数 | 无效用户ID提示，不发详情请求；后端非int路径仍为标准422 |
| 密码错误/不存在，或验密后凭据/版本失配 | 401 用户名或密码错误，时间不更新、无成功Cookie |
| 密码正确但账号禁用（包括写前复核发现） | 403 账号已被禁用，请联系管理员 |
| 认证持久化失败 | 500 登录信息保存失败，请稍后重试；回滚、无成功Cookie/敏感SQL日志 |
| 详情读取失败 | 显示错误/重试，撤下旧账号及危险操作；404有专用目标不存在说明 |
| 有作品/待收尾任务删号、最后管理员、自操作 | 保持上方原400/409与code，不因详情入口绕过 |

### 5. 正例 / 基准 / 反例

- 正例：用户有正常、回收站和删除中作品，详情分别统计并解释全部作品数；从正常作品进入管理工作区仍显示该用户名。
- 基准：管理员创建零资源账号，详情显示0和“暂无记录”；该用户首次真实登录后才有时间，普通页面访问不刷新。
- 反例：将总数减正常数当回收站数；将历史created_at当最近登录；commit后refresh新token_version并给已验旧密码签Cookie。

### 6. 必需测试

- `backend/tests/test_admin_user_details.py`：权限即时复核/安全字段/一次SELECT；多owner/空作品/空章/当前字数/三分组和过期保留；真实软删→恢复→purge；注册/登录唯一更新入口；验密中禁用/重置/改密、commit后重置仍旧版本；真实并发迟到请求不倒退；flush/commit失败无成功Cookie及哈希日志。
- `backend/tests/test_migrations.py::test_0028_nullable_login_time_preserves_users_and_children`：存量null、升级/降级保留用户/作品/章节/私有配置及外键完整。
- `frontend/src/components/adminUsers/adminUserActionsController.test.ts`：实际提交API、异步校验/双击/StrictMode/换目标/权限失效/旧响应/安全409；`adminUserDetailsUI.test.ts`、`utils/adminUserDetails.test.ts`、`api/admin.test.ts`覆盖展示、路由、ID、固定owner与Cookie/错误。
- 浏览器：直达/刷新、正常作品与回收站owner深链、账号确认/取消/改密/删号导航、离页和真实401撤下Modal、零资源/null/失败重试/404、375px明暗主题与管理他人内容的网络隔离。

### 7. 错误写法 vs 正确写法

错误：

```python
# 无条件覆盖时间，并把并发重置的新会话版本签给只验证过旧密码的请求。
user.last_login_at = now
await db.commit()
await db.refresh(user)
return user
```

正确（见 auth_service._record_successful_login）：

```python
# 条件同时核对已验哈希/版本/启用状态；SQL CASE 防倒退。
# UPDATE ... RETURNING(User) 得到认证快照后提交，禁止事后refresh升级版本。
authenticated = result.scalar_one_or_none()
if authenticated is None:
    await db.rollback()
    current = (await db.execute(
        select(User.password_hash, User.is_active).where(User.id == user_id)
    )).one_or_none()
    if current is not None and current.password_hash == password_hash and not current.is_active:
        raise ForbiddenError("账号已被禁用，请联系管理员")
    raise UnauthorizedError("用户名或密码错误")
await db.commit()
return authenticated
```
