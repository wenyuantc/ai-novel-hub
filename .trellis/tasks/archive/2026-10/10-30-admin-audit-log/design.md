# 管理员操作日志：技术设计

状态：用户已于 2026-10-31 批准最终规划，任务进入实施。对应 PRD R1–R6/AC1–AC10。仅规划文档可在本阶段变更。

## 1. 边界与最小架构

复用 FastAPI → service → SQLAlchemy/PostgreSQL、SQLite 测试、AntD/React 管理页。不引入外部日志系统、新数据库、全局事务框架或权限角色。

数据流：

```
真实认证 + schema 校验
  → 已登记业务 service（确定动作/对象/是否属于管理行为）
  → 业务修改 + 成功审计（同一事务）
  → audit 查询 service → Pydantic → api/auditLogs.ts → 管理页/只读详情

业务拒绝/提交失败 → 回滚原事务 → 新事务写安全失败事件
日期清理 → 管理员预览 → 绑定范围的确认令牌 → 删除 + 受保护清理事件（同事务）
```

- 新增 `models/audit.py`、`schemas/audit.py`、`services/audit_service.py`；事务写入/动作定义过大时仅在同领域拆 `services/audit_writer.py`，不预建通用框架。
- 新增 `api/v1/admin_audit.py`，在现有 v1 router 中注册；路由仅依赖/校验/序列化，ORM 保持在服务。
- 审计发生在掌握业务语义、且尚未 commit 的 service 边界，不在成功 HTTP 响应之后补写，也不通过 ORM 自动差异采集所有字段。

## 2. 持久化与传输

拟新增 `0023_admin_audit_logs.py`（父 revision 0022；开工前重查），单表 `admin_audit_logs`：

| 字段 | 设计 |
|---|---|
| id | 递增主键；SQLite AUTOINCREMENT，清理后不复用 |
| operation_id | 36 字符唯一操作标识；同一操作成功/失败最多留一个结果，清理预览 nonce 也使用此约束 |
| occurred_at | 服务端 UTC、带时区 datetime，支持微秒；按 occurred_at DESC, id DESC 稳定排序 |
| actor_user_id / actor_username | 数字 ID 与用户名快照（≤64），不依赖当前用户存在 |
| action / result | 稳定动作代码（≤64），success/failure；schema 使用 Literal/集中常量，不用 DB Enum |
| target_type / target_id / target_label | 固定目标类型、可空数字 ID、≤200 名称快照；创建失败可无 ID |
| owner_user_id / owner_username | 可空所有者快照；全局资源无 owner |
| project_id / project_title / chapter_id | 可空定位快照，不从当前归属重新推断历史 |
| summary | 由 Pydantic 校验的有界 JSON：changed_fields、安全标量前后值、字数、计数、批量 ID 摘要 |
| version_ref | 可空 JSON：project/chapter/version ID、seq、内容 SHA-256；摘要独立于此关联 |
| error_code / error_message | 可空安全错误分类和固定中文说明；不存任意异常 detail/extra 或请求内容 |

所有实体 ID 均为快照，不建 CASCADE/SET NULL 外键。表是实例级审计，不属于项目导入导出注册表。索引至少覆盖时间、actor+时间、target_type+target_id+时间；动作/结果按查询实际补充，避免无依据堆叠索引。

API Out schema 只列明必要字段；不从 ORM 对象全量 dump。摘要不允许任意 dict 注入；状态/布尔/数字为白名单，正文只留字数/字段名。目标名/用户名作为有长度约束的身份快照，不保存文本正文、备注内容或凭据字段。

## 3. 写入契约与事务

提供小型显式操作上下文，持有 primitive actor/target 快照、operation_id、结果和 committed 标记：

