# 作品回收站与删除生命周期

## 1. 范围 / 触发条件

适用于作品删除、管理员回收站、任务准入/收尾、删号保护、到期清理、审计和相关前端。作品删除不再是物理删除；任何新增作品内容读写、直接 ID 查询、自动任务入队或独立流会话，都必须接入本契约。

沿用单进程任务引擎前提。管理员只有人工内容管理与回收站管理权限；删除期间对目标任务的内部取消不是通用的他人任务/模型权限。

## 2. 签名（数据库 / API / 服务）

迁移 `0024_project_recycle_bin`（父0023）：

- `projects.deleted_at`、`last_deleted_at`、`deleted_by_user_id`、`deleted_by_username`、`deletion_started_at`、`deletion_token` 均可空；删除人是无业务外键的快照，last_deleted_at是跨恢复保留的内部代次高水位。
- `(deleted_at, id)` 索引；已删除和删除中不能并存，意图时间/token 必须同时为空或非空。
- SQLite projects启用AUTOINCREMENT，物理purge后不复用实体ID；PostgreSQL保持既有序列。仅时间高水位不能保护“purge→同名新实体且ID复用”的情况，两者必须配套。
- `projects.user_id` → users 改为 RESTRICT；项目向下关联外键不变。
- `admin_audit_logs.actor_user_id` 可空，系统自动清理使用空 ID，不使用伪造用户。

```python
# app/services/project_service.py
async def get_owned_project(db, user_id, project_id, *, lock: bool = False) -> Project
async def get_manageable_project(db, actor_user_id, project_id, *, lock: bool = False) -> Project

# app/services/project_lifecycle.py
RETENTION_DAYS = 30
def active_conditions()
def ensure_active(project: Project | None) -> Project
async def lock_record(db, project_id, *, active_only: bool = False) -> Project
async def lock_active(db, project_id) -> Project

# app/services/recycle_bin_service.py
async def move_to_recycle_bin(db, engine, actor_id, project_id) -> None
async def list_projects(db, *, q, owner_user_id, page, page_size) -> RecycleProjectList
async def restore(db, actor_id, project_id, deleted_at) -> None
async def purge(db, engine, actor_id, project_id, deleted_at, *, title=None, cutoff=None) -> None
async def recover_pending_deletions(sessionmaker) -> None
async def purge_expired(engine, *, now=None) -> int
class RecycleBinCleaner:  # start()/stop()，默认 interval=3600 秒
    ...

# app/services/task_service.py
async def stop_project_tasks(db, engine, project_id) -> int
```

所有回收站接口前缀 `/api/v1/admin/recycle-bin/projects`，使用 `AdminUser`：

| 方法/路径 | 输入 | 响应 |
|---|---|---|
| GET 空路径 | q?≤200、owner_user_id?>0、page≥1、page_size=20（1–100） | `{items: RecycleProjectOut[], total}` |
| POST /{id}/restore | `{deleted_at: 带时区时间, confirm:true}` | 204 |
| POST /{id}/purge | 同上，加 `title: string(1–200)` | 204 |

普通 `DELETE /projects/{id}` 仍是 `CurrentUser`、204；路由显式传入 TaskEngine，内部校验 owner 或人工管理员权限。

## 3. 契约

### 生命周期与事务屏障

```text
正常 → 删除中（持久化意图/token）→ 排空任务执行链 → 回收站
              └─ 失败/超时/取消 → 正常，已取消任务不复原
回收站 → 管理员恢复 / 手动彻删 / 系统到期清理
```

- 正常项目条件是 `deleted_at IS NULL AND deletion_started_at IS NULL`，不能只看创作 `status`；原 status、ID、owner 不因软删除/恢复而改变。
- 写入口显式 `lock=True`，在同一事务中用集中条件 no-op UPDATE 锁父项目并检查生命周期；显式保留 updated_at。所有权检查仍由 owner/manageable helper 负责，生命周期锁本身不是授权。
- 不能只在请求开始查一次：旧请求可能在删除后才提交。多事务/独立 session 必须在后续项目写入前重新获取屏障；智能体工具、分析回写、直接章节 ID 同样适用。
- 删除先提交自己的意图，封住普通写入与新建/重试/继续/自动派生任务，再取消排队、运行、待审核任务。等待期间不持有父行事务锁，以免收尾等待同一行死锁。
- 必须等待真实运行体、请求侧取消清理和成功回调；数据库任务终态、`cancel_running` 返回或活动任务数量为零都不是充分条件。引擎 `cleanup_scope` / `project_work` 也覆盖请求侧收尾。
- 只有排空执行链、资源状态可恢复且重新核实 actor 权限后，才在锁定父行的事务中写 deleted_at/删除者、清除意图和提交成功审计。
- 失败只释放本次 token 的意图。请求取消需可靠 finally；数据库暂不可用留下的意图仅在下次单进程启动、引擎运行前解除，不能自动完成旧删除。

