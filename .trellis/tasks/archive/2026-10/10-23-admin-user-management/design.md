# 技术设计：管理员功能

## 数据模型（迁移 0003）

> 并发约束：任务 `10-23-ai-config-model-params` 已占用迁移 `0002`（down_revision `0001`）并在修改 `backend/app/models/entities.py`。本任务迁移编号为 `0003`，`down_revision` 取实现时 `alembic heads` 的唯一 head（预期为 `0002`），不得再造平行分支。

`users` 新增：

| 列 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `is_admin` | bool, not null | `false` | 管理员 |
| `is_active` | bool, not null | `true` | 是否启用 |
| `token_version` | int, not null | `0` | 会话版本号，递增即作废旧令牌 |

使用 `server_default` 保证存量行回填；downgrade 删除三列。

## 会话失效

- `create_access_token(user_id, token_version, ...)` 在 payload 写入 `ver`。
- `decode_access_token` 返回 `(user_id, ver)`；缺少 `ver` 的旧令牌按 `0` 处理，与回填默认值一致，因此升级不会把在线用户踢下线。
- `get_current_user`：用户不存在、`ver != user.token_version` → 401「未登录或登录已过期」；`is_active == False` → 401（前端统一跳转登录页）。
- 递增 `token_version` 的操作：管理员重置密码、禁用、用户自助改密。自助改密成功后为当前请求重新签发 Cookie，当前会话保持登录。
- 登录时 `is_active == False` → 403「账号已被禁用，请联系管理员」。

## 管理员引导

- Settings 新增可选 `admin_username`、`admin_password`；只设一个即启动失败（fail-fast）；格式校验复用 `Credentials` 规则。
- 在 FastAPI lifespan 启动阶段执行 `bootstrap_admin(sessionmaker, settings)`：不存在则创建（is_admin=True），存在则置 `is_admin=True, is_active=True`，不改密码。日志只记录用户名，不记录密码。
- Docker 入口已先执行 `alembic upgrade head`，lifespan 执行时表结构已就绪。
- compose 与 `.env.example` 透传两个变量；README 说明「已存在时不覆盖密码」，改密请走界面。

## 接口（/api/v1）

- `GET /auth/me` → `UserOut` 增加 `is_admin`
- `POST /auth/change-password` `{old_password, new_password}` → 204；旧密码错误 400「原密码不正确」
- 依赖 `AdminUser`：基于 `CurrentUser`，非管理员 403「需要管理员权限」
- `GET /admin/users?q=&page=1&page_size=20` → `{items: AdminUserOut[], total}`；`AdminUserOut = {id, username, is_admin, is_active, created_at, project_count}`
- `POST /admin/users` `{username, password, is_admin=false}` → 201 `AdminUserOut`；用户名/密码校验复用 `Credentials`；重复 409「用户名已存在」（与注册共用 IntegrityError 兜底）；不检查 `REGISTRATION_ENABLED`
- `PATCH /admin/users/{id}` `{is_admin?, is_active?}` → `AdminUserOut`
- `POST /admin/users/{id}/reset-password` `{new_password}` → 204
- `DELETE /admin/users/{id}` → 204
- 不存在的用户 → 404

## 护栏（service 层统一校验，返回 400/409 中文提示）

- 目标为自己：禁止 `is_active=false`、`is_admin=false`、删除 → 400「不能对自己执行该操作」
- 操作后「启用中的管理员」数量为 0 → 409「至少需要保留一名启用中的管理员」
- 并发下两名管理员互相降级：校验与更新放在同一事务，PostgreSQL 下对管理员行 `SELECT ... FOR UPDATE`（SQLite 测试环境无行锁，但单连接串行，不影响正确性）

## 前端

- `types.ts`：`UserOut.is_admin`、`AdminUserOut`、分页结构
- `src/api/admin.ts`、`auth.changePassword`
- 顶栏用户菜单：「修改密码」弹窗（所有用户）、「用户管理」入口（仅管理员）
- `pages/AdminUsersPage.tsx`：AntD Table + 搜索 + 分页；表头「新建用户」按钮（弹窗：用户名、初始密码输入两次、「设为管理员」勾选）；行操作：重置密码（弹窗输入两次）、禁用/启用、设为/取消管理员、删除（Modal 内输入用户名才可确认）；对自己的行禁用危险操作
- 路由 `/admin/users` 用 `RequireAdmin` 守卫，非管理员重定向首页

## 权衡

- 令牌版本号 vs 服务端 Session 表：版本号只需一列且每次请求本来就会查用户，零额外查询；代价是无法单独作废某一个会话（MVP 可接受）。
- 环境变量引导不覆盖密码：避免 `.env` 中的旧密码在每次重启时把管理员已修改的密码改回去；代价是忘记管理员密码时需另一名管理员重置，或临时换一个 `ADMIN_USERNAME` 引导新管理员。
- 用户创建逻辑抽成 `auth_service.create_user(db, creds, *, is_admin)`，注册与管理员创建共用（重名检查、bcrypt、IntegrityError→409），避免两份实现漂移；注册开关只在注册入口检查。
- 禁用用户在 `get_current_user` 返回 401 而非 403：前端已有「401 → 清空登录态并跳转登录页」逻辑，可直接复用。

## 回滚

- `alembic downgrade -1` 删除新增三列；回滚后旧代码签发的令牌不含 `ver`，同样兼容。
