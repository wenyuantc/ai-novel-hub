# C2 实施与验收清单

依赖 C1 已归档。基于 `main` 上的 `dd65a06` 核对过领取、收尾和租约实现。下列清单记录各项实施结果，实际观测见[验收记录](research/verification.md)。

Trellis 自动载入长规范时只取开头的 32 KiB。实施或验收时按标题打开相关原文。后台任务表从第 520 行起，MCP 适配、插件管理和智能体工具分别从第 331、385、445 行起，PostgreSQL 隔离库与迁移冻结结构从第 151、169 行起。[数据库规范](../../spec/backend/database-guidelines.md) · [MCP 规范](../../spec/backend/skills-mcp.md) · [质量规范](../../spec/backend/quality-guidelines.md)

## 1. 建立可观察的双实例基线

- [x] 从 `tests/conftest.py` 的 `peer_app_factory` 创建共库的 A、B。验证两端使用不同的 worker 身份、连接池、授权运行时和 Settings 对象，两份设置从同一部署基线初始化。
- [x] 通过正常入口入队。两个引擎各选中**不同**候选后再同时争最后一个名额，记录进入数据库门、事务提交、处理器进入和退出四个时点。用有超时的事件控制顺序，断言双方都确实竞争过，再交换先后。单测里的退避或碰巧同时运行两次调度均不足以证明互斥。
- [x] 优先在现有用例旁补红测，记录当前行为。拟将两种数据库共用的 AC1–AC5 用例放入 `tests/test_task_scheduling_constraints.py`，跨应用设置用例放入 `tests/test_system_settings_cross_process.py`。PostgreSQL 复用 C1 专用库守卫和 schema 隔离。

## 2. 将领取、占用和归还放到数据库

- [x] 核对 Alembic head，按设计新增 `task_execution_slots`。登记原执行身份、任务轮次、用户和串行键，检查删除任务、复用 ID、暂停后继续以及重试产生新任务的情况。同步 ORM 表注册、迁移结构测试和作品导出的 `APP_SCHEMA`，维持 v16 作品文件兼容。
- [x] 在 `TaskEngine._claim_and_run` 现有授权门内读取两级有效上限，复核占用和同键队首。领取任务、业务资源归属、统计、占用登记共用一次提交。模拟提交失败及提交已生效但回执丢失，核对后者仅启动一次处理器。保留现有协作撤权、旧文档清理与管理员取消顺序。
- [x] 在原执行体收尾出口按冻结身份归还。覆盖回调已成功但裁剪或终态发布未结束、取消、故障与正常停机。模拟释放写入失败及回执丢失，验证重试不会重复业务回调，也不会释放后继轮次。
- [x] 将遗留占用回收接入 C1 已持久确认的停止或失联决定，先于执行者记录裁剪。模拟主库锁等待、失联决定尚未传播、接管者再度退出。只读配额查询不改变租约和业务收据。旧版本遗留任务、接管中任务与待核对任务均不新增占用。
- [x] 升降级测试分别覆盖空库、从 0066 带旧任务升级、存在活跃或无法确认的占用时拒绝降级、停机排空后成功降级。检查拒绝时版本号和原记录均完整。验证原生备份冷恢复及单书导出的表范围。

## 3. 验证领取顺序和读接口

- [x] **AC1、AC2** 全局上限 2、排队 6 个任务。再令单个用户限额为 1、排队 3 个任务。记录两个处理器的合计峰值、每轮入场和调用次数。分别在领取前后降额至 0，验证后项等待、在途任务完成、调高后继续。显式不限的用户仍受全局限额。
- [x] **AC3、AC4** 同项目章节分析按 ID 完成，较大 ID 先到领取门也不能越过队首。分别令队首用户满额和队首本地退避，验证同键后项等待、其他键仍可执行。在 PostgreSQL 锁住队首再尝试后项，验证不会跳过队首。
- [x] **AC5、AC6** 原执行体停在成功回调或最后发布时，分别用不同用户、不同键隔离全局、用户和串行键三种限制。回调完成后再次领取。独立恢复接管或待核对任务存在时，验证同键可用且配额显示正确。
- [x] **AC9** 统一配额 API、任务列表、详情和排队进度帧的数据来源。在候选尚未提交、已提交但确认迟到及已归还时，用 B 的独立连接观察。验证用户仍只能看到其有权读取的任务。对端占满时记录实际尝试次数，确认本地完成通知不会引起空转，定时重扫仍能推进。
- [x] **AC6 断言迁移** 依 [占用调研中的逐项清单](research/occupancy.md) 核对现有用例。在验收记录列出每处改动的测试名、原断言与新证据。提交前读 0，物理提交后的两个回执窗口均读 1。本地取消仍需验证执行游标受保护，管理员排空仍需验证完成前不会清理。

