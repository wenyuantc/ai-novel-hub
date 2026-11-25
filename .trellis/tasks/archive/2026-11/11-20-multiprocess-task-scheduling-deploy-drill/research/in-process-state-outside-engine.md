# Research: 任务引擎之外的进程内状态与启动逻辑（多进程 / 多实例影响）

- **Query**: TODOS 第 25 项规划前调研——找出 `backend/app/` 中任务引擎目录之外，所有依赖「全站只有一个进程」的状态、启动步骤、周期协程、缓存、互斥与两步令牌，判断多 Uvicorn worker 或多实例共享数据库时各自的后果
- **Scope**: internal（只读代码与规范，未运行多进程实验）
- **Date**: 2026-11-20
- **约定**: 下文 A、B 指共享同一数据库的两个应用进程；「重新读库」指该进程再次从数据库取值；路径均相对 `backend/app/`，另有说明的除外

## 汇总表

严重度：S1 破坏正确性或安全 / S2 重复副作用或上限放大 / S3 仅体验降级或浪费 / S4 无影响。

| # | 名称 | 位置（文件:行号） | 类别 | 多进程后果 | 严重度 | 数据库层兜底 | PG 与 SQLite 是否不同 |
|---|---|---|---|---|---|---|---|
| 1 | 注册开关 `registration_enabled`（系统设置只发布到本进程） | `services/system_settings_service.py:60-69,93-100`；消费 `api/v1/auth.py:34`、`services/auth_service.py:57-59` | 缓存 | A 关闭注册后，B 的 `Settings` 仍是旧值，继续放行注册；没有定时重新读库，直到 B 重启，或管理员的设置页 GET / PATCH 恰好落到 B | **S1（安全门禁失效）** | 无，注册只看进程内布尔值 | 否 |
| 2 | MCP 总开关在路由、试用、工具发现处的门禁 | `services/mcp/service.py:52-54,296-298,344-346`；`api/v1/mcp_plugins.py:21-30`；`services/agent/mcp_tools.py:308,399` | 缓存 | A 关闭 MCP 后，B 仍允许插件管理、刷新工具（真实外连）和试用调用（真实 `call_tool`）；智能体轮次里的真实发送不受影响 | **S1（安全门禁失效）** | 部分：只有智能体真实发送在发送事务内读库（`services/mcp/settings_gate.py:70-108`） | 是：PG 加 advisory 共享锁后读库；SQLite 直接读库 |
| 3 | 备份运行器的启动恢复 | `services/backups/runtime.py:97-109,514-581` | 启动步骤 | B 启动时把所有 `active_slot=1`（排队或运行中）的备份无条件写成 interrupted，清理其临时目录和 GPG 目录，中止 S3 分片上传；A 正在执行的备份被破坏 | S1 | 无，`_write` 不带状态条件（`runtime.py:160-181`） | 否 |
| 4 | 备份执行的领取没有条件更新 | `services/backups/runtime.py:131-154,239-249` | 周期任务 | 每个进程的 `_loop` 每秒查同一条 queued 记录，先读后写 running；窗口内两个进程会同时领取同一次备份：后到者通常在建临时目录时失败，随即把运行行写成 failed、释放 `active_slot` 并发失败邮件，而先到者仍在执行，双方用无条件写互相覆盖行状态 | S1 | 部分：受理有 `active_slot` / `scheduled_date` / `request_id` 唯一键（`models/backup.py:67-79`），执行领取没有 | 否 |
| 5 | 取消 running 任务时的「孤立 running」分支 | `services/task_service.py:1300-1326,1705-1723` | 其他（HTTP 层读引擎内存） | 取消请求落在非执行进程时 `engine._running` 查不到执行体，被当成孤立任务：执行 `on_abort` 并把行改成 cancelled，而原进程的执行体仍在运行 | S1 | 部分：条件更新带任务 UUID、轮次、执行键；但「执行体是否还在」只有进程内证据 | 否 |
| 6 | 作品删除的排空依赖本进程执行证据 | `services/recycle_bin_service.py:131,186-200,221-243,502-503`；`services/task_service.py:1634-1702` | 互斥 | `engine.project_work` / `has_cleanup_failure` 看不到其他进程的执行体和请求侧收尾；删除会经第 5 项强制收尾他进程的任务后继续软删除 | S1 | 部分：未结束任务、`execution_key`、未决外部调用、未核对收据、`generating` 章节都查库；规范写明这些不充分（`recycle-bin.md:73`） | 否 |
| 7 | 外部调用的启动恢复 `recover_external(stale_only=False)` | `services/agent/service.py:147-154,205-212`（启动时由 `services/task_engine/engine.py:582` 调用） | 启动步骤 | B 启动时不看时限，把 A 正在进行的外部 MCP 调用提案（executing）判为失败、结果未知 | S1 | 条件更新加 claim 和 MAC 核对能防止改错行，不能区分「他进程仍在执行」 | 否 |
| 8 | 本地备份副本目录 | `services/backups/runtime.py:488-512`；`services/backups/storage.py:140-160,183-197` | 其他（主机本地文件） | 多主机且未共享 `BACKUP_DIR` 时：下载落到没有该文件的实例，副本被永久标为 missing；留存清理删不到他机文件 | S1（仅多主机不共享卷；同机多 worker 为 S4） | 无 | 否 |
| 9 | 启动时解除删除意图 `recover_pending_deletions` | `services/recycle_bin_service.py:507-515`；受影响方 `:113-177` | 启动步骤 | B 启动时无条件清空所有未删除作品的删除意图；A 进行中的删除在锁内核对 token 时以 409 失败，已被取消的任务不恢复 | S2 | 有：A 在父行锁内核对 `deletion_token`（`:146-148,194-196`），不会带着旧假设完成删除 | 否 |
| 10 | 灵感生成锁 `_generating` | `services/inspiration_service.py:93-95,489-499,557-559,588-604` | 互斥 | 同一会话同一步骤可在两个进程同时生成：重复模型调用；`_save_round` 无行锁无版本号、整体覆盖 JSON，后提交者覆盖先提交者的轮次 | S2 | 无 | 否 |
| 11 | 项目备份导入令牌缓存 | `services/project_transfer/importer.py:92-96,1401-1437`；`api/v1/transfer.py:61-93` | 令牌 | 预览与确认落在不同进程时返回 400「导入凭据已过期」；每个进程各自 6 份上限，内存占用上限乘以进程数 | S2 | 无（按设计存在进程内） | 否 |
| 12 | 系统设置其余键只发布到本进程 | 同第 1 项；消费 `services/analysis_service.py:953`、`services/batch_service.py:177`、`services/rich_document_review.py:1184`、`core/open_api_deps.py:22`、`services/quota_service.py:34,76` | 缓存 | B 继续按旧值：自动分析照常触发；开放接口按旧阈值放行；任务并发上限用旧值（引擎内的计数另计） | S2 | 部分：限流计数在库内（第 36 项），阈值没有 | 否 |
| 13 | `settings_confirmed` 标志 | `core/authorization_runtime.py:26`；`services/system_settings_service.py:43,67,198,207` | 缓存 | A 的设置提交结果未知而暂停新准入时，B 不知情，继续按自己的快照准入 | S2 | 仅 MCP 真实发送另外读库 | 否 |
| 14 | 智能体会话锁 `_locks` | `services/agent/service.py:68,331-337`；使用处 `:706,743,1357,1628,1648` | 互斥 | 跨进程不互斥。事务持有期间由数据库锁串行；外部调用「先提交 executing 再联网」这类跨提交窗口只剩持久状态标记 | S2（部分未核实） | 部分：授权门禁行、User 行、作品父行锁；`AgentConversation` 行锁（`:1385`）；活动轮次的部分唯一索引只含 queued / running，不含 paused（`models/entities.py:145,804-810`） | 是：PG 有会话行锁；SQLite 无行锁，靠全库写锁，争用时报 busy 而不是排队 |
| 15 | 数据库迁移随每个实例启动 | 仓库根 `Dockerfile:37`；`backend/alembic/env.py`（无跨实例锁） | 启动步骤 | 多实例同时启动会并发执行 `alembic upgrade head` | S2（具体失败形态未核实） | 个别迁移内有 `LOCK TABLE`，没有全局迁移锁 | 是：PG 为事务性 DDL；SQLite 用批量重建表 |
| 16 | CPU 并发限流器 | `services/novel_template_snapshot.py:126`、`services/novel_template_service.py:45`、`services/novel_template_apply.py:61`、`services/setting_image_codec.py:23`、`services/card_service.py:109`、`services/book_import/splitter.py:44` | 其他 | 每个进程各 2 个名额，总并发乘以进程数 | S2（上限放大，影响轻） | 不适用 | 否 |
| 17 | 回收站到期清理 | `services/recycle_bin_service.py:518-579` | 周期任务 | 每个进程启动时扫一次、之后每小时一次；同一作品由父行锁串行，后到者得到 NotFound / Conflict 并只记 warning；审计与删除同事务，不会重复 | S3 | 有 | 否 |
| 18 | 仪表盘采样 | `services/admin_dashboard_service.py:216-285` | 周期任务 | 每个进程每 60 秒 upsert 同一日的行，`captured_at` 较新者生效 | S3 | 有（`ON CONFLICT DO UPDATE ... WHERE`） | 否（各用本方言的 insert，语义相同） |
| 19 | 用量遗留调用收尾 | `services/ai_usage_recording.py:121-146`；`services/ai_usage_storage.py:555-566` | 周期任务 | 每个进程每小时把超过 1 天仍未结束的调用标为 interrupted；按时间阈值，不误伤他进程的近期调用 | S3 | 有（条件 UPDATE） | 否 |
| 20 | 备份计划受理 | `services/backups/service.py:373-493,503-509` | 周期任务 | 每个进程每秒尝试受理；同一天只会成功一条，后到者撞唯一键，被记成一条「备份调度暂时不可用」的 error 日志 | S3 | 有 | 否 |
| 21 | 协作撤权通知 | `services/collaboration_service.py:115-125`；`api/v1/collaboration.py:49-125` | 其他 | 只唤醒本进程的订阅；他进程上的协作者任务流要到下一帧或心跳才复核并关闭 | S3 | 有（规范写明每帧在租约内按库复核，`error-handling.md:426`；引擎侧实现不在本调研范围） | 未核实 |
| 22 | 任务进度与输出读本地 hub | `services/task_service.py:324-335,909-915`；`services/admin_task_service.py:101` | 其他 | 非执行进程拿不到实时进度和流式文本，回落到库内的节流值；「结果待核对」提示只反映本进程 | S3 | 有（库内节流值） | 否 |
| 23 | 配额页的已用并发与本地唤醒 | `services/quota_service.py:83,114` | 其他 | `used` 只统计本进程已准入的任务；`engine.notify()` 只唤醒本进程的引擎 | S3 | 无 | 否 |
| 24 | 公开开关 `/auth/config` | `api/v1/auth.py:26-29` | 缓存 | 各进程返回值不一致，前端 30 秒轮询会在新旧值之间来回变 | S3 | 无 | 否 |
| 25 | 分析、向导任务在非执行进程取消 | `services/task_service.py:1131-1147` | 其他 | 返回 409「执行结果尚未核对，不能取消」，取消不生效 | S3 | 不适用 | 否 |
| 26 | 备份模块的进程局部标志 | `services/backups/runtime.py:75,81-95`；`services/backups/service.py:139` | 缓存 | `_uncertain`、`confirmed`、工具就绪检查（`shutil.which`）各进程各算，概览页的「可用」随请求落点变化 | S3 | 不适用 | 否 |
| 27 | 登录态、角色、权限 | `core/deps.py:80-88`；`services/authorization_service.py:141-208,278-288` | 缓存（实际没有缓存） | 每个请求读库核对 `token_version`、`is_active`、角色与权限；改角色、禁用、改密对所有进程立即生效 | S4 | 有 | 否 |
| 28 | `AuthorizationRuntime` 进程内可重入锁 | `core/authorization_runtime.py:20-52`；`core/database.py:57`；`services/authorization_service.py:105-127` | 互斥 | 只在进程内排队；跨进程的串行由 `authorization_state` 单例行的空更新承担 | S4 | 有 | 是：PG 为行锁，可重复读隔离下先 `LOCK TABLE`；SQLite 为全库写锁 |
| 29 | `authorization_service.initialize` | `services/authorization_service.py:80-89` | 启动步骤 | 迁移 0029 已写入状态行和 4 个内置角色，正常情况下不写任何东西 | S4（若代码新增内置角色却没有迁移，并发首次启动会撞唯一键使其中一个进程启动失败） | 有 | 否 |
| 30 | 启动时的 `system_settings.load` | `services/system_settings_service.py:102-109`；`main.py:115` | 启动步骤 | 只读库并发布到本进程 | S4 | 不适用 | PG 多取一把 advisory 共享锁 |
| 31 | `bootstrap_admin` | `services/auth_service.py:143-184` | 启动步骤 | 在授权门禁下先查后写；账号已是启用中的超管就直接返回；撞用户名唯一键时重试一次 | S4 | 有 | 否 |
| 32 | `sync_presets` | `services/style_service.py:316-335` | 启动步骤 | 先读后写；并发首次启动撞 `preset_key` 唯一键时异常被吞掉，只记日志 | S4 | 有（`models/entities.py:1014`） | 否 |
| 33 | `purge_expired_all` | `services/book_import/service.py:69-78` | 启动步骤 | 按到期时间删除，可重复执行 | S4 | 有 | 否 |
| 34 | `initialize_tracking` | `services/task_statistics_service.py:24-41` | 启动步骤 | `ON CONFLICT DO NOTHING` 加单例行串行；只给没有统计行的任务补基线记录 | S4 | 有 | 是：PG 用 `FOR UPDATE` 串行；SQLite 靠 INSERT 取得的写事务 |
| 35 | 备份设置快照 | `services/backups/service.py:175-204` | 缓存 | 每次读取、受理、保存前都重新读库 | S4 | 有（revision 条件更新） | 否 |
| 36 | 开放接口限流计数 | `services/api_key_service.py:200-242` | 其他 | 有效性检查、分钟窗口和计数在一条条件 UPDATE 内完成，所有进程共享 | S4（阈值见第 12 项） | 有 | 否 |
| 37 | 作品数量配额 | `services/quota_service.py:41-51` | 其他 | 锁 User 行后读库计数 | S4 | 有 | 否 |
| 38 | AI 可见性写锁与发送租约 | `services/ai_dispatch.py:338-405` | 互斥 | 是数据库层的锁，跨进程有效 | S4 | 有 | 是：PG 用 `pg_try_advisory_xact_lock(_shared)` 轮询最多 2 秒；SQLite 用空更新取全库写锁 |
| 39 | 章节抢占 | `services/chapter_claim.py:252-316,443-480` | 互斥 | 占用身份存库，靠条件 UPDATE 和影响行数判定 | S4 | 有 | 未逐条核对方言分支 |
| 40 | 备份受理的单飞 | `services/backups/service.py:432-435,503-509` | 互斥 | `active_slot` 唯一键保证同时只有一条排队或运行中的备份 | S4 | 有 | 否 |
| 41 | 检索索引缓存 | `services/retrieval_service.py:1715-1746` | 缓存 | 每次加载先查库内签名再决定是否复用；`invalidate()` 只清本进程 | S4 | 有 | 否 |
| 42 | 提及匹配缓存 | `services/mentions/matcher.py:248-314` | 缓存 | 键含正文哈希和词典指纹，是纯计算结果 | S4 | 不适用 | 否 |
| 43 | 无状态令牌（缩略图 AI、审计清理预览、全书搜索） | `services/thumbnail_ai_tokens.py:14-31`；`services/audit_service.py:143-171`；`services/book_search.py:145` | 令牌 | 令牌自带加密或签名，任何进程都能校验（前提是各进程的 `ENCRYPTION_KEY`、`JWT_SECRET` 相同） | S4 | 不适用 | 否 |
| 44 | 拆书导入两步 | `services/book_import/service.py:81-89,292-379` | 令牌（存库） | 解析记录存库；开始导入时锁行并核对 revision，同事务删除记录 | S4 | 有 | 是：SQLite 忽略 `FOR UPDATE` |
| 45 | 调用或任务作用域内的登记表 | `services/ai_dispatch.py:95,105,448`；`services/project_ai_config.py:132`；`services/agent/mcp_tools.py:569-594`；`services/mcp/client.py:83` | 其他 | 以对象身份为键，生命周期不跨请求 | S4 | 不适用 | 否 |
| 46 | 纯函数缓存 | `core/config.py:120`；`ai/templating.py:265`；`services/project_transfer/importer.py:636`；`api/open/docs.py:41` | 缓存 | 部署配置、模板编译、类型形状、开放接口文档 | S4 | 不适用 | 否 |
| 47 | HTTP 层调用本进程引擎的其余入口（订阅任务流、管理员取消、创建后登记 hub 与唤醒） | `api/v1/tasks.py:60-88`；`services/admin_task_service.py:256`；`services/task_service.py:657-662,1334-1342` | 其他 | 请求落在非执行进程时的表现取决于引擎内部 | 未核实（引擎内部，由引擎调研覆盖） | 未核实 | 未核实 |