### 访问与完整性

- 个人/管理员正常作品列表及 count、开放 API 列表/授权、直接章节/回写 ID、正文/设定/版本/封面、Task 输出/SSE/清理/裁剪、日志、备份独立流会话、灵感知识注入都必须检查生命周期。
- KeyInfo/ApiKeyPrincipal及密钥列表的project_ids只投影正常作品，不能向开放接口继续列出回收站ID；底层api_key_projects授权关系仍保留，纯改名不撤销隐藏授权，作品恢复后按原授权自动可用。不要把持久授权与当前可见项目列表混为一谈。
- 回收站 Task 历史不由普通全局清理/裁剪偷偷删除；用户级资源引用数仍计算回收站的真实引用，不误作正常书架统计。
- 软删除保留项目外键图，包括不导出的 Task/agent/检索数据。恢复不复制项目，不启用禁用账号，不自动排 AI/向量任务，不重建已撤销授权或独立删除的私人资源。
- purge 物理清理只属于目标作品的数据；灵感会话按原 SET NULL 留存，用户级模型/风格/Skill/MCP 和其他作品不删除。提交后失效目标检索缓存/任务 hub；客户端仅丢弃目标项目/任务状态。
- `deleted_at` 原始 UTC 微秒值是回收站代次。restore/purge 锁行重读并逐值比较，旧页面不能操作“恢复→再次删除”的新记录；前端不得经 Date 格式化再回传。
- 不得假定 now_utc 每次都不同：最终软删在父行锁内取 `max(now_utc,last_deleted_at+1微秒)` 并同事务更新当前标记和持久高水位；恢复不清高水位。正常时钟为当前UTC，冻结/回拨时为单调逻辑UTC，跨session/重启也不能复用代次。高水位属于生命周期元数据，不纳入正文/设定等原内容的无损比对，也不随备份迁移。
- 回收站列表仅含 id/title/genre/status、owner ID/用户名、删除者快照、deleted_at/expires_at/restorable、章节/字数，不读取正文或图片二进制。count/items 共用条件；搜索按字面匹配转义 `%`/`_`/反斜线，按 deleted_at DESC,id DESC。

### 保留期、清理、删号与审计

- UTC 连续30天；`now < deleted_at + 30天` 才可恢复，等于即过期。清理延迟或停机不延长恢复窗口。
- 启动补清一次，之后每小时清理；一次固定 cutoff，按删除时间/ID 每批100条键集读取，每个作品独立事务重检期限。失败项下轮重试，不死循环、不饿死其他目标；有异常执行体时拒绝物理删。
- shutdown 等待 cleaner，再停任务引擎，最后关闭 DB；测试不靠页面访问触发后台清理，也不泄漏后台任务。
- 删号锁定目标用户，计数其全部作品（包括回收站/过期待清理），非零409；清空后仍有原自身/最后管理员护栏。数据库 RESTRICT 兜底并发和未来漏查，不能靠过滤正常作品的 count 判断。
- SQLite RESTRICT 可产生扩展错误787，或扩展错误1811且消息精确为 `FOREIGN KEY constraint failed`；count=0后并发创建的失败也须翻译为user_has_projects 409。无关自定义trigger或其他IntegrityError不能被笼统吞成该冲突。
- 管理员恢复/purge 通过显式 include_own_project 也记录自己的作品；普通本人创作和自主删除不扩大管理员采集范围。新动作 project.restore/purge/expire；project.delete 的新摘要标注 soft_delete，旧历史不改写。
- 系统 expire 的 actor_user_id=null、actor_username=系统自动清理，只有内部 system_expiry 入口允许；成功与业务同事务。摘要新增 lifecycle_action、retention_days、cancelled_task_count，不存正文/凭据；既有日期清理 nonce/高水位/保护契约不变。
- PROJECT_RULE.exclude 排除所有生命周期/协调列，导入总是正常作品，备份仍v1，APP_SCHEMA=0024；不允许恶意备份注入删除状态。

### 前端与兼容

`/admin/recycle-bin` 是 RequireAdmin lazy 路由，移动端卡片、桌面表格；精确名称 purge、忙碌防重复、旧响应/会话失效和末页回退。所有普通删除入口说明30天、管理员恢复及任务停止失败。具体组件/状态约定见前端 component/state-management 规范。

