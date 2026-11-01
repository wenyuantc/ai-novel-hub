# 管理员用户详情：技术设计

状态：最终规划已获用户“开始”批准，任务已激活实施。日期：2026-11-01。

## 1. 边界与整体方案

保持 FastAPI 路由→service→SQLAlchemy、React 页面→api/client 的既有分层。不新增依赖、缓存、定时统计或通用管理框架。

两条纵向链路：

1. 注册/登录 service 在成功的认证事务中持久化 `users.last_login_at`，再由原 auth 路由签 Cookie。
2. `/admin/users/:userId` 通过专用详情 GET 读取安全账号元数据和三种生命周期的基础计数；正常作品列表使用现有管理员作品 API；账号操作使用原管理员用户写入 API。

证据：`research/auth-contracts.md`、`research/current-contracts.md`。产品和验收的权威来源为 `prd.md`，本文件只定义技术实现。

## 2. 数据与迁移

- User 新增 `last_login_at: datetime | None`，数据库 `DateTime(timezone=True)`、nullable、无历史默认值、不建索引。
- 计划新增 revision `0028`（文件建议 `0028_user_last_login.py`），父版本为当前 head `0027`；实施前再次核实唯一 head，出现并行迁移则协调后顺延，不覆盖他人版本。
- 使用原位 ADD/DROP COLUMN，不重建 users 父表，不更改子表外键、Project→User RESTRICT 或会话字段。
- 不新增表，不涉及项目备份注册表；既有 UserOut、管理员列表及写操作响应字段集合保持兼容。
- downgrade 会丢弃新增的登录时间记录，其他账号/作品/子资源不变；回滚前按需备份该列。测试验证真实存量子行和 foreign_key_check，而非只有空库往返。

## 3. 登录时间的事务与并发

### 密码登录

1. 保留初始用户查询、线程内 bcrypt 校验和现有密码错误/禁用文案。
2. 用 UTC 时间执行条件 UPDATE，核对 ID、启用状态、已验密码哈希与 token_version。条件失配不记成功时间、不发 Cookie，按当前情况返回原有安全 401/403，不泄露密码/账号状态。
3. SQL CASE 将 last_login_at 写为已存值和本次时间的较大者，避免迟到请求或时钟回拨让时间倒退；显式保持 `updated_at=User.updated_at`，不伪装账号配置编辑。
4. 使用受条件更新保护的认证返回快照，commit 成功后返回给原路由；不能 commit 后 refresh 成另一次并发重置后的 token_version 再签 Cookie。session 已配置 expire_on_commit=False，可保留被验证的快照。
5. 若后续禁用/重置在该事务之后发生，旧快照 Cookie 由现有会话版本检查失效；本任务不改变 JWT/cookie 属性，也不宣称能撤回已完成的登录 HTTP 响应。

### 注册自动登录

- 仅 register 通过现有 `create_user_uncommitted` 创建账号，在注册自己的提交边界记录首次 last_login_at 后提交。
- 不在共用创建原语、create_user 包装、bootstrap、改密或 `_set_auth_cookie` 中通用写入时间。
- 注册响应仍为现有 UserOut；创建失败/持久化失败不返回成功会话。

### 非目标行为

普通认证依赖、`/auth/me`、登出、改密续签、API Key 使用和管理员创建均不触碰 last_login_at。读取详情不写登录审计；原有管理员账号写入的 AuditOperation 不变。

## 4. 详情读接口

新增 `GET /api/v1/admin/users/{user_id}`，使用 `AdminUser`、DbDep，路由不直接查询 ORM。

```text
AdminResourceUsage = {
  project_count: int >= 0,
  chapter_count: int >= 0,
  total_words: int >= 0
}

AdminUserDetail = 原 AdminUserOut 的安全字段 + {
  last_login_at: datetime | null,
  usage: {
    active: AdminResourceUsage,
    recycled: AdminResourceUsage,
    deleting: AdminResourceUsage
  }
}
```

- 顶层 project_count 继续表示全部未物理删除作品，供既有 rowActions / 删号保护使用；不改为只算正常作品。
- service 建议 `admin_service.get_user_detail(db, user_id)`。只投影白名单账号列，不把完整 ORM/密码字段交给 schema 自动序列化。
- 统计由 SQL 完成：复用 `chapter_stats_subquery()` 的每作品单行章节数/字数，以目标 User 的项目为集合做条件聚合，再与安全 User 列关联；详情一次 SELECT 返回同一读取快照中的账号和统计，无逐项目调用、分页小计或 N+1。
- 空用户通过外连接/coalesce 得 0，不存在 User 得 `404 用户不存在`。分页作品在详情成功后另读，允许其为稍后的实时快照，不承诺跨请求事务快照；手动刷新重取详情和列表。

### 生命周期分组

| usage 字段 | 条件 | UI |
|---|---|---|
| active | deleted_at IS NULL 且 deletion_started_at IS NULL | 正常作品 |
| recycled | deleted_at IS NOT NULL | 回收站（包括到期待清理） |
| deleting | deleted_at IS NULL 且 deletion_started_at IS NOT NULL | 非零时单独提示“删除处理中”，不提供正常工作区链接 |

三组互斥且覆盖全部现存项目，三组作品数之和必须等于顶层 project_count。不能用“全部减正常”代替回收站。删除中只补充同维度计数以解释过渡状态，不扩展资源种类或开放新的删除操作。