## 最重要的结论：过期读是否造成越权

没有发现按进程缓存的角色、权限、登录态或配额。`get_current_user`（`core/deps.py:80-88`）每个请求读 `User` 行核对 `token_version` 与 `is_active`；`authorization_service.require`（`:278-288`）每次用一条 SQL 取用户、角色和 `AuthorizationState.revision`（`snapshot_statement`，`:141-158`，带 `populate_existing`）。任务提示里称作「授权 runtime」的对象不是权限缓存，而是一把进程内可重入锁加一个 `settings_confirmed` 布尔值（`core/authorization_runtime.py:20-52`）。

因过期读而失效的门禁只有两处，都来自系统设置只发布到本进程的 `Settings` 对象（第 1、2 项）：注册开关，以及 MCP 总开关在路由、试用和工具发现处的检查。两者都没有时间上限，旧值会一直保留到该进程重启或恰好处理一次设置页请求。

## S1：破坏正确性或安全

### 1. 注册开关（系统设置只发布到本进程）

证据：
- `SystemSettingsManager._publish`（`services/system_settings_service.py:60-69`）用 `setattr` 把六个键写进本进程的 `Settings` 对象，然后调用本进程的 `notify`。
- `_reload` 只在四处被调用：`load()`（启动 `main.py:115`，以及 `GET /admin/system-settings`，`api/v1/admin_system_settings.py:16-21`）、`_update()` 开头（`:133`）、写失败后的即时重读（`:200`）、`_recover()` 循环（`:216`，只在本进程状态不确定时运行）。没有定时器，也没有按 revision 的例行比对。
- 注册入口把进程内的值直接传进去：`api/v1/auth.py:34` → `auth_service.register(..., registration_enabled=settings.registration_enabled)`，`services/auth_service.py:57-59` 只判断这个布尔值。
- 规范已写明这是当前契约：`.trellis/spec/backend/system-settings.md:5`「继续沿用任务引擎已有的单进程部署前提，不提供多worker广播」。

