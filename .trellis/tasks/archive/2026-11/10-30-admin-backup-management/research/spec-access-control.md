# 备份任务角色与撤权规范摘录

本任务规划时的规范摘录；原规范为权威来源。源文件变化后按定位重新读取相关条款。

来源：`.trellis/spec/backend/rbac-quotas.md:48`，行 48–78；原文件 SHA-256：`e63801729464166778f53735c5ae23e1777d5edb0111b9a1652bda9b6e3f2465`。

### 身份、目录与目标保护

- **唯一超管事实为 `User.is_admin`**；`is_super_admin` 是其安全输出名。super_admin角色元数据由此投影；无显式成员的非超管投影普通用户。复制super只复制37项可委派动作，不复制身份、角色/授权管理权。
- 四内置角色只读可复制：super_admin全部37项；content_admin为projects三项、workshop八项、recycle_bin.read/restore共13项；reviewer为workshop.read/approve/reject/hide/unhide五项；user无后台项。任意角色都保留本人个人能力。
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


来源：`.trellis/spec/backend/rbac-quotas.md:96`，行 96–104；原文件 SHA-256：`e63801729464166778f53735c5ae23e1777d5edb0111b9a1652bda9b6e3f2465`。

### 前端读取、异步失效和人工只读

- `RequirePermission` 对每管理路由声明具体read；角色路由superOnly。菜单/移动/账户/面包屑同能力，不固定跳无权dashboard。目录之间独立检查users/projects/recycle/audit读权。
- 私有授权刷新独立于init与公开配置；路由进入、可见轮询/回前台及权限403触发。403不登出、不自动重发业务；401才清会话。epoch+请求代次阻止旧me/401/logout覆盖新login，旧logout菜单回调也须重验当前anonymous才导航。
- `useAuthorizationScope`/cleanup及页级controller管理本页Modal，不destroyAll。撤写只关失效动作、保留合法读取；撤读移除相应数据。仅reload不充分：工坊Browse按actor+workshop.read重挂，刷新500时也不得留下隐藏条目/匿名真名；个人草稿不因无关revision被清空。
- `projectAccess` 派生canRead/canEdit/canRecycle/canUseAI。只读覆盖正文/标题/选角/Meta-S、版本恢复、阶段编辑器（即使在Form外）、成员/职业卸下、伏笔及移动章节操作。事件提交前再查实时canEdit，不能只隐藏主按钮。
- owner-only AI/模型/Key/私人Skill/MCP/任务输出/生成日志/备份/阅读进度不扩权。管理他人时不得挂载这些自动请求/订阅；本人功能保留。
- 角色桌面Table、375px卡片共享动作规则；内置不可编辑删除、在用不可删。配额显示三态/来源/当前占用/全站顶，0不经truthy转为不限；409/未知结果保留草稿并先刷新核实。
