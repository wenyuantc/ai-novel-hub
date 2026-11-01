# 管理员操作审计与日期清理

## 1. 范围 / 触发条件

适用于 `admin_audit_logs`、`audit_writer`、`audit_service`、`/admin/audit-logs` 及前端审计页。修改范围内管理写入口或增加动作时，必须同步审计、传输类型、标签及覆盖测试。

首批包括：用户创建/角色状态/重置密码/删除；跨用户作品资料与删除、章节 CRUD/从大纲建章/选角/版本维护；工坊发布/审核/元数据/上下架/删除；全局 Skill 创建/导入/修改/参考资料/内置恢复/删除；审计清理。0024增加管理员回收站恢复/彻底删除及系统到期清理（见[回收站](./recycle-bin.md)）；0026增加显式管理员任务取消受理（见[任务监控](./admin-task-monitoring.md)）。其他设定子模块、本人日常创作、普通用户行为、普通AI/任务、私人资源及平台 Key 不自动采集。

权限沿用 [管理员会话](./auth-admin.md)、[人工作品管理](./admin-projects.md)、[工坊](./workshop.md)、[Skill](./skills-mcp.md)，不扩大读取或管理权限。普通用户和未登录/参数校验失败不记管理员操作。

## 2. 签名

```python
# backend/app/services/audit_writer.py
class AuditOperation:
    def __init__(self, db, actor: User | int | None, action: AuditAction,
                 target_type: AuditTargetType, target_id: int | None = None,
                 *, project_id: int | None = None, enabled: bool = True,
                 operation_id: str | None = None,
                 include_own_project: bool = False, system_expiry: bool = False)
    def target(self, obj, *, label: str | None = None)
    def track(self, obj, fields)
    def version(self, version: ChapterVersion | None, project_id: int)
    def affected(self, ids: list[int])
    async def commit(self, commit_fn=None, *, flush_fn=None)

class AuditOperationError(AppError):  # 固定安全 detail，status_code=500
    status_code = 500

# backend/app/services/audit_service.py
async def list_logs(db, filters: AuditLogFilters) -> AuditLogList
async def get_log(db, log_id: int) -> AuditLogDetail
async def preview_cleanup(db, actor: User, dates: AuditDateRange, secret: str) -> AuditCleanupPreview
async def cleanup(db, actor: User, req: AuditCleanupRequest, secret: str) -> AuditCleanupOut

# backend/app/services/auth_service.py
async def create_user_uncommitted(db, creds: Credentials, *, is_admin: bool = False) -> User
async def create_user(db, creds: Credentials, *, is_admin: bool = False) -> User

# backend/app/services/version_service.py
async def flush_versioned(db) -> None
```

迁移 `0023_admin_audit_logs`（revision 0023，父 0022）新增实例级表 `admin_audit_logs`。模型为 `app/models/audit.py::AdminAuditLog`，在 models 导出；不加任何业务外键、不进项目备份。SQLite AUTOINCREMENT；operation_id 唯一；按时间、actor+时间、target_type+target_id+时间建索引。

保留 primitive 快照字段：actor ID/用户名、target 类型/ID/名称、owner ID/用户名、project ID/标题、chapter ID、UTC occurred_at、action/result、summary、version_ref、安全 error_code/error_message。删除业务对象、用户或版本不会级联删审计，也不会抹掉原始标识。

0024允许 `actor_user_id=null` 表示内部 `project.expire` 的「系统自动清理」。前端不得显示 #null 或假管理员ID；原人工历史ID仍保持。只有 `system_expiry=True` 且actor为空、action为project.expire才允许系统身份，客户端不能设置该选项。

## 3. 契约

0027接入 `system_settings.update` / target_type=`system_settings`，变更与持久覆盖同事务；settings_changes为最多六项的局部类型化摘要（key、覆盖前后、有效前后、来源），只该动作可携带。不得记录完整Settings或任意JSON；失败不保留未提交after。运行时发布/提交确认协调见[系统设置](./system-settings.md)。

### 写入与结果