推理：管理员在 A 上关闭注册并得到成功响应后，落到 B 的 `POST /auth/register` 仍然成功。写路径本身在多进程下是安全的（`_update` 先重读，再做 `revision` 条件更新，`:183-191`），缺的只是把结果传给其他进程。

若要支持多进程，这里最少需要：让每个进程在使用这类门禁值之前能得知库内 revision 已变化（或直接读库判定）。

### 2. MCP 总开关在路由、试用、工具发现处的门禁

证据：
- 路由依赖 `require_mcp` → `ensure_enabled(settings)`（`api/v1/mcp_plugins.py:21-22,30`；`services/mcp/service.py:52-54`）。
- 刷新工具 `refresh_tools`（`services/mcp/service.py:281-316`）和试用调用 `call_once`（`:338-347`）在连接前后各检查一次，检查的都是进程内的 `settings.mcp_enabled`，随后发起真实外连。
- 智能体轮次的真实发送是另一条路：`_send_lease` 调 `settings_gate.acquire_send`（`services/agent/mcp_tools.py:186`），在发送事务里读 `system_settings` 行；代码注释写着「不能依赖跨实例过期的内存开关」（`mcp_tools.py:185`）和「不采信可能陈旧的进程快照」（`mcp_tools.py:350`）。
- `build_tool_map` 与 `tool_specs`（`mcp_tools.py:308,399`）仍读进程内的值，只影响是否向模型暴露工具，不影响真实发送的门禁。