1. 在业务校验之前建立操作上下文；真实 actor 来自当前用户或受信任的 actor ID，不从 target/owner 推导。仅当前启用管理员且动作属于范围才启用。
2. 在原有授权 helper 中确定 target/owner；不为审计额外读取不可见的私有资源。已知是自己的创作/私有资源时关闭采集；不存在对象只保存安全 ID 的失败，不伪造所有者。
3. 业务变更完成后，在同一 session 中添加成功事件，使用原有 commit/commit_versioned/唯一冲突翻译语义共同提交。普通非审计路径保持原行为。
4. 通常提交返回后立即标记 committed，再做 refresh/响应投影；后续响应失败不再记第二个业务失败。工坊 approve 按既有锁契约在提交前 flush/refresh 并生成 output，再 audit.commit；保留这一例外，提交前投影失败仍回滚，不为统一流程改变原锁保护。
5. 业务异常或提交前异常：回滚整个业务事务，再在干净的新事务中写失败。rollback 后不访问已过期 ORM 的属性，所有必要信息事先快照。
6. failure 仅保留明确业务代码或通用 system_error，不能直接使用 str(exc)、exc.detail、SQL 参数或完整栈作为审计内容。未登录/普通用户/422 不进入此服务上下文。
7. 审计失败不能被吞掉并继续提交业务。失败审计本身写不进去时，不递归写审计，保留已脱敏运维错误并返回安全错误。
8. operation_id 唯一约束防止同一操作同时留下成功与失败；若提交确认丢失，失败补写不能覆盖或并列于已持久化的成功事件。需要用新事务查询或唯一键冲突识别处理，而不是把所有 IntegrityError 都误译成业务字段冲突。

不 monkey-patch AsyncSession.commit，不增加全局 ORM listener，也不把普通 register、bootstrap、自助改密和后台任务纳入采集。管理员创建用户抽出共用的不提交原语，保留原公开 create_user 的提交包装以保持注册/bootstrap 行为。

成功调用但字段未实际变化仍可记录事件，changed_fields=[]/changed=false，不能捏造变更。一个 PATCH 同时变更角色与启用状态记一条 user.update，摘要清楚区分字段。

## 4. 动作接入清单

| 动作代码 | 接入业务边界 | 关键限制 |
|---|---|---|
| user.create/update/reset_password/delete | admin_service；create/reset 补 actor | 自身/最后管理员防护与 token_version 原规则保留 |
| project.update/delete | project_service | 仅跨用户；删除前快照项目与级联数量 |
| chapter.create/create_from_outlines/update/delete | chapter_service | 仅跨用户；沿用字数、生成中冲突、关联及版本规则 |
| chapter.cast.update | cast_service.set_cast | 摘要记录 ID/数量，不复制角色档案 |
| chapter_version.update/delete/restore | chapter_version_service | 备注只记字段/是否变化，不复制任意备注正文 |
| workshop.publish/approve/reject/update/hide/unhide/delete | workshop_service | submit 自动通过和 /admin/items 复用只记一次；保持锁顺序 |
| skill.create/import/update/references.replace/delete/restore_builtin | skill_service | 仅全局；import→create 只记一次，解析业务失败亦可留安全失败 |
| audit.cleanup | audit_service | 成功/失败均受保护；预览本身不另记写事件 |

其他人工子模块不自动接入；新增动作应在同一动作定义、schema/TS 标签和覆盖测试中登记。

### 版本关联

- 复用 record_manual/restore 已产生的版本返回值，必要时在同一事务查取；同文/清空正文/元数据修改可没有新版本。
- 记录关联时的 version ID/seq/content hash，不保存正文；不修改现有合并、裁剪、标星规则。
- 详情在已核实 project/chapter/version 关系后计算 available/changed/missing 状态，不经审计 API 返回版本正文。
- 查看正文是用户主动触发的独立既有 versionApi.get；界面始终说明业务版本可变。关联失效不影响事件查看，不把后来合并的正文标作操作时刻全文。

## 5. 查询与清理 API

共同前缀 `/api/v1/admin/audit-logs`，全部使用 AdminUser。

| 方法/路径 | 请求 | 响应 |
|---|---|---|
| GET 空路径 | actor_user_id?, actor_q?≤64, target_type?, target_id?, action?, result?, from_at?, to_at?, page≥1, page_size=20(1–100) | {items: AuditLogBrief[], total} |
| GET /{id} | 正整数 id | AuditLogDetail（完整安全摘要、版本关联状态，不含正文） |
| POST /cleanup-preview | {from_at, to_at}，均为带时区时间 | {from_at,to_at,count,protected_count,expires_at,preview_token} |
| POST /cleanup | {preview_token,confirm:true} | {deleted_count,audit_log_id} |

固定清理路径在动态 ID 路径之前注册。查询/预览/执行复用同一日期规范化与过滤 helper，使用参数化 SQL。actor_q 对用户名快照做不区分大小写的字面子串匹配并转义 %/_/反斜线，能查已注销管理员，不依赖当前用户列表。

