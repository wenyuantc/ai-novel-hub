# 备份任务提交与审计规范摘录

本任务规划时的规范摘录；原规范为权威来源。源文件变化后按定位重新读取相关条款。

来源：`.trellis/spec/backend/admin-audit.md:11`，行 11–50；原文件 SHA-256：`c1fcd6276fa9da9cb3cc8dc3553453a8f74653b49e44c6caa4acc2d4e525e64e`。

## 2. 签名

```python
# backend/app/services/audit_writer.py
class AuditOperation:
    def __init__(self, db, actor: User | int | None, action: AuditAction,
                 target_type: AuditTargetType, target_id: int | None = None,
                 *, project_id: int | None = None, enabled: bool = True,
                 operation_id: str | None = None,
                 include_own_project: bool = False, system_expiry: bool = False,
                 bootstrap: bool = False)
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

0024允许 `actor_user_id=null` 表示内部 `project.expire` 的「系统自动清理」。前端不得显示 #null 或假管理员ID；原人工历史ID仍保持。到期系统身份只允许system_expiry=True、空actor及project.expire。0029另允许内部bootstrap=True、空actor、user.roles.bootstrap，显示“管理员引导”；客户端不能设置这两个选项。


来源：`.trellis/spec/backend/admin-audit.md:55`，行 55–81；原文件 SHA-256：`c1fcd6276fa9da9cb3cc8dc3553453a8f74653b49e44c6caa4acc2d4e525e64e`。

### 写入与结果

- `task.cancel_request` / target_type=`task` 是异步取消的**受理动作**，成功只表示Task持久意图与受理审计同事务提交，不表示执行体已取消。本人作品也通过局部include_own_project=True采集；后续signal/abort/finalize按任务监控契约执行，不能用外层AuditOperation掩盖旧取消helper内部提交。

- 服务层显式接入，不使用 middleware/ORM 全局监听，也不 monkey-patch session.commit。必须保留真实 actor，不能用 owner 代替。
- `AuditOperation.__aenter__` 以当前授权快照核实启用管理账号（超管或有效权限非空），不只检查is_admin；动作授权仍由业务显式校验，enabled=False不是通行证；project_id 指向本人作品时默认关闭采集。管理员restore/purge通过局部 `include_own_project=True` 也记录自己的作品，系统expire走专用内部选项；不能因此全局开放本人日常创作采集。已确认私有 Skill 不采集，且不为审计越过原可见性 helper。
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


来源：`.trellis/spec/backend/system-settings.md:33`，行 33–41；原文件 SHA-256：`2c40b2cdb2a7882deb6482c3ec7b80675dc16a916351c4eb0c50841dce08f362`。

### 取值与写入

- 后台稀疏覆盖 > 启动时捕获的部署基线 > Settings代码默认；来源在发布前按原始model_fields_set捕获，显式应用注入也属于deployment。只保存六项，不序列化完整Settings。
- PATCH两层extra=forbid；省略保持、false明确关闭、null删除覆盖恢复部署值，changes不能为空。严格布尔/整数，拒绝布尔冒充整数及字符串/浮点；正整数最小1；开放API限额还须≤2_147_483_647（数据库Integer计数边界，部署基线同样fail-fast），不可存入超大值再让后续认证溢出500。不要求单用户≤全局，两级约束共同生效。
- 管理写锁序为既有_lock→共享AuthorizationRuntime→auth DB gate→User升序→设置资源，自有会话及revision条件UPDATE（首次唯一INSERT），真实管理员和成功审计同事务。先确认commit，后无await同步发布六字段，再notify调度；不先发布再存库。
- 正常失败回滚不发布。提交确认丢失重读持久状态，保留既有审计的真实结果，不盲重发旧候选。自有shield写工作不因HTTP调用者取消被拆断，关停close等待写任务；无法确认时settings_confirmed=False暂停新准入，dispatch直接返回避免热循环；即时重读仍须处于同runtime，后台恢复确认后再开放。load/reload/recovery所有发布路径共用runtime，禁止继续覆盖，不回退默认或宣称离线一致。
- 应用启动在bootstrap/任务恢复前load；持久值非法或读库失败不静默忽略。create_all测试不运行lifespan，fixture显式load/close。
- 审计action=system_settings.update、target_type=system_settings、固定目标。settings_changes最多6项，仅本动作可用；key/before_override/after_override/before_effective/after_effective/before_source/after_source均类型受限，失败不保留未提交after。禁止带入密钥、原始请求或异常。


来源：`.trellis/spec/backend/logging-guidelines.md:81`，行 81–88；原文件 SHA-256：`c29d53c85b4ac35930b260497b7ec484eed2783caff810a05f32e71747f3908c`。

## 审计敏感边界（强制例外）

`app/services/audit_writer.py::AuditOperation` 持有业务事务，SQLAlchemy异常可能包含章节正文、密码哈希等SQL参数；禁止直接logger.exception或继续向全局未知异常handler传播。只用`logger.error`输出固定中文消息、operation_id/committed，然后`raise AuditOperationError("服务器内部错误") from None`，由AppError handler返回固定500。失败审计不可写时同样只记安全标识，不递归审计。已提交业务的响应投影失败也不补写业务失败。

该例外不取消其他模块现有traceback规则。回归 `test_audit_system_database_error_does_not_reach_raw_exception_logger` 同时检查响应、审计行和caplog；不能仅脱敏数据库记录而遗漏运维日志。完整契约见[管理员操作审计](./admin-audit.md)。

管理员取消受理及异步drain/状态提交属于同一安全边界；共用的章节/批量/KB任务on_abort也可能从此链触发，失败只输出固定中文及任务/操作标识，不用原始traceback/SQL参数泄露断点或上游信息，也不把未知异常再次送全局logger.exception。不能因复用普通helper而在内层先泄露、外层再宣称安全。此局部例外不扩散到无关provider/SSE逻辑，具体入口与caplog回归见[任务监控](./admin-task-monitoring.md)。


来源：`.trellis/spec/backend/logging-guidelines.md:89`，行 89–102；原文件 SHA-256：`c29d53c85b4ac35930b260497b7ec484eed2783caff810a05f32e71747f3908c`。

## 不应记录的内容

- **绝不记录 API key** —— 无论明文还是密文（`api_key_enc`）。key 仅在
  `ai_config_service.build_provider` / provider 实例内部以明文存在；
  不要记录 `cfg`、provider 对象或 SDK 客户端的 kwargs。
- 绝不记录密码、密码哈希、JWT 或 `access_token` cookie、`JWT_SECRET`、
  `ENCRYPTION_KEY`。
- 不要记录完整的 prompt 或生成的章节正文（用户内容，可能很大）。
- 不要记录带 `input` 的 `RequestValidationError.errors()` —— 它包含原始请求
  （422 handler 出于同样原因去掉了 `input`，见 `error-handling.md`）。
- 不要为每个 SSE 分块添加 `logger.info`。

当确实需要展示敏感信息时（仅限 UI，不用于日志），使用
`app.core.security.mask_secret`（`sk-***1234`）。