推理：A 关闭 MCP 后，B 上的用户仍可创建和导入插件、刷新工具列表、试用调用工具，都是带用户自定义请求头的真实出站请求。智能体侧的真实调用会被数据库门禁拦住。

若要支持多进程，这里最少需要：试用和工具发现路径在外连前采用与智能体真实发送相同的读库判定，或依赖第 1 项的跨进程生效机制。

### 3. 备份运行器的启动恢复

证据：
- `BackupRuntime.start`（`services/backups/runtime.py:97-109`）先 `manager.load()` 再 `_recover()`。
- `_recover`（`:514-581`）取出所有 `active_slot == 1` 的 `BackupRun`，逐条：核验或回退其产物（`_recover_artifact`，其中会对带 `multipart_upload_id` 的 S3 产物调用 `store.abort`，`:610-617`）、清理 GPG 目录（`_clean_encryption_home`）、删除 `.work-<execution_key>` 临时目录（`clean_temporary`）、最后把行写成 `status="interrupted", active_slot=None`（`:568-580`）。
- `_write`（`:160-181`）是「按主键取行、逐字段赋值、提交」，没有状态或执行键条件。

推理：这与任务引擎启动恢复是同一类问题。B 启动或重启时，A 正在执行的备份会被标成中断，它的明文快照目录被删、加密代理被回收、分片上传被中止；A 的执行体随后失败或继续用无条件写覆盖 B 写入的终态。

若要支持多进程，这里最少需要：恢复只处理能证明已无执行者的记录（例如执行者身份加存活证明）。

### 4. 备份执行的领取没有条件更新

证据：
- `_loop`（`services/backups/runtime.py:131-154`）每次循环先 `accept_scheduled()`，再查一条 `status == "queued" and active_slot == 1` 的记录并 `_execute`，随后最多等 1 秒（或被本进程的 `notify` 唤醒）。
- `_execute`（`:239-249`）先 `_read` 核对状态为 queued，再 `_write(status="running")`，两步之间没有条件更新。
- `manager.lock` 是 `asyncio.Lock`（`services/backups/service.py:137`），只在进程内有效。

推理：受理方进程被 `notify` 立即唤醒，其他进程在 1 秒内轮询到同一行。若它们在受理方写入 running 之前读到 queued，就会并行执行同一 `execution_key`。`temporary_directory` 用 `os.mkdir` 建同名目录，先到者已建好时后到者得到 `storage_failed`（`services/backups/storage.py:202-215`）。后到者此时 `temporary` 仍为 None，不会删临时目录，但它的 `finally` 仍会回收该次执行的 GPG 目录（加密备份时，`runtime.py:323-327`），并经 `_finish` 把产物和运行行写成 failed、释放 `active_slot`、按配置发送失败邮件（`:429-454`）；先到者仍在导出，之后又用无条件写把行改回自己的结果。窗口很短，但没有任何数据库层的阻挡。

若要支持多进程，这里最少需要：把 queued 到 running 的转换改成带条件的原子领取，或保证全站只有一个进程运行备份循环。

### 5. 取消 running 任务时的「孤立 running」分支

证据（`services/task_service.py:1300-1326`）：

```python
if task.status == "running":
    await db.commit()
    execution = engine._running.get(task.id)
    if execution is None or execution.done():
        # 结束状态落库曾失败的孤立 running；单进程中已无执行体，可重试幂等收尾。
        await _abort_loaded_task(engine, task)
        ...
        result = await db.execute(update(Task).where(
            Task.id == task.id, Task.statistics_key == task.statistics_key, Task.status == "running",
            Task.queued_at == expected_queued_at, Task.execution_key == task.execution_key,
            Task.admin_cancel_operation_id.is_(None),
        ).values(status="cancelled", ..., execution_key=None))
```

`_abort_loaded_task`（`:1705-1723`）对章节类任务同样只查本进程的 `engine._running`，查不到就调用 `handler.on_abort`。

推理：`POST /tasks/{id}/cancel` 落在非执行进程时，本进程查不到执行体，于是走这条分支：先做 `on_abort` 清理，再把任务行改成 cancelled 并清空执行键。原进程的执行体没有被取消，会继续消耗模型调用，直到它下一次带执行键的数据库核对失败。清理与执行并发发生。`stop_project_tasks`（`:1634-1702`）对作品下每个未结束任务调用同一函数，所以作品删除也会触发它。

若要支持多进程，这里最少需要：取消请求能判断任务是否由其他存活进程执行，并把取消交给该进程，而不是按孤立任务收尾。

### 6. 作品删除的排空依赖本进程执行证据