迁移必须验证带存量子数据的 SQLite 父表重建、外键完整性及审计 AUTOINCREMENT 高水位，不只测空库。downgrade 在有回收站/删除中作品或新增生命周期审计动作/摘要时拒绝，不能复活作品、删除审计或伪造系统 actor；上线前先备份，异常先停清理/停写再前滚修复。

## 4. 校验与错误矩阵

| 条件 | 结果 |
|---|---|
| 未登录/禁用/失效会话 | 401 |
| 普通用户访问回收站 | 403 |
| 普通入口访问已删作品、跨用户/跨项目子资源 | 404，不泄漏正文 |
| 已授权入口遇删除中 | 409，code=project_deleting |
| 非回收站目标 / 代次变化 | 409，project_not_deleted / recycle_record_stale |
| 恢复到期作品 | 409，recycle_expired；不因尚未物理清理而放行 |
| 目标已物理清理 | 404 |
| 名称不精确相同 | 400；不 trim/大小写折叠 |
| confirm 缺失/false/1、无时区、查询范围非法 | 422；不得写业务 |
| 任务取消/收尾失败或超时 | 409，project_tasks_not_stopped，作品未删除 |
| 有任何作品时删号 | 409，user_has_projects；通常附安全 project_count |
| 审计写失败 | 对应业务回滚；有可用数据库时另留安全失败 |

## 5. 正例 / 基准 / 反例

- 正例：Alice 删除作品→正常入口404→管理员恢复原ID→正文、版本、封面和阅读进度仍在；无自动模型调用。
- 基准：作品没有任务时直接完成软删；普通用户没有回收站入口，管理员也不能由正常工作区改回收站内容。
- 反例：只取消 running、看到 cancelled 就物理删除；queued/paused 的 on_abort 或 succeeded 的 on_success 仍可能写库。
- 反例：删号只查正常项目，或沿用 Project→User CASCADE；回收站记录仍会被绕过保护清空。

## 6. 必需测试

- `tests/test_recycle_bin.py`：完整关联数据往返、owner/admin/第三人、字面搜索/分页、禁用owner恢复、期限边界、系统清理/部分失败、审计原子性、旧代次/精确确认、备份注入拒绝、restore/purge竞争。
- `tests/test_recycle_bin_tasks.py`：排队/运行/待审核、成功派生与取消收尾、超时不删、执行链退出及失败重试；使用事件屏障而非 sleep 猜时序。
- `tests/test_project_lifecycle.py`：陈旧 identity map、普通已接纳写入与删除屏障、RESTRICT、启动遗留意图、重复锁保留未flush标量/JSON。
- 代次必须另测冻结时钟、回拨及独立session/重启：第一次删除的purge请求不能匹配恢复后新删除记录，即使物理时钟值完全相同。
- SQLite另测最高项目ID purge后新建同名作品不复用ID，旧请求不能误删新实体；迁移时不得回退既有审计AUTOINCREMENT序列。
- `tests/test_recycle_bin_migrations.py`：0023→0024存量父子行完整、FK/索引/约束、审计序列及受保护降级；原 admin/projects/tasks/open-api/transfer/audit 测试继续回归。
- 前端 API/utils/UI/store 测试覆盖原始微秒、精确名称（含Unicode超长UTF-16表示）、取消、旧响应失效、404撤下缓存及窄屏；真实浏览器验证恢复、危险确认取消/禁用和权限导航。
- SQLite不证明PostgreSQL行锁；未真实运行PG时必须在验收报告中明示缺口。

## 7. 错误写法 vs 正确写法

错误：只在读取时排除 deleted_at，然后无锁更新子资源；或读取 Task.status 就认为所有收尾完成。

```python
project = await get_manageable_project(db, actor_id, pid)
chapter.content = body.content
await db.commit()  # 删除可能已经在读取后完成
```

正确：在同一写事务中持有集中生命周期屏障，删除协调先提交准入意图、等真实执行链退出后再软删。

```python
project = await get_manageable_project(db, actor_id, pid, lock=True)
# 校验同项目子资源，修改并按既有业务/审计出口提交。
await audit.commit(flush_fn=version_service.flush_versioned)
```

错误：客户端把 deleted_at 经 `new Date(...).toISOString()` 回传，或给名称确认输入增加按UTF-16计数的200长度截断。

正确：原始 `project.deleted_at` 字符串回传；名称逐字比较当前完整标题，服务器再执行字符数、代次和权限校验。