日期采用半开区间 `[from_at,to_at)`，起止必须成对、带时区且 from<to。前端按浏览器本地日期转为起始日零点到结束日次日零点的 UTC ISO 字符串；确认中显示本地日期与 UTC 偏移，服务端统一为 UTC。API 不接受客户端自由拼接 SQL 或可空清理范围。

### 清理确认与并发

- 预览不删记录；计数分为可清理和 action=audit.cleanup 受保护记录。无可清理项时 UI 不开放确认。
- 使用项目已有 PyJWT/JWT_SECRET 签发独立 purpose 的十分钟短期令牌，绑定 actor ID、日期范围、预览高水位 ID、预计数量和随机 nonce；不复用登录 token 的 purpose。
- 执行再次验证当前管理员、令牌签名/purpose/有效期/actor/字段边界及 confirm=true。不能只靠前端弹窗或可伪造的 expected_count。
- 删除条件固定为日期范围、id≤高水位、action != audit.cleanup；不接收任何“包含受保护记录”选项。
- 高水位避免删到预览后新分配 ID 的事件；不是物化快照，既有未完成事务或并行清理可能使实际数与预计不同，结果以实际受影响行数为准。
- 删除与插入 action=audit.cleanup 的成功事件同事务，summary 含范围、预计/实际条数、高水位。nonce 映射 operation_id 唯一约束，令牌重放或并发重复提交返回 409，不重复删除。
- 失败清理也用 audit.cleanup 并保持保护；失败后重新预览。过期/他人令牌/重复 nonce 只返回安全错误，不披露令牌内容；无可识别范围时失败留痕只存安全类别。
- 不提供编辑审计/单条删除/全量无范围清空的 API；所谓“不可再次清除”限于应用入口，不承诺阻止数据库管理员直接改表。

### 错误约定

401 未登录/失效；403 非管理员或令牌与当前 actor 不匹配；404 记录不存在或被清理；422 参数不合法（无操作审计）；400 无效/过期预览（安全原因）；409 重放预览；500 安全系统错误。预览及列表读取失败不自我递归审计。

## 6. 前端

- 新增 RequireAdmin + lazyPage 路由 `/admin/audit-logs`，同步 AppLayout 用户菜单、桌面/移动导航、页面名和面包屑。
- 新增 `pages/AdminAuditLogsPage.tsx`，按职责拆 `components/auditLogs/AuditLogDetailDrawer.tsx`、`AuditCleanupModal.tsx`；不重构其他管理页。
- 布局为紧凑标题/说明、可换行筛选、表格/移动卡片、只读详情。保留现有主题 token、App.useApp、useModalWidth、时间工具与请求错误格式；不做营销式 hero 或额外 dashboard。
- 筛选变更回第 1 页；历史 actor 可按用户名/ID输入，目标类型/ID、动作、结果和日期可组合。空结果、加载、错误+重试均可见。
- 清理使用独立日期选择，不隐式带列表其他筛选。日期变动清空预览/令牌；预览后展示范围、预计数、保护数、不可恢复及保护说明，明确确认才提交。
- 防重复点击；发起预览/执行后以请求序列或 mounted+actor/range key 丢弃旧响应。切账号、关弹窗、范围变化后不得复活旧确认。过期/409要求重新预览。
- 完成显示实际条数并重新加载列表；当前页被清空时回退有效页，详情被其他管理员清掉时显示已不存在提示。
- 审计正文无本地存储/全局缓存；若主动查看业务版本，关闭详情后丢弃正文状态。

## 7. 兼容、上线与风险

- 只新增表/API/页面并为范围内服务补审计，不更改原业务响应字段及授权；create/reset 服务签名变动同步路由和测试，注册/bootstrap 保持默认行为。
- 迁移不回填历史事件；启用前先升级数据库，再部署代码。回滚优先回应用版本并保留审计表，不自动 drop 已积累记录；显式降级会丢失审计，应先备份。
- 成功审计与业务原子，不等于数据库故障/进程崩溃下的失败事件绝不丢失；独立可靠投递、外部防篡改和归档均不在本期。
- 清理不可恢复但受保护事件持续增长；版本可能合并/删除；高水位预览并非精确快照。
- 最关键回归点：actor/owner 混淆、嵌套服务双记、服务内 commit 提前释放、失败后 ORM 属性过期、业务 IntegrityError 与审计唯一键混淆、日期时区、预览重放和保护过滤遗漏。