证据：
- `move_to_recycle_bin`（`services/recycle_bin_service.py:113-177`）在 `:131` 用 `engine.project_work(project_id)` 判断是否还有机会等到收尾；`_wait_for_external_work`（`:180-204`）等待的是本进程的 work 集合；`_ensure_idle`（`:221-243`）先看 `engine.project_work` 与 `engine.has_cleanup_failure`，再查库。
- 规范明确数据库事实不够：`.trellis/spec/backend/recycle-bin.md:73`「必须等待真实运行体、请求侧取消清理和成功回调；数据库任务终态、`cancel_running` 返回或活动任务数量为零都不是充分条件。引擎 `cleanup_scope` / `project_work` 也覆盖请求侧收尾」。
- 彻底删除后的 `engine.forget_project_hubs`（`:502-503`）和 `retrieval_service.invalidate`（`:504`）只作用于本进程。

推理：A 处理删除请求时，看不到 B 上该作品的执行体，也看不到 B 上正在进行的请求侧收尾（`cleanup_scope`）以及 B 内存里记着的收尾失败。数据库检查能挡住仍带 `execution_key` 或未结束状态的任务，但经过第 5 项之后这些行已被 A 改成 cancelled，检查随之通过。B 内存里的收尾失败记录是否总有数据库可见的对应事实，属于引擎内部，未核实。

若要支持多进程，这里最少需要：把「该作品在任何进程都没有执行体和请求侧收尾」变成跨进程可查的事实。

### 7. 外部调用的启动恢复

证据：
- `recover_external` 的说明（`services/agent/service.py:151-154`）：「stale_only=False 用于进程启动：单进程部署下启动时不可能有正在进行的确认请求」。启动调用点在 `services/task_engine/engine.py:582`。
- `stale_only=True` 的路径有两道保护：`executed_at` 超过 2 分钟（`EXTERNAL_STALE_AFTER`，`:73`；`:205`），以及本进程没有该作品的执行或收尾工作（`:207-212`）。`stale_only=False` 两道都跳过。
- 规范同样写着这个前提：`.trellis/spec/backend/skills-mcp.md:533`。

推理：A 正在等待一次外部 MCP 调用返回（提案处于 executing，事务已提交）时 B 启动，B 会把该提案改成 failed 并标记结果未知。A 的调用其实可能成功，记录与外部系统的真实结果不一致。`stale_only=True` 路径在跨进程时只剩 2 分钟时限这一道保护（单次调用上限约 50 秒）。

若要支持多进程，这里最少需要：启动恢复也遵守时限，或只处理能证明执行者已不存在的提案。

### 8. 本地备份副本目录（仅多主机）

证据：`prepare_download`（`services/backups/runtime.py:488-512`）打开本地文件失败且错误为 `storage_missing` 时，把产物写成 `status="missing"` 并返回「备份副本已不存在」；`LocalStore.open`（`services/backups/storage.py:140-160`）对 `FileNotFoundError` 抛的正是 `storage_missing`。`LocalStore.delete`（`:183-197`）在文件不存在时直接返回，调用方随后把产物记为 deleted。

推理：同一容器内多 worker，或多实例挂载同一个 `BACKUP_DIR`，不受影响。实例分布在不同主机且各用各的本地目录时，下载请求落到没有文件的实例，会把一份实际存在的副本永久标成 missing；留存清理也会把删不到的他机文件记成已删除。

若要支持多进程，这里最少需要：部署文档要求所有实例共享同一备份目录，或限定备份相关请求只由持有文件的实例处理。

## S2：重复副作用或上限放大

### 9. 启动时解除删除意图

证据：`recover_pending_deletions`（`services/recycle_bin_service.py:507-515`）对所有 `deleted_at IS NULL` 的作品执行 `deletion_started_at=None, deletion_token=None`，没有时间或归属条件；函数说明写着「仅在单进程启动、引擎尚未运行时调用」。删除流程在提交意图后（`:133-137`）会等待并取消任务，再在父行锁内核对 `project.deletion_token != token`（`:146-148`），不一致就抛 409「作品状态已变化，请刷新后重试」。

推理：B 启动会清掉 A 进行中的删除意图。A 不会带着旧假设完成删除，但这次删除失败，而它已经取消的任务不会恢复；在 A 发现之前，意图原本冻结的新写入和新任务又被放开。PostgreSQL 下这条 UPDATE 还会对所有未删除作品取行锁，需等待持有作品行锁的事务结束。

若要支持多进程，这里最少需要：只清理能证明发起者已不存在的意图（例如按意图年龄或发起者身份）。

### 10. 灵感生成锁

证据：`_generating` 是模块级字典，注释「进程内的生成锁（单进程部署）」（`services/inspiration_service.py:93-95`）；`_acquire` / `_release`（`:489-499`）；占锁点在全部校验之后（`:557-559`），释放在 `run_generate` 的 `finally`（`:642-643`）。`_save_round`（`:588-604`）普通 SELECT 读会话，追加一轮后整体写回 `state` JSON，没有 `FOR UPDATE`，也没有版本条件。规范：`.trellis/spec/backend/inspiration.md:107-108`「依赖单进程部署（同任务引擎）」。

推理：同一会话同一步骤的两次生成请求落在不同进程时都会通过，产生两次模型调用；两边各自读到旧的轮次列表再整体写回，后提交者会覆盖先提交者追加的那一轮。影响限于用户自己的会话。

若要支持多进程，这里最少需要：把「该步骤正在生成」放到所有进程都能看到的地方，或让保存轮次时能发现并发修改。

### 11. 项目备份导入令牌缓存

证据：`_CACHE: dict[str, _Entry]`（`services/project_transfer/importer.py:1401`）保存解析后的整个 `Backup` 对象；`TOKEN_TTL = 600`、每用户 2 份、全进程 6 份（`:92-95`）；`get_token` 找不到就抛 400「导入凭据已过期，请重新选择备份文件」（`:1428-1433`）。规范：`.trellis/spec/backend/project-transfer.md:111`「进程内缓存（单进程前提，同 tasks）」。前端只走令牌路径（`frontend/src/api/transfer.ts:27`），过期时提示重新选择文件（`frontend/src/components/transfer/ImportBackupModal.tsx:35`）。

推理：预览和确认落在不同进程时，确认一定失败，按过期处理，不会导入错误数据。无会话粘滞时每次尝试约有 (N-1)/N 的概率失败。备份文件最大 100MB，每个进程各缓存最多 6 份解析结果，内存上限乘以进程数。接口本身支持 multipart 直接导入（`api/v1/transfer.py:75-82`），不依赖令牌。

