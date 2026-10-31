# 管理员用户与会话失效

> 扩展自 [认证与密钥场景](./quality-guidelines.md#场景认证与密钥环境契约)。
> 二者不一致时，以下内容以本文件为准：JWT payload（`ver`）、`get_current_user` 校验、已禁用用户的登录。

平台签发的 API Key 使用独立的 X-API-Key 认证，与 token_version 会话失效机制分离；禁用账号同样阻止其 Key 使用。密钥的再次查看、授权、撤销及开放接口见 [平台 API Key 与开放读取](./open-api.md)。

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
async def create_user(db, creds: Credentials, *, is_admin: bool = False) -> User   # 原包装提交；注册/bootstrap保持旧行为
async def change_password(db, user: User, data: ChangePasswordRequest) -> User
async def bootstrap_admin(sessionmaker, settings: Settings) -> None              # 在 main.py 的 lifespan 中调用

# backend/app/services/admin_service.py
list_users(db, *, q, page, page_size) -> AdminUserList
create_user(db, actor, data) / update_user(db, actor, user_id, data) / reset_password(db, actor, user_id, data) / delete_user(db, actor, user_id)
```

### 3. 契约

- 管理员用户写入接入[统一操作审计](./admin-audit.md)：创建/重置密码同样传真实 actor；业务与成功审计同事务。创建走 create_user_uncommitted，由审计边界提交；普通注册/bootstrap 的 create_user 包装不变，密码/哈希不进入审计。

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
| `POST /admin/users` | `{username, password, is_admin=false}` | 201 `AdminUserOut` |
| `PATCH /admin/users/{id}` | `{is_admin?, is_active?}` | `AdminUserOut` |
| `POST /admin/users/{id}/reset-password` | `{new_password}` | 204 |
| `DELETE /admin/users/{id}` | — | 204（项目/章节/AI 配置通过外键 CASCADE 删除） |

  `AdminUserOut = {id, username, is_admin, is_active, created_at, project_count}`。用户管理接口只返回
  账号信息与计数；管理员另可通过[作品管理](./admin-projects.md)维护他人的小说内容。
  这不授予所有者模型凭据、平台密钥、私有资源或后台任务的额外访问权限。

- **管理员的其他权限**（`10-23-skills-mcp`，见 [skills-mcp.md](./skills-mcp.md)）：
  - **全局 Skill**：`skills.owner_user_id` 为空的 Skill 只有 `is_admin` 用户可以新建（`POST /skills` 或导入时 `scope=global`）、修改、删除、
    `PUT /references` 与 `POST /{id}/restore-builtin`；普通用户可见但写操作 403（文案提示复制为自己的副本），他人私有 Skill 对管理员同样 404——
    管理员**不能**读取其他用户的私有 Skill。权限按当前请求的 `user.is_admin` 判断，降级后立即失去写权限，不需要 `token_version` 递增。
  - **`MCP_ENABLED`**（`Settings.mcp_enabled: bool = True`）：部署级开关，不是用户级。关闭后 `/api/v1/mcp-plugins/*` 全部 403 `管理员已关闭 MCP`，
    智能体不再暴露任何 MCP 工具（运行中途关闭时，待处理的调用返回「工具不存在或已变更」）；已保存的插件保留。`GET /auth/config` 返回
    `{registration_enabled, mcp_enabled}`，前端据此隐藏入口。与 `REGISTRATION_ENABLED` 一样只经环境变量设置，目前没有写进 `.env.example` /
    `docker-compose.yml`，需要时手动加。
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
  操作者本身始终是剩余的启用中管理员）；删除级联；`test_change_password_keeps_current_session`。
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