- `task.cancel_request` / target_type=`task` 是异步取消的**受理动作**，成功只表示Task持久意图与受理审计同事务提交，不表示执行体已取消。本人作品也通过局部include_own_project=True采集；后续signal/abort/finalize按任务监控契约执行，不能用外层AuditOperation掩盖旧取消helper内部提交。

- 服务层显式接入，不使用 middleware/ORM 全局监听，也不 monkey-patch session.commit。必须保留真实 actor，不能用 owner 代替。
- `AuditOperation.__aenter__` 核实当前启用管理员；project_id 指向本人作品时默认关闭采集。管理员restore/purge通过局部 `include_own_project=True` 也记录自己的作品，系统expire走专用内部选项；不能因此全局开放本人日常创作采集。已确认私有 Skill 不采集，且不为审计越过原可见性 helper。
- 业务变更和成功审计共用一次事务。通常通过 audit.commit 设置 committed，再 refresh/构造响应；非审计路径仍按原规则提交。工坊 approve 保留既有例外：在条目/审核锁释放前先 flush/refresh 并生成 output，再 audit.commit。不能为了统一文案把其响应投影移至提交后；提交前投影失败仍整体回滚。
- 业务 flush 在成功审计入库前完成；调用者传已有业务错误翻译的 flush_fn。章节使用 version_service.flush_versioned，Skill 使用 _flush_unique，审计表自身错误不能被误译成业务唯一键 409。
- 失败先 rollback，再使用提前保存的安全快照写失败记录。不能在 rollback 后读过期 ORM 属性，不能把未提交的 after 值当作事实。
- 提交后响应失败不写第二条业务失败。同 operation_id 已有成功事实时（包括 commit 确认丢失）不追加相反结果；持久化唯一约束兜底。
- 审计写不进去不能放行业务提交。数据库不可用时失败记录不能保证落库，不递归审计、不重新提交原业务。
- 范围内未知系统异常由 AuditOperationError 固定 500 截断，运维日志只保留 operation_id/committed；不得把含正文/密码 SQL 参数的原异常再次交给全局 logger.exception。该安全例外优先于一般 traceback 规则，见 [日志规范](./logging-guidelines.md)。
- create_user_uncommitted 只创建/flush；管理员创建由 audit.commit 提交。普通 create_user 包装仍提交并 refresh，注册与 bootstrap 原契约不变。
- 工坊 submit 自动通过与官方发布只记一次 workshop.publish；Skill import→create 只记一次 skill.import。原上传大小守卫保留，global 超限拒绝通过专用服务记安全失败。

### 摘要与版本

`schemas/audit.py` 是唯一后端传输定义；TS 镜像在 `frontend/src/api/types.ts`，标签的唯一来源为 `api/auditLogs.ts`，日期/请求失效工具在 `utils/auditLogs.ts`。

summary 拒绝未知键：changed_fields、受限状态/布尔/数值 changes、正文前后字数、Skill 正文字符数、参考资料数量、affected_count/affected_ids（最多100，ids_truncated）、删除章节/版本数、清理范围/预计/实际条数和高水位；0024增加受限 `lifecycle_action`（soft_delete/restore/purge/expire）、`retention_days` 和 `cancelled_task_count`。新project.delete标明软删，既有永久删除历史不重写。

0026仅为task.cancel_request增加task_key（UUID）、task_kind、accepted_from_status和cancel_reason；原因trim后1–200字且无Unicode C类字符，是唯一局部允许的管理员主动自由文本例外。writer拒绝其他动作带入这组字段，不开放任意JSON。失败只保留已校验的task_key/task_kind/cancel_reason白名单，不保留未提交状态变化；自动摘要不复制Task.title/error/input/output/checkpoint。前端该动作的成功标签显示“已受理”，其他动作仍为“成功”。

禁止保存密码及哈希/长度、模型与平台 Key 明密文、JWT/Cookie、请求/异常原文、正文全文或 diff、版本备注全文、Skill/工坊正文。target_label 和用户名只作有长度限制的身份快照。