若要支持多进程，这里最少需要：让确认请求能在任意进程取得预览时的解析结果，或改用不依赖进程内状态的确认方式。

### 12. 系统设置其余键

证据与第 1 项相同，消费点如下：
- `auto_analyze_enabled`：`services/analysis_service.py:953`（`enqueue_auto`）、`services/batch_service.py:177`、`services/rich_document_review.py:1184`。B 用旧值时，关闭后仍会排入自动分析任务，产生模型费用。
- `open_api_rate_limit_per_minute`：`core/open_api_deps.py:22` 把进程内阈值传给 `authenticate_key`。计数在库内共享，但每个进程用自己的阈值做条件判断，调低后落到旧值进程的请求仍按旧阈值放行。
- `task_max_concurrency` / `task_max_per_user`：`services/quota_service.py:34,76` 用于解析与展示；真正的准入在引擎内部，不在本调研范围。

若要支持多进程，这里最少需要：同第 1 项。

### 13. `settings_confirmed` 标志

证据：标志挂在每个进程自己的 `AuthorizationRuntime` 上（`core/authorization_runtime.py:26`）。管理器构造时置为 False（`services/system_settings_service.py:43`），成功发布后置为 True（`:67`），提交结果不明时置回 False（`:198,207`）。规范：`.trellis/spec/backend/rbac-quotas.md:77`「提交事实未知时 `settings_confirmed=False` 暂停新准入……继续沿用单进程TaskEngine，不宣称多实例租约」。

推理：这个标志表达的是「本进程发布的值与库内一致」。A 的提交结果不明时只有 A 暂停；B 的标志仍为 True，而 B 的值可能已经过期。

若要支持多进程，这里最少需要：同第 1 项，另需明确「设置待确认」是否要让所有进程都暂停准入。

### 14. 智能体会话锁

证据：
- `conversation_lock`（`services/agent/service.py:331-337`）注释：「SQLite 无 FOR UPDATE；单进程任务引擎再以会话锁串行化 HTTP 操作」。五处使用：删除会话（`:706`）、发送消息（`:743`）、确认或拒绝提案（`:1357`）、撤销单项（`:1628`）、撤销整轮（`:1648`）。
- 锁内都会走 `lock=True` 的入口，取得数据库层的锁：授权门禁行（`services/agent/access.py:60-69` → `authorization_service.gate`）、User 行、作品父行（`services/project_access.py:90-94`）。确认路径另对会话行加 `FOR UPDATE`（`:1385`）。
- 「一个会话只有一个活动轮次」的检查是 `active_turn`（`:392-401`），覆盖 queued、running、paused；而任务表的部分唯一索引条件是 `status IN ('queued', 'running') AND resource_key IS NOT NULL`（`models/entities.py:145,804-810`），不含 paused。
- 外部调用提案的确认会先提交 executing 再联网（`:71-72` 的注释；规范 `skills-mcp.md:525`），这期间数据库锁已释放，只有进程内会话锁还持有。

推理：只要事务还开着，跨进程的并发由数据库锁串行，PostgreSQL 与 SQLite 都成立。事务中途提交之后（外部调用进行中、提交后刷新与续跑之间），其他进程上的同会话操作不再被挡住，只能靠持久状态标记兜底（提案 `status == "executing"` 时再次确认得到 409；轮次任务的状态条件更新）。哪些不变量在这些窗口内只靠进程内锁维持，没有逐一核实。SQLite 下，原本在进程内排队的同会话请求，在多进程时会变成等待全库写锁，超时即报 busy。

若要支持多进程，这里最少需要：逐一核对上述五个入口在事务中途提交后的窗口，确认每个不变量都有持久状态条件兜底。

### 15. 数据库迁移随每个实例启动

证据：仓库根 `Dockerfile:37` 的启动命令是 `alembic upgrade head && uvicorn app.main:app ...`。`backend/alembic/env.py` 没有 advisory 锁或其他跨实例互斥；部分迁移在内部对具体表执行 `LOCK TABLE ... IN ACCESS EXCLUSIVE MODE`（例如 `0061_ai_usage.py:167`）。

推理：多个容器同时启动会并发执行迁移。同一容器里用 `--workers` 起多个 worker 时迁移只在启动命令里执行一次，不受影响。并发迁移的具体失败形态没有实验验证。

若要支持多进程，这里最少需要：把迁移从应用启动命令里分离，或保证同一时刻只有一个实例执行迁移。

### 16. CPU 并发限流器

证据：五个 `anyio.CapacityLimiter(2)` 和一个 `threading.BoundedSemaphore(2)`（位置见汇总表），用于图片归一化、模板快照校验、设定卡解析和自定义正则子进程。

推理：这些上限按进程计，总并发乘以进程数。它们限制的是本进程线程池的 CPU 占用，而每个进程有自己的线程池，所以影响较轻。

若要支持多进程，这里最少需要：确认这些上限按进程计是可接受的即可。

## S3：仅体验降级或浪费

### 17. 回收站到期清理

`RecycleBinCleaner.start`（`services/recycle_bin_service.py:554-559`）启动时先跑一遍，之后每 `CLEANUP_INTERVAL = 3600` 秒一次（`:48`）。`purge` 先用空更新锁父行（`project_lifecycle.lock_record`），核对 `deleted_at` 与到期时间，审计和删除在同一事务提交；`purge_expired` 捕获 `ConflictError` / `NotFoundError` 只记 warning（`:540-541`）。两个进程清理同一作品时后到者等行锁，随后发现行已不存在。结果正确，多出的只是重复扫描和 warning 日志。它调用的 `_ensure_idle` 仍依赖本进程引擎证据（见第 6 项），但进入回收站的作品在软删除时已排空。

若要支持多进程，这里最少需要：无；可选只让一个进程运行。

### 18. 仪表盘采样

`collect_snapshot`（`services/admin_dashboard_service.py:216-241`）按日 upsert，更新条件是已存的 `captured_at` 不晚于新值。类注释写着「单进程采样协程」（`:245`），但写入本身可重复执行。规范 `.trellis/spec/backend/admin-dashboard.md:105` 的限制是「部署仍要求单进程，不支持新旧版本同时调度」，针对的是任务统计的连续性。

若要支持多进程，这里最少需要：无。

### 19. 用量遗留调用收尾

