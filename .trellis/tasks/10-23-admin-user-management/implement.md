# 实施计划：管理员功能

## 前置条件（开工闸门）

- 并发任务 `10-23-ai-config-model-params`（会话「模型配置功能增强」）正在修改 `backend/app/models/entities.py`、`backend/app/schemas/ai_config.py`、providers，并新增迁移 `0002`。**本任务的后端改动须在该任务提交后才开始**：开工前确认 `git status` 中这些文件已无他人未提交改动、`uv run alembic heads` 只有一个 head。
- 若届时仍未提交，先只做不重叠的部分（前端管理页骨架），后端等待。

## 执行顺序

后端（先完成并测试，前端依赖其 schema）
1. 模型新增 `is_admin / is_active / token_version` + 迁移 `0003`（down_revision 为当时唯一 head，含 downgrade）
2. JWT 加 `ver`；`get_current_user` 校验版本与启用状态；登录拒绝禁用用户
3. Settings 新增 `admin_username / admin_password`（成对校验）+ lifespan 引导
4. `POST /auth/change-password`；`UserOut.is_admin`
5. 抽出 `auth_service.create_user` 供注册与管理员创建共用；`AdminUser` 依赖 + `services/admin_service.py`（创建、列表/搜索/分页、PATCH、重置密码、删除、护栏）+ `api/v1/admin.py`
6. 测试：管理员创建用户（注册关闭时仍可创建、可登录、设为管理员、重名 409、普通用户 403）、迁移升降级、旧令牌兼容、版本失效、禁用即时生效、引导创建/幂等/不覆盖密码/成对校验、403、护栏（自己、最后一个管理员）、删除级联、自助改密保持当前会话

前端
7. 类型与 `api/admin.ts`、`changePassword`
8. 顶栏菜单：修改密码弹窗 + 管理入口；`RequireAdmin`；`AdminUsersPage`（含「新建用户」弹窗）
9. 删除二次确认（输入用户名）；对自己行禁用危险按钮

部署与文档
10. compose / `.env.example` / `backend/.env.example` 透传 `ADMIN_USERNAME` / `ADMIN_PASSWORD`；README 增「管理员」一节
11. 更新 `.trellis/spec/backend`（认证场景加 ver、引导、护栏矩阵）与 `.trellis/spec/frontend`（类型、页面）

## 验证命令

- `cd backend && uv run ruff check . && uv run pytest`
- `cd frontend && npm run lint && npm run build && npm test`
- Docker 冒烟（独立 compose 项目名）：设置 ADMIN_* 且 REGISTRATION_ENABLED=false 启动 → 管理员登录 → 管理员创建普通用户并用其登录 → 重置/禁用后原会话 401 → 重启验证密码未被覆盖

## 风险点与回滚

- 令牌兼容：缺少 `ver` 必须按 0 处理，否则升级会让所有在线用户掉线——需专项测试
- 最后一个管理员护栏的并发：同一事务内加锁校验
- 引导时机：lifespan 在测试的 ASGITransport 下不会自动触发，引导逻辑做成独立函数单测
- 回滚点：迁移 `0003` 可 downgrade；前端改动独立于既有页面
- 并发冲突：`entities.py` / `auth.py` 等与其他会话共享，提交时只暂存本任务改动的文件，不用 stash/reset 清场