version_ref 仅为 project/chapter/version ID、seq、content_sha256；详情比对得到 available/changed/missing。版本会原地合并、裁剪、删除，不因审计修改原规则。审计 API 不返回正文；用户主动查看才经现有 versionApi.get 读取当前业务版本，界面提示它不一定是原操作时全文。

### API 与日期清理

所有端点均使用 AdminUser，前缀 `/api/v1/admin/audit-logs`：

| 方法/路径 | 请求 | 响应 |
|---|---|---|
| GET 空路径 | actor_user_id?, actor_q?≤64, target_type?, target_id?, action?, result?, from_at?, to_at?, page≥1, page_size=20（1–100） | {items: AuditLogBrief[], total} |
| GET /{log_id} | log_id>0 | AuditLogDetail（brief+summary/version_ref/version_status） |
| POST /cleanup-preview | {from_at,to_at} | {from_at,to_at,count,protected_count,expires_at,preview_token} |
| POST /cleanup | {preview_token,confirm:true} | {deleted_count,audit_log_id} |

brief 包括身份/目标/归属快照、动作/结果/时间和安全错误，不含 summary 或正文。actor_q 搜索历史用户名，转义 %、_、反斜线为字面量；count/列表共用过滤，排序 occurred_at DESC,id DESC。

日期必须成对、带时区、起<止，统一 UTC 半开 `[from_at,to_at)`。前端本地日期包括结束日整天，转至次日零点作为排他终点，明确展示本地 UTC 偏移及后端区间。

长期保留，无自动过期。清理仅按独立日期，不继承列表其他筛选。预览 JWT 使用已有 JWT_SECRET、HS256，purpose=admin_audit_cleanup、TTL10分钟，绑定 actor/range/high_water_id/count/nonce。令牌不渲染 DOM；前端必须重新勾选不可恢复确认才可执行。

执行再次验管理员/签名/purpose/有效期/actor，先以 nonce 唯一 operation_id 占位；DELETE 条件必须同时有日期、id≤预览高水位及 action != audit.cleanup。删除和成功清理事件同事务；成功/失败清理事件一律受保护。重放409，失败后重新预览。高水位不是物化快照，实际数量可能不同，但不包含之后新分配 ID 的记录。

前端 `/admin/audit-logs` 使用 RequireAdmin+懒路由；AppLayout 提供桌面/移动入口。列表有组合筛选、分页、空/错态和重试；详情只读。清理日期/账号/弹窗关闭会使旧预览失效；请求序列+当前 actor 验证阻止旧响应复活，busy 禁重复提交；结果不确定提示先刷新清理留痕。移动端用卡片而非撑宽整页的表格。

## 4. 校验与错误矩阵

| 条件 | 结果 |
|---|---|
| 未登录、失效/禁用账号 | 401，无管理员操作审计 |
| 普通用户读取/详情/清理 | 403，无管理越权采集 |
| 非正 ID、非法枚举、日期不成对/无时区/起止颠倒、confirm 非 true | 422，不产生操作审计 |
| 记录不存在或已清理 | 404，不能恢复已清掉审计 |
| 已认证管理员的范围内业务拒绝 | 保留原 400/403/404/409，持久化安全失败类别 |
| 无效/过期/错误 purpose 预览 | 400，extra.code=invalid_preview |
| 预览 actor 与当前管理员不同 | 403，code=preview_actor_mismatch |
| nonce 已消费，包括并发重放 | 409，code=preview_replayed，不重复删除 |
| 业务版本唯一竞争 | 409，不能因审计先 flush 变500 |
| 范围内未知系统故障 | 安全 500，不把原始 SQL 参数放进响应/日志 |
| 成功已提交但投影失败 | 安全 500，原成功审计保留，不追加失败 |
| 清理 DELETE 或成功留痕失败 | 删除共同回滚；有可用库时另记受保护失败 |

“受保护”限应用 API，不是针对数据库管理员的防篡改保证；也不承诺进程终止/同库故障下失败事件绝不丢失。

## 5. 正例 / 基准 / 反例