`usage_maintenance`（`services/ai_usage_recording.py:132-146`）启动时和每小时调用 `recover_stale_usage`，只处理开始时间早于 1 天的未结束调用（`_PENDING_MAX_AGE`，`:23`）。函数说明已考虑多实例：「保持其他实例近期发送的记录可收尾」（`:122`）。

若要支持多进程，这里最少需要：无。

### 20. 备份计划受理

`accept_scheduled` → `_accept`（`services/backups/service.py:373-493`）每次都先重新读库取设置，再查当天是否已有 `scheduled_date` 相同的记录、是否已有 `active_slot == 1` 的记录，然后插入。`scheduled_date` 与 `active_slot` 都有唯一约束（`models/backup.py:68,79`），`_insert_run` 把冲突转成 `ConflictError`（`:503-509`）。两个进程同时到点时只有一个插入成功；另一个的异常在 `_loop` 里被 `except Exception` 接住，记一条「备份调度暂时不可用，将重新核实持久状态」的 error 日志（`services/backups/runtime.py:148-150`），下一轮看到当天已有记录就只推进 `next_scheduled_at`。

若要支持多进程，这里最少需要：无；日志措辞会误导排障。

### 21. 协作撤权通知

`_notify_project_change`（`services/collaboration_service.py:115-125`）说明是「仅发无内容的本进程唤醒」；回调是 `engine.notify_collaboration_change`（`api/v1/collaboration.py:49-125`）。规范 `.trellis/spec/backend/error-handling.md:426`：「跨实例 / 无通知时由下一帧租约或心跳复核失败关闭」，并列出 PostgreSQL 回归 `test_postgres_http_stream_rechecks_after_cross_instance_revoke`。撤权是否生效由每帧的数据库复核决定，通知只影响关闭的及时性。帧租约的实现在引擎目录内，本调研未读。

若要支持多进程，这里最少需要：无（接受最长一个心跳周期的延迟）。

### 22 至 26（简述）

- 第 22 项：`to_out`（`services/task_service.py:324-335`）、`get_output`（`:909-915`）和管理端任务列表（`services/admin_task_service.py:101`）优先用本进程 hub 的实时进度与文本，取不到时用库内值；代码注释说明库内值是节流刷新的。最少需要：无，或让读请求落到执行进程。
- 第 23 项：`get_quotas` 的 `used=engine.admitted_count(user_id)`（`services/quota_service.py:83`）只算本进程；`update_quotas` 后的 `engine.notify()`（`:114`）只唤醒本进程。最少需要：展示口径说明或跨进程计数（取决于引擎方案）。
- 第 24 项：`GET /auth/config`（`api/v1/auth.py:26-29`）返回进程内的两个开关；前端 `PublicConfigController` 每 30 秒轮询（`system-settings.md` 前端一节）。最少需要：同第 1 项。
- 第 25 项：`chapter.analyze` 与 `wizard.*` 的 running 任务在本进程查不到执行体时直接 409（`services/task_service.py:1131-1147`），不走第 5 项的孤立分支。最少需要：同第 5 项。
- 第 26 项：`BackupRuntime._uncertain`（`runtime.py:75`）、`BackupManager.confirmed`（`service.py:139`）和 `tool_readiness`（`runtime.py:81-95`，用 `shutil.which` 检查本机工具）都按进程或主机计算。最少需要：各实例的备份工具与部署配置保持一致。

## S4：无影响（已核实有数据库层保证或不跨请求）

- **第 27、28 项**：见「最重要的结论」。`gate()`（`services/authorization_service.py:105-127`）先取进程内租约，再对 `authorization_state` 单例行做空更新，持有到事务结束；注释已提到「其他实例的门禁行锁」（`:113-114`）。
- **第 29 至 34 项（启动步骤）**：逐项结论见汇总表。共同点是要么只读，要么有唯一键或单例行串行，重复执行不改变结果。`bootstrap_admin` 的既有行为是把指定账号确保为启用中的超管（每次进程启动都会执行），多进程下语义不变。
- **第 35、40 项**：备份设置每次操作前重新读库（`_load_locked`，`services/backups/service.py:180-187`）；保存用 revision 条件更新（`:318-331`）。
- **第 36 项**：规范 `.trellis/spec/backend/open-api.md:71` 明确「禁止 Python 先读后加或进程内计数」，代码与之一致。
- **第 38 项**：`lock_visibility_write` 与发送租约都通过 `_lock`（`services/ai_dispatch.py:379-398`）落到数据库层的锁。
- **第 41 项**：`_load_index`（`services/retrieval_service.py:1743-1746`）每次先算 `(行数, max(id), sum(version), sum(chapter_no))` 签名再比较。`invalidate()` 的显式调用点（回收站三处、`services/agent/effects.py:1616`）只清本进程；其他进程是否仍能靠签名发现变化，与单进程下其他写入口的情况相同。
- **第 43 项**：三种令牌分别是 Fernet 加密（含用途与过期时间）、HS256 JWT、以 `JWT_SECRET` 派生密钥计算的 HMAC（`main.py:152`）。
- **第 45 项**：`_ACTIVE_SEND_LEASES`、`_CHAPTER_TRANSPORTS`、`_CHAPTER_GUARDS`、`_PREFLIGHTS` 以会话或 Provider 对象为键，用来证明「这次调用确实来自受控路径」；`McpSessions._workers` 属于一次 `agent.turn` 运行；`_test_connectors` 只登记测试替身。
- **二进制内容**：封面、缩略图、模板图片存在数据库的 `LargeBinary` 列（`models/setting_images.py:35`、`models/novel_templates.py:53`、`models/entities.py:300`），不依赖本地磁盘。除备份目录外，没有发现写本地文件系统的业务路径。

## 同一请求链路跨进程的两步流程