## 4. 让设置在两个应用生效

- [x] **AC7 注册** 在创建用户事务内复核部署默认和持久开关。让 A 的关闭先提交，B 的下一次注册应返回原有 403，没有新账号、登录记录或认证 Cookie。反向排序时验证先成功的注册仍有效。
- [x] **AC7 MCP** 覆盖插件管理所有入口、逐项导入、刷新和试用。A 关闭后 B 新发起的操作应在解析地址或连接前拒绝。本机受控服务记录连接和请求两种计数。再在握手完成前、分页之间、工具调用发送前和收到响应头后分别关闭，核对已发请求可收尾、后续业务请求被拒，错误和取消都释放发送保护。复跑智能体发送租约用例，保留原身份和工具调用次数。
- [x] **AC8 设置传播** 在无领取任务时也启动周期刷新。A 更新后在 B 的正常入口验证全局上限、默认用户上限、自动分析的派生行为和同一分钟的开放接口限流，记录从提交到生效的时间。检查 `/auth/config` 两开关与 `no-store`，以及 `false`、`null` 回到部署值、旧 revision 和并发更新。阻塞一次旧读取，验证它不会盖过新设置。
- [x] **AC6、AC8 故障恢复** 复跑系统设置保存错误和审计用例。读取损坏或提交未知时，不凭旧值放行新的注册、MCP 业务请求或领取。验证成功重读后恢复，应用退出或启动中途失败时收回轮询和在途写入。

## 5. 运行验证并记录结果

以下命令在 `backend/` 执行。PostgreSQL 16 使用已配置的本机专用 `ainovel_test`，以串行方式运行，记录实际执行数及跳过原因。

```bash
uv run pytest -q tests/test_task_scheduling_constraints.py tests/test_tasks.py tests/test_rbac_concurrency.py tests/test_user_quotas.py
uv run pytest -q tests/test_system_settings_cross_process.py tests/test_system_settings.py tests/test_system_settings_runtime.py tests/test_auth.py tests/test_open_api.py
uv run pytest -q tests/test_mcp_plugins.py tests/test_mcp_settings_gate.py tests/test_agent_mcp_send_lease.py
uv run pytest -q tests/test_task_worker_lease.py tests/test_task_worker_recovery_crash.py tests/test_task_worker_slow_headers.py tests/test_admin_chapter_claim_cancellation.py tests/test_recycle_bin_tasks.py
uv run pytest -q tests/test_task_worker_migration.py tests/test_migrations.py tests/test_backup_sqlite_restore.py tests/test_project_transfer.py
uv run ruff check .
env -u AINOVEL_POSTGRES_TEST_URL LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -n 4 --dist loadfile --durations=20
```

在配置 PostgreSQL 16 连接的终端运行下列命令。核实 AC1–AC4 和新迁移往返确实在 PostgreSQL 执行，另记录 SQLite 与 PostgreSQL 对慢请求、设置关闭和恢复的结果。

```bash
uv run pytest -n 0 -q -rs tests/test_task_scheduling_constraints.py tests/test_system_settings_cross_process.py tests/test_task_worker_lease_postgresql.py tests/test_task_worker_migration.py
```

- [x] **AC10** 在本任务 `research/verification.md` 按 AC1–AC10 登记证据。记录双端实际进入的顺序、峰值、设置生效间隔、数据库版本及完整测试结果。
- [x] 检查通过后更新后端对应规范和父任务进度，再归档本项。正式多进程部署由 C7 验收。若需回退 C2，先停旧执行与新领取，核验活跃占用和未确认写入，按已验证的降级步骤恢复与 0066 匹配的程序。降级受阻时保留数据库和占用记录供核对。