- 正例：管理员 A/B 连续改 Alice 正文，即使命中同一 manual 版本，也留下两条真实 actor 的摘要；A 的关联可显示 changed，业务版本删除后两条摘要均保留。
- 基准：所有者修改本人正文按旧规则保存/合并版本，不采集成管理员行为；普通注册/bootstrap 不被审计改掉提交语义。
- 清理：预览31条后又产生更大 ID 的事件，执行只清界限内31条；再次清理仍保留先前 audit.cleanup；删掉原管理员后其清理事件仍有 ID/用户名。
- 反例：业务先 commit、HTTP200后补日志；或 catch 原始 SQLAlchemy 异常后 logger.exception——前者失去原子性，后者可能泄露参数。

## 6. 必需测试

`backend/tests/test_admin_audit_logs.py`：
- all_audit_routes_require_current_admin：全部端点401/403及实时权限。
- user_success_failure_credentials_and_noop、registered_action_failures_keep_safe_identity_without_false_success：覆盖动作/失败/无变化和凭据不外露。
- project_chapter_two_actors_versions_and_deleted_snapshots：双 actor、同版本合并、删除后历史。
- chapter_cast_outlines_versions_success_and_business_failures、global_skills_import_and_workshop_single_events：首批矩阵与嵌套不双记。
- filters_stable_order_literal_search_pagination_and_safe_failure_target：组合/字面搜索/同时间排序/分页。
- audit_write_failure_rolls_back_business_and_safe_system_failure、user_create_audit_failure_and_postcommit_projection_failure、commit_confirmation_lost_never_adds_failure：成功/失败原子性及不确定提交。
- version_unique_competition_remains_safe_409：手动版本写入和 commit 前业务 flush 均保持409；audit_system_database_error_does_not_reach_raw_exception_logger：响应、审计和 caplog 均无原始SQL参数。
- cleanup_half_open_utc_high_water_protection_and_actual_count、cleanup_bad_tokens_safe_protected_failure、cleanup_atomic_failure_keeps_rows_and_protected_failure、cleanup_same_nonce_concurrent_only_one_result、cleanup_parallel_preview_actual_count_can_differ：日期/令牌/并发/保护与回滚。
- oversized_global_skill_import_still_rejected_without_content、global_skill_slug_conflict_is_409_and_not_audit_integrity_failure、summary_whitelist_rejects_free_text_and_unbounded_values：安全守卫、冲突边界及有界摘要。

`backend/tests/test_migrations.py` 固定0023升降级与head链路；原有 admin/auth/projects/chapters/versions/cast/skills/workshop 回归不得放宽。

新增 `test_skill_create_and_workshop_publish_business_rejections_are_audited` 验证全局 Skill 创建及工坊发布的业务拒绝；`test_private_skill_denial_audits_only_safe_id_and_private_writes_are_excluded` 验证不可见私有 Skill 的失败只留安全 ID、私有日常写不采集。

前端 `api/auditLogs.test.ts`、`utils/auditLogs.test.ts`、`components/auditLogs/auditLogsUI.test.ts`：参数/凭据传输、日期转换、请求失效/账号变化、清理令牌不入DOM、受保护说明和字段标签（content中性“正文内容”、hidden_by“下架方”）。真实浏览器补筛选/详情/预览取消、375px/主题、网络恢复与权限导航，不以SSR替代。

## 7. 错误写法 vs 正确写法

错误：先提交业务，再另写审计，且不保留原版本错误边界。

```python
await db.commit()
await audit.commit()
```

正确（章节 service 的事务出口）：

```python
await audit.commit(commit_versioned, flush_fn=version_service.flush_versioned)
await db.refresh(chapter)
```

错误：日期 DELETE 忘记 action 排除，会连清理行为一起抹除；或只凭客户端 count/confirm 删除。

正确：先验证绑定当前 actor 和范围的短期 preview_token、消费 nonce，再在同事务中删除日期及高水位内的非 audit.cleanup 记录并保存实际条数。权限、保护与原子性必须在服务端保证。