| 流程 | 第一步 | 第二步 | 状态存放 | 两步落在不同进程时的表现 |
|---|---|---|---|---|
| 项目备份导入 | `POST /projects/import/preview` | `POST /projects/import`（JSON 带 token） | 进程内 `_CACHE` | 确认返回 400「导入凭据已过期」，不写库（第 11 项） |
| 任务：创建后取消 | 任意创建入口（写库，并在本进程登记 hub） | `POST /tasks/{id}/cancel` | 任务行在库；执行体在执行进程内存 | running 任务被当成孤立任务收尾，原执行体继续运行（第 5 项）；分析与向导任务返回 409（第 25 项） |
| 任务：创建后订阅流 | 同上 | `GET /tasks/{id}/stream` → 本进程 `engine.subscribe`（`api/v1/tasks.py:60-88`） | hub 在执行进程内存 | 未核实（引擎内部） |
| 任务：管理员取消 | 受理并写入持久取消意图 | `engine.request_admin_cancel`（`services/admin_task_service.py:256`、`services/task_service.py:1334-1342`） | 意图在库；收尾在本进程引擎 | 未核实（引擎内部） |
| 作品删除 | 删除请求在 A | 该作品的任务在 B 执行 | 删除意图在库；执行证据在各进程内存 | A 看不到 B 的执行体，按孤立任务收尾后软删除（第 5、6 项） |
| 外部调用提案确认 | A 提交 executing 并联网 | B 上对同一会话的其他操作，或 B 启动 | 提案状态在库；会话锁在进程内 | 同一提案再次确认得到 409；B 启动会把它判为失败（第 7、14 项） |
| 系统设置 | `PATCH /admin/system-settings` 在 A | 之后的业务请求在 B | 库内一行，加各进程的 `Settings` 对象 | B 按旧值执行，直到它自己重新读库（第 1、2、12 项）；设置页的 GET 落到哪个进程，哪个进程就会刷新 |
| 整库备份 | 手动受理在 A（唯一键保证单条） | 任一进程的 `_loop` 执行；下载在 B | 记录在库；文件在执行主机的 `BACKUP_DIR` | 执行可能被两个进程同时领取（第 4 项）；多主机不共享目录时下载把副本标成 missing（第 8 项） |
| 灵感选项生成 | A 上生成进行中（同步 SSE） | B 上再次生成同一步骤 | 进程内 `_generating` | 两次都执行，轮次可能互相覆盖（第 10 项） |
| 协作撤权 | 作者在 A 撤权 | 协作者的任务流在 B | 成员关系在库；订阅在进程内存 | 下一帧或心跳时关闭（第 21 项） |
| 缩略图 AI 生成后应用 | `.../thumbnail/ai/generate` | `.../thumbnail/ai/apply` | 加密令牌由客户端带回 | 正常（第 43 项） |
| 审计清理 | `POST .../cleanup-preview` | 清理请求带 `preview_token` | 签名令牌由客户端带回 | 正常（第 43 项） |
| 全书搜索替换 | `POST .../preview` | `POST .../apply` | HMAC 由客户端带回 | 正常（第 43 项） |
| 拆书导入 | 上传与调整分章 | 开始导入 | `book_imports` 行 | 正常（第 44 项） |
| 富文档提案审核 | 任务暂停并产生提案 | `/tasks/{id}/rich-proposals/{token}/accept` | 令牌在任务 `checkpoint`（`services/task_service.py:310-317` 读取） | 令牌本身在库内；后续续跑是否依赖执行进程未核实 |

## Related Specs

- `.trellis/spec/backend/system-settings.md:5,47` — 六项设置的发布契约，明确不提供多 worker 广播；MCP 各检查点读共享 `Settings`
- `.trellis/spec/backend/rbac-quotas.md:73,77` — 共享 runtime、`settings_confirmed`、「不宣称多实例租约」
- `.trellis/spec/backend/recycle-bin.md:7,73,76` — 删除排空必须等真实运行体；遗留意图只在单进程启动时解除
- `.trellis/spec/backend/inspiration.md:107-108` — 进程内生成锁
- `.trellis/spec/backend/project-transfer.md:111` — 进程内导入令牌
- `.trellis/spec/backend/skills-mcp.md:525-536` — 外部调用恢复的触发点与 2 分钟时限
- `.trellis/spec/backend/admin-dashboard.md:105` — 不支持新旧版本同时调度
- `.trellis/spec/backend/error-handling.md:344,426` — 任务引擎单进程前提；撤权通知与跨实例复核
- `.trellis/spec/backend/open-api.md:71` — 限流计数必须在数据库内
- `.trellis/spec/backend/agent.md:192` — 确认或拒绝时的进程内会话锁
- `.trellis/spec/backend/database-guidelines.md:552-554` — 任务表的单进程前提
- `.trellis/spec/backend/admin-backups.md` — 整库备份（本调研未通读，只核对了代码）

## Caveats / Not Found

- 全部结论来自静态阅读，没有启动多进程做实验，也没有运行测试。
- 任务引擎目录 `services/task_engine/` 的内部没有读。凡是结论取决于它的地方都标了「未核实」：任务流订阅、管理员取消的收尾、创建任务后本地 hub 的去向、其他进程引擎如何得知新任务、`_failed_cleanups` 是否有数据库可见的对应事实、每帧租约的复核实现。
- `services/task_service.py` 虽在引擎目录之外，但大量直接读取引擎的内存状态（`engine._running`、`engine.hubs`、`engine._failed_cleanups`、`engine.admin_cancellations` 等，共十余处）。本调研只核实了取消路径和排空路径；继续、重试、暂停等路径未逐一核对。
- 第 14 项：五个会话锁入口在事务中途提交后的窗口内，各不变量是否都有持久条件兜底，没有逐一核对。
- 第 15 项：并发 `alembic upgrade head` 在 PostgreSQL 与 SQLite 下的实际失败形态没有验证。
- 第 39 项：`chapter_claim.py` 只核对了占用与释放用条件 UPDATE 加行数判定，没有逐条核对方言分支。
- SQLite 多进程：`core/database.py:10-25` 没有设置 WAL 或 busy_timeout，文件锁在同一主机的多进程之间有效；内存库和网络文件系统上的库无法在进程间共享。README 把 SQLite 定位为本地开发与测试用（`README.md:77,170`）。
- 部署一致性前提：系统设置的部署基线（`services/system_settings_service.py:40-47`）和 MCP 部署默认值（`services/mcp/settings_gate.py:24-33`）都在各进程启动时从环境变量读取。各实例环境变量不一致时，没有后台覆盖的键会得到不同的有效值；`ENCRYPTION_KEY` 或 `JWT_SECRET` 不一致会让第 43 项的令牌和登录 Cookie 无法跨实例校验。
- 搜索方法：在 `backend/app/`（排除 `services/task_engine/` 与 `__pycache__`）用正则查找模块级可变容器、`asyncio` / `threading` 原语、`lru_cache`、弱引用字典、`ContextVar`、`create_task`、`CapacityLimiter`、模块级实例化，以及 `app.state` 上的对象；模块级跨请求状态只发现汇总表列出的这些。类实例字段只核对了挂在 `app.state` 上的长寿命对象。
