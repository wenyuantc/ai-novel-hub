# 实施切片协作契约

用户于 2026-10-30 明确回复“批准”。任务已激活为 in_progress，分支 codex/openapi-api-keys。本文件仅冻结代理之间的命名与传输字段，不改变已批准的产品范围。

## 管理 API（后端基座提供，前端消费）

- 基址 /api/v1，路由 /api-keys。
- `GET /api-keys` → `ApiKeyOut[]`。
- `POST /api-keys` → 201 `ApiKeyCreateOut`，在 ApiKeyOut 基础上增加 `key: string`。
- `PATCH /api-keys/{id}` → ApiKeyOut。
- `POST /api-keys/{id}/reveal` → `{key: string}`。
- `POST /api-keys/{id}/revoke` → 204。
- ApiKeyOut：`id, name, key_masked, all_projects, project_ids, status, expires_at, revoked_at, last_used_at, created_at, updated_at`。
- status 为 active / expired / revoked；所有时间是 UTC ISO 字符串，其中 expires_at / revoked_at / last_used_at 可空。
- 创建输入：name、all_projects（默认 false）、project_ids（默认 []）、expires_at（省略默认 90 天，null 不过期）。PATCH 字段均可省略，expires_at=null 代表不过期。
- 同时 all_projects=true 和非空 project_ids 时，服务清空无意义的显式授权，输出 project_ids=[]；指定模式至少一部小说，所有 ID 必须归属本人。

## 开放读取依赖（后端基座提供，开放读取切片消费）

- `app/core/open_api_deps.py` 导出 `OpenApiUser`（Annotated dependency）。
- 主体类型 `ApiKeyPrincipal`：`key_id, name, user_id, username, all_projects, project_ids, expires_at`；project_ids 可用 tuple[int, ...] 或 frozenset[int]，读取代理只进行 membership 和遍历。
- 该类型由 `app/services/api_key_service.py` 导出，避免模型凭据传到路由。
- `api_key_service.get_authorized_project(db, principal, project_id)` → Project，验证 owner 和 Key 授权，非法统一404。
- Header 仅 X-API-Key；请求额度 / last_used_at 在开放依赖中处理。读取切片不重复计算或提交额度。
- 开放读取切片负责 `backend/app/api/open/`、开放 schemas/services、main 注册和专属 docs；基座不改 main.py，避免共享文件冲突。

## 所有权

- 基座：backend 密钥模型、迁移、管理路由及 api/v1 注册、独立依赖、配置 / env / compose、备份排除及对应测试。
- 开放读取：backend 新开放 API/schema/service/main 与读取文档测试。
- 前端：frontend 全部本任务改动，独占 types.ts/router.tsx/StudioNavigation.tsx。
- 主会话：任务文档、README、规范、集成 / 提交 / 收尾。
- 未跟踪 pelican-bicycle.html 不属于本任务。

契约若需内部命名调整，先消息告知其他代理和主会话；不得悄悄增加权限或改变产品行为。
