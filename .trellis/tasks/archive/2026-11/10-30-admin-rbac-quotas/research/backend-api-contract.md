# 后端 API 固定契约（2026-11-01）

本文件为本轮后端实现与前端协作的唯一字段约定。该契约已实现；最终验证与环境边界见../verification.md，单独本文件不代表PG已验证。

## 认证与账号

`RoleBrief = {id:number,name:string,system_key:string|null,is_builtin:boolean}`。

`UserOut` 保留 `id,username,is_admin,created_at`，增加 `is_super_admin:boolean,roles:RoleBrief[],effective_permissions:PermissionCode[],authorization_revision:number`。is_admin与is_super_admin同为唯一超管flag，细粒度判断只用后者/permissions。

`AdminUserOut` 保留 `id,username,is_admin,is_active,created_at,project_count`，增加 `is_super_admin:boolean,is_management_account:boolean,roles:RoleBrief[],authorization_revision:number`。管理目标不额外返回effective_permissions。详情继续增加last_login_at与原usage。

`AdminUserCreate={username,password}`；`AdminUserUpdate={is_active?:boolean}`。旧HTTP is_admin字段禁止（422）。内部bootstrap不受此HTTP字段限制。

## 权限与角色

`PermissionDefinition={code:PermissionCode,label:string,group:string,dangerous:boolean,requires:PermissionCode[]}`。

`RoleOut=RoleBrief+{description:string,permissions:PermissionCode[],revision:number,member_count:number,created_at:string,updated_at:string,authorization_revision:number}`。

- GET `/admin/permissions` -> `{items:PermissionDefinition[]}`。
- GET `/admin/roles?q=&page=1&page_size=20` -> `{items:RoleOut[],total:number,authorization_revision:number}`。
- GET `/admin/roles/{id}` -> RoleOut。
- POST `/admin/roles` body `{name,description?:string,permissions?:PermissionCode[],source_role_id?:number,source_revision?:number}` -> RoleOut（201）。复制时source两字段一起提供；permissions省略则复制允许委派目录，给出则使用所选列表。
- PATCH `/admin/roles/{id}` body `{authorization_revision,revision,name?:string,description?:string,permissions?:PermissionCode[]}` -> RoleOut。permissions整体替换。
- DELETE `/admin/roles/{id}?authorization_revision=&revision=` -> 204。
- PUT `/admin/users/{id}/roles` body `{authorization_revision:number,role_ids:number[]}` -> AdminUserOut。整体替换；空数组普通基线。

以上角色/目录端点仅super。四system_key为super_admin/content_admin/reviewer/user。角色标签/分配使用真实id，不用名称猜身份。33项PermissionCode以design.md第3节为准。变更的read依赖必须显式勾选，后端拒绝缺依赖列表；quota.read依赖users.read、quota.update依赖quota.read（具体代码为quotas.read/quotas.update）。

## 配额

`QuotaConfig = {mode:'inherit'} | {mode:'unlimited'} | {mode:'limited',value:number}`。limited严格非负整数（0..2147483647）；其他mode不发送value。

`QuotaItem={config:QuotaConfig,effective_limit:number|null,source:'user'|'system'|'default',used:number}`。effective_limit的null仅表示不限额，继承由config表达。used为全生命周期作品数/真正admitted后台占用。

`UserQuotaOut={user_id:number,username:string,is_management_account:boolean,revision:number,authorization_revision:number,global_task_limit:number,project_count:QuotaItem,task_concurrency:QuotaItem}`。

- GET `/admin/users/{id}/quotas` -> UserQuotaOut，需quotas.read。
- PATCH 同路径 body `{authorization_revision:number,revision:number,changes:{project_count?:QuotaConfig,task_concurrency?:QuotaConfig}}` -> UserQuotaOut，需quotas.update，changes不可空。管理目标修改额外要求super。

无quota行revision=0，两项inherit；项目inherit固定默认不限，任务inherit为system有效并发。用户无限也受global_task_limit约束。

## 错误与安全

- 403 `code=permission_denied`（可有required_permission），可触发能力刷新但不401登出/重放。
- 403 `code=management_account_protected`，不误作会话失效。
- 409 `code=stale_revision|role_in_use|builtin_role_readonly|quota_exceeded`。quota_exceeded仅安全metric/used/limit。
- 401原认证失效；422安全字段校验；404不存在。自身/最后super保持400/409。

管理响应no-store；API前缀均由统一客户端补`/api/v1`，Cookie/AbortSignal/错误处理不变。