章节数包含空章；total_words 只求和当前 Chapter.word_count，沿用非空白字符口径。不 join 历史版本/日志，不读取正文，不计算字节/Token/费用。

### 错误与披露

| 场景 | 结果 |
|---|---|
| 未登录/禁用/会话过期 | 既有 401 与统一前端清会话 |
| 当前非管理员 | 403 需要管理员权限 |
| 用户目标不存在 | 404 用户不存在 |
| 非法路径参数 | FastAPI 标准 422；前端非正/非安全整数不发请求、显示无效目标 |
| 读取失败 | 既有安全错误包，不回显 SQL、凭据或私有配置；可手动重试 |

## 5. 前端结构与复用

### 路由、导航和展示

- 在 RequireAdmin 下保留 `/admin/users` 列表，增加懒加载的 `/admin/users/:userId`；采用与其他管理组一致的 Outlet 布局，避免给没有 Outlet 的列表直接挂 children。
- 用户名链接及“详情”进入该路由；菜单仍以前缀选中“用户管理”。不扩展无关创作导航分组。
- 详情以 `actorId + targetUserId` 为 key 重建内部组件；非法 ID、非管理员和身份未知的守卫遵循现有规则。
- 页头为返回用户列表、用户名/我标识、手动刷新及账号操作。基本信息使用 Descriptions/Tag，资源区是紧凑对比表或响应式两列，不做营销式大卡片。
- 采用现有 AntD、中文文案和主题 token。375px 信息纵向排列、按钮换行；作品列表可桌面 Table/移动卡片，正文页面不因固定 minWidth 产生整页横滚。
- last_login_at 为 null 显示“暂无记录”；非空通过 formatTime，不直接 Date/dayjs 解析无时区字符串。

### 所属作品

- 首次详情读成功后，用 `adminApi.listProjects({owner_user_id: targetId, page, page_size})` 加载正常作品；owner 固定，不提供清空后变为全站的筛选。
- 详情列表只负责分页、名称/状态/章节数/字数/更新时间及进入 `/projects/{id}/overview`；复用 PROJECT_STATUS、formatTime 和响应类型，不复制全站搜索/删除工具栏。
- 账户详情与作品列表错误各自可重试；首次加载不显示另一个目标的旧列表。手动刷新同时刷新两者，返回详情重新获取最新统计。
- 保留 `/admin/projects?owner_user_id={id}` 和 `/admin/recycle-bin?owner_user_id={id}` 深链；从 A 目标切 B 再打开相同管理路由时仍须核实 owner 正确。若现有仅在初始化读查询串导致错误，做最小参数同步修复和回归，不改全站筛选架构。
- 作品工作区复用服务端 owner 与现有 ProjectManagementNotice / projectAccess；无需更改 owner helper、AI 能力或 Reader 进度逻辑。

### 账号动作

- 从现有 AdminUsersPage 中窄提取“目标账号操作控件/控制逻辑及弹窗”，供列表和详情共同使用；创建用户仍属于列表。可拆按钮与目标弹窗，但不建通用 CRUD 框架。
- 继续复用 rowActions、canConfirmDelete、userDeleteBlockReason、userProjectCountFromError、validation 及原写 API；不新增账号管理写入口。
- 回调区分 changed/deleted：列表刷新并保留末页回退；详情刷新，成功删号后安全跳回列表。冲突回写最新全生命周期 project_count，pending task 冲突原样显示。
- 每种写动作在提交前及响应落地前核对真实 actor、目标及请求代次；同步 pending ref 防双击。请求 gate 可复用 createRecycleRequestGate，不复用回收站的业务权限。
- 组件卸载/目标改变/身份失效时作废请求并销毁本组件创建的确认框，不调用全局 Modal.destroyAll。StrictMode 的 effect setup/cleanup/setup 不能使活跃组件永久失效。
- 角色/启停依旧确认，删号依旧严格用户名输入；忙碌期间禁止误关闭或再开另一危险目标。不得在过期回调里 toast、reload、导航或重开表单。

## 6. 安全边界与范围控制

- 用户详情不授予新增内容权限：模型/平台密钥、私有 Skill/MCP/配置、AI/任务输出、阅读进度保持严格 owner 隔离。
- 普通 UserOut、AdminUserOut、JWT 与 Cookie 属性保持兼容；只给新的详情响应增加字段。
- 只改 User 登录字段、认证记录、管理员详情查询、前端路由/页面及账号动作复用；除 owner 深链最小正确性修复外，不顺带重构作品、任务和全局状态。
- 基线中的 `.codex/config.toml`、`.gitignore` 及未跟踪的 source-command skills 不归本任务；不覆盖、暂存或提交。

## 7. 上线与回滚

1. 后端先备份并迁移到包含 last_login_at 的版本，再部署依赖它的应用；前端随后上线。不得以缺失列继续运行新 ORM。
2. 使用历史 null/零资源/有回收站用户做管理详情 smoke，再通过隔离测试账号登录观察更新；不使用真实用户凭据验证。
3. 回滚优先恢复旧应用并保留兼容新增列。确需 downgrade 时先停用新应用、按需导出登录时间，再原位删列；不得重建 users 或删除关联业务数据。
4. 本设计没有跨项目数据搬迁、后台统计或外部服务副作用。
