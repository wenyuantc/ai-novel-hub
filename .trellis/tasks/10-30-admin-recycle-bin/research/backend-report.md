# 后端实施报告

## 变更边界（写代码前确定）

- 当前差距：作品及删号仍物理级联删除；目标是作品生命周期、管理员回收站与安全恢复，任务停止成功后才能软删除。
- 行为归属：Project 模型/迁移、项目授权与写事务入口、Task 准入/取消/执行体收尾、回收站 service/API、lifespan、审计及备份适配。
- 修改范围：仅 backend/** 和本报告；未修改前端、Trellis spec、任务元数据/规划文档，未提交或推送，未触碰真实用户库或模型。
- 集中父项目锁和可见条件、复用 Task 取消核心及 AuditOperation；严格 owner 与人工管理员例外保持原语义。

## 实施内容

1. `0024_project_recycle_bin`：生命周期及协调字段、30天窗口、内部 `last_deleted_at` 高水位、Project→User RESTRICT、SQLite Project AUTOINCREMENT、系统审计 actor 可空。迁移保护项目子数据和两个 SQLite 序列；使用过生命周期后拒绝破坏性降级。
2. `project_lifecycle.py`：正常项目谓词、父行 no-op UPDATE 事务屏障、从数据库刷新状态但保留未flush普通标量/JSON，保护 agent TransactionSession 重复锁。保留身份/生命周期字段权威性。
3. `recycle_bin_service.py`：提交删除意图后取消并排空真实执行体、queued/paused清理、终态后的on_success及独立MCP确认请求；失败/超时/HTTP取消不留墓碑，成功同事务审计；仅管理员恢复/purge，启动补清+小时周期清理。
4. 普通列表/count、直接章节/分析/回写ID、Task读写/输出/SSE/清空/裁剪、开放API、备份流、灵感关联、普通人工写入等显式接入生命周期。备份流跨chunk删除中止，不继续输出隐藏内容；新生命周期字段不导出/导入，不进入agent内容效果快照。
5. Task Engine 登记真实项目执行链及请求清理；所有项目任务准入点与自动enqueue共享屏障。失败on_abort必须传播，运行任务收尾失败仍写终态保留失败证据，后续可重试；无执行体的孤立running可修复取消。
6. Agent写入口与等待conversation_lock后的重验、MCP网络返回后的重新准入；已发出的远端副作用不可撤回，遇删除只保存受控“结果未知”收尾，不写远端结果或自动续跑。
7. 用户名下任何作品（含回收站及待过期清理）都拒绝删号，返回code=user_has_projects与可用project_count；清空后仍保留自身/最后管理员护栏。
8. 管理员restore/purge、系统expire与原有管理删除接入安全审计；系统不伪造用户ID，已有审计日期清理契约保持。

## 冻结的API契约

- GET `/api/v1/admin/recycle-bin/projects`：q/owner_user_id/page/page_size；`{items,total}`。
- item：id/title/genre/status、owner_user_id/owner_username、deleted_by_user_id:int|null、deleted_by_username:string|null、deleted_at/expires_at:UTC ISO、restorable:boolean、chapter_count/total_words:int。
- POST `/{id}/restore`：`{deleted_at,confirm:true}` →204。
- POST `/{id}/purge`：`{deleted_at,title,confirm:true}` →204。
- 原DELETE `/projects/{id}` 仍204；收尾失败/超时409 `project_tasks_not_stopped`。
- deleted_at原样回传、逐字title、confirm严格boolean；last_deleted_at仅内部保存，不新增公开字段。
- AuditSummary新增 lifecycle_action:soft_delete|restore|purge|expire|null、retention_days:30|null、cancelled_task_count:number|null；actor_user_id:int|null，系统名称“系统自动清理”。

## 已运行验证（准确区分代码时点）

- 最新代码新增完整生命周期矩阵：39 passed，32.66秒。命令：`LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 UV_CACHE_DIR=/private/tmp/ainovel-uv-cache uv run --no-sync pytest tests/test_recycle_bin.py tests/test_project_lifecycle.py tests/test_recycle_bin_tasks.py tests/test_recycle_bin_migrations.py -q`。
- 上述39项包含：完整关联数据往返/精确purge、权限与直接ID旁路、严格确认/旧代次、UTC到期边界、系统actor、审计失败原子性、周期/启动清理重试、正文与组织成员写入屏障、MCP确认请求排空/等锁期间删除、running与KB abort失败后重试、重复锁保留待写JSON、备份流中止、冻结/回拨/重启的代次高水位、最高Project ID purge后不复用、带子数据且外键开/关的迁移及审计序列保留/降级保护。
- 最新 Ruff：`UV_CACHE_DIR=/private/tmp/ainovel-uv-cache uv run --no-sync ruff check .` → All checks passed；`git diff --check -- backend` →通过。
- 早一轮核心定向182 passed（116.99秒）：recycle/lifecycle/tasks/admin/admin-projects/admin-audit/open-api/project-transfer/migrations。其后按独立检查继续补安全回归，不把182当作最后全套证据。
- 早一轮 agent_mcp_skills：25 passed；更新后的组合targeted50 passed（40.29秒）。后续39项已覆盖终检新增修复。
- 第一轮全套 pytest 启动于终检修复与新增列之前，结果为1116 passed / 15 failed / 1 warning（547.27秒）；其日志 `/private/tmp/ainovel-recycle-full-backend.log` 不是最终冻结版验收证据。4项MCP本机socket测试为沙箱PermissionError，其他含旧物理删除语义断言、备份/阅读与关系并发，交接终检定位。需完整重跑最终版本，不可宣称全套通过。

## 环境与仍需终检事项

- 初始uv默认缓存目录读取被沙箱阻止，改用允许的/private/tmp缓存并--no-sync复用现有venv；未下载依赖。
- 初始C locale导致Alembic读取含中文注释配置时报ASCII解码错误，使用已安装的en_US.UTF-8执行，不改项目配置。
- PostgreSQL未实测：主会话已确认无本机psql/initdb/pg_ctl，Docker守护进程未运行。SQLite确定性并发/约束回归不能替代PG行锁证据。
- 真实浏览器与隔离HTTP全流程由主会话负责。增加last_deleted_at后旧create_all临时库需重建/迁移，不能直接复用旧schema。
- 交接给终检后 backend/** 冻结，由主会话指定的check代理接管直接修复和最终全套复验；本人不再并行写后端，避免覆盖他人修改。
