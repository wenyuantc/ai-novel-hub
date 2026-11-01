# 用户详情：登录时间与迁移调研

日期：2026-11-01。仅为规划证据，不代表已实施。

## 已确认的产品口径

- 成功密码登录及注册后的自动登录更新最近登录时间。
- 密码错误、不存在账号、账号禁用、普通已登录请求、改密续签不更新。
- 历史无记录显示“暂无记录”，不根据 `created_at` / `updated_at` 补造历史。
- 不记录最近活跃、IP、设备信息；不新增登录审计系统。

## 现存调用链与复用点

- `backend/app/api/v1/auth.py:32-43`：只有 register/login 两条路由在各自 service 成功返回后调用 `_set_auth_cookie`；`auth.py:58-64` 的改密也会签发 Cookie，因此不能把时间更新放进 `_set_auth_cookie`。
- `backend/app/services/auth_service.py:17-38`：`create_user_uncommitted` 做用户名唯一校验、异步线程 bcrypt、flush；`create_user` 是提交包装。管理员创建走前者并由审计边界提交，bootstrap 走后者（`auth_service.py:73-100`）。不能在这两个共用创建函数中默认标记“已登录”。
- `backend/app/services/auth_service.py:41-54`：register 检查注册开关后调用创建包装；authenticate 读取 User、异步验密，再检查启用状态，当前没有写入。
- `backend/app/api/v1/auth.py:13-23`：Cookie 从返回对象的 `token_version` 签发；签发属性和 JWT 格式不得因本任务更改。
- `backend/app/core/database.py:19-21`：session 使用 `expire_on_commit=False`，提交后可保留本次认证核验过的对象快照，不必刷新成另一次并发密码重置后的版本。
- `backend/app/services/api_key_service.py:200-238`：现有原子认证用条件 UPDATE、`CASE` 防时间倒退、显式 `updated_at=...`、`RETURNING`、提交后再进入业务。可参考这些局部模式，不复用 API Key 的认证逻辑或开放权限。
- `backend/app/models/base.py:25-30`：`TimestampMixin.updated_at` 有 SQL onupdate。单独写登录时间时应显式保持原 updated_at，避免把登录混同于账号配置编辑。
- `backend/app/schemas/admin.py:28-35`：AdminUserOut 仅安全账号元数据与全生命周期 project_count。详情应扩展专用响应，不把 User ORM / 密码哈希 / token_version 序列化给客户端。

## 并发风险及设计约束

1. bcrypt 在工作线程中执行，期间管理员可能禁用账号或重置密码。不能只依据最初读到的 `is_active` 无条件 UPDATE 登录时间。
2. 写入时至少同时核对目标 ID、启用状态、被验证的密码哈希和会话版本。失配不更新登录时间、不签发新会话；保留现有密码错误 401 和已正确验密的禁用账号 403 语义。
3. 正确验密后，不能在 commit 后刷新并用未经本次密码验证的新 `token_version` 签发 Cookie。使用受条件更新保护的返回快照；若后续禁用/重置先发生，新 Cookie 仍携带旧版本并被现有会话验证拒绝，不能“升级”为新版本绕过撤销。
4. 不使用“读时间 → Python max → 写回”处理并发。SQL 中将 last_login_at 保持为已存值和本次 UTC 时间的较大者，迟到写入不能覆盖更新的记录。
5. 注册自动登录在注册专用提交边界设置 last_login_at；管理员创建账号、bootstrap、改密不经过这个标记。
6. 持久化失败必须先回滚并使登录/注册请求失败，不能已下发成功 Cookie 再异步补记时间。

## 迁移与兼容

- 当前 head 是 `backend/alembic/versions/0027_system_settings.py`（revision 0027）。计划新增 0028，以实际实施时 head 为准。
- `users.last_login_at`：`DateTime(timezone=True)`、nullable、无默认回填，不新增查询索引。
- 参考 `backend/alembic/versions/0026_admin_task_monitoring.py:16-36` 使用原位 ADD/DROP COLUMN，避免 SQLite 重建有众多外键子表的 users 表；迁移测试必须验证子行和外键完整性。
- 普通 UserOut、JWT、原有管理员用户列表/写入响应不扩展私人或不必要字段；最近登录由专用详情响应提供。
- 前端格式遵循 `.trellis/spec/frontend/type-safety.md:113-123`：无时区后缀按 UTC 解析，使用 formatTime；null 单独显示“暂无记录”。

## 建议的回归证据

- `backend/tests/test_auth.py`：注册自动登录、成功登录推进、失败/禁用/访问/改密不推进，Cookie 属性不变。
- `backend/tests/test_admin.py`：管理员创建/bootstrap 不产生登录记录；重置/禁用的 token_version 规则保留。
- 新增并发控制测试：验密阻塞期间禁用/改密不成功记登录、不签发新版本 Cookie；迟到写入不倒退时间。
- `backend/tests/test_migrations.py`：0027 存量用户和子行 → 0028，last_login_at 全为 null；写入时间后 downgrade 回 0027，只移除新增列，原数据、子行、外键仍完整。
- 统计与详情 GET 不应触发登录时间变更，也不应触发任何真实模型请求。
